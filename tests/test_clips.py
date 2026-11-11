"""Tests for clip selection: voiced seconds, and splicing to length.

Seams under test:
  1. voiced_segments(), where speech actually is in a recording
  2. voiced_seconds(), the number the whole filter turns on
  3. plan_splice(), which utterances to join to reach the target

Deliberately not tested: decoding. That is ffmpeg's job, at the edge of the
module, and the rest of this works on plain samples.
"""

import math
import unittest

from pipeline import clips

RATE = 16000


def tone(seconds, amplitude=9000, freq=180.0, rate=RATE):
    n = int(seconds * rate)
    return [int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def hiss(seconds, amplitude=60, rate=RATE):
    """Room tone. Not silence: a real recording never is."""
    n = int(seconds * rate)
    return [int(amplitude * math.sin(i * 1.7)) for i in range(n)]


class VoicedSegments(unittest.TestCase):
    def test_speech_between_room_tone_is_found(self):
        samples = hiss(1.0) + tone(2.0) + hiss(1.0)
        got = clips.voiced_segments(samples, RATE)
        self.assertEqual(len(got), 1)
        start, end = got[0]
        self.assertAlmostEqual(start, 1.0, delta=0.15)
        self.assertAlmostEqual(end, 3.0, delta=0.15)

    def test_a_recording_with_no_speech_has_no_segments(self):
        self.assertEqual(clips.voiced_segments(hiss(3.0), RATE), [])

    def test_a_breath_does_not_split_a_sentence(self):
        # A pause between words is not the end of speech. Without a hangover
        # every utterance shatters into syllables and the voiced total is right
        # only by accident.
        samples = hiss(0.5) + tone(1.0) + hiss(0.15) + tone(1.0) + hiss(0.5)
        self.assertEqual(len(clips.voiced_segments(samples, RATE)), 1)

    def test_a_real_pause_does_split_it(self):
        samples = hiss(0.5) + tone(1.0) + hiss(1.2) + tone(1.0) + hiss(0.5)
        self.assertEqual(len(clips.voiced_segments(samples, RATE)), 2)


class VoicedSeconds(unittest.TestCase):
    """The number the whole filter turns on.

    Common Voice utterances run 3 to 7 seconds against an 8 to 12 second spec,
    and a 3 second clip can hold a single word. File duration says nothing.
    """

    def test_it_counts_speech_and_not_the_gaps(self):
        samples = hiss(1.0) + tone(2.0) + hiss(2.0) + tone(1.0) + hiss(1.0)
        self.assertAlmostEqual(clips.voiced_seconds(samples, RATE), 3.0, delta=0.3)

    def test_a_long_file_of_nearly_nothing_counts_as_nearly_nothing(self):
        samples = hiss(6.0) + tone(0.4) + hiss(6.0)
        self.assertLess(clips.voiced_seconds(samples, RATE), clips.MIN_VOICED_S)


class PlanSplice(unittest.TestCase):
    """Reaching about 8 voiced seconds from utterances that are 3 to 7 long."""

    TAKES = [
        {"id": "a", "speaker": "s1", "voiced_s": 3.1},
        {"id": "b", "speaker": "s1", "voiced_s": 3.4},
        {"id": "c", "speaker": "s1", "voiced_s": 2.9},
        {"id": "d", "speaker": "s2", "voiced_s": 6.0},
    ]

    def test_it_joins_enough_takes_to_reach_the_target(self):
        got = clips.plan_splice(self.TAKES, speaker="s1", target_s=8.0)
        self.assertGreaterEqual(sum(t["voiced_s"] for t in got), 8.0)

    def test_it_never_mixes_speakers(self):
        got = clips.plan_splice(self.TAKES, speaker="s1", target_s=8.0)
        self.assertTrue(all(t["speaker"] == "s1" for t in got))

    def test_a_take_below_the_floor_is_not_used(self):
        takes = self.TAKES + [{"id": "x", "speaker": "s1", "voiced_s": 0.8}]
        got = clips.plan_splice(takes, speaker="s1", target_s=8.0)
        self.assertNotIn("x", [t["id"] for t in got])

    def test_it_gives_up_rather_than_shipping_a_short_clip(self):
        thin = [{"id": "a", "speaker": "s1", "voiced_s": 2.5}]
        self.assertEqual(clips.plan_splice(thin, speaker="s1", target_s=8.0), [])

    def test_it_stops_once_the_target_is_met(self):
        got = clips.plan_splice(self.TAKES, speaker="s1", target_s=6.0)
        self.assertLessEqual(len(got), 2)


class WavDecoding(unittest.TestCase):
    """WAV is read with the stdlib, so an audit needs nothing installed.

    EdAcc ships mono 32 kHz 16-bit WAV. ffmpeg is still the general path, for
    Common Voice's mp3 and anything else, but requiring it to measure a WAV
    would block the audit on an install for no reason.
    """

    def _wav_bytes(self, samples, rate=RATE):
        import array
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(array.array("h", samples).tobytes())
        return buf.getvalue()

    def test_a_wav_round_trips_through_the_stdlib(self):
        samples = tone(0.5)
        got, rate = clips.decode_wav_bytes(self._wav_bytes(samples))
        self.assertEqual(rate, RATE, "the file's own rate comes back with it")
        self.assertEqual(len(got), len(samples))
        self.assertEqual(list(got[:8]), samples[:8])

    def test_stereo_is_mixed_down(self):
        # A round clip is mono. Measuring one channel of a stereo interview
        # would count only the speaker who happened to be on the left.
        import array, io, wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(array.array("h", [1000, 3000] * 100).tobytes())
        got, _rate = clips.decode_wav_bytes(buf.getvalue())
        self.assertEqual(len(got), 100)
        self.assertEqual(got[0], 2000)


class VoicedWithinASpan(unittest.TestCase):
    """How much speech a trimmed span actually holds.

    build_clips recorded the sum of its sources' voiced seconds, which is the
    figure before trimming. A 13.2 second take cut down to a 9 second budget
    was filed as 13.2, so clips.tsv claimed more speech than the clip held and
    every audit run off that column was measuring the wrong thing.
    """

    SEGMENTS = [(0.0, 2.0), (3.0, 5.0), (6.0, 10.0)]

    def test_a_span_covering_everything_holds_every_segment(self):
        self.assertAlmostEqual(
            clips.voiced_within(self.SEGMENTS, 0.0, 10.0), 8.0)

    def test_a_span_is_credited_only_for_the_speech_inside_it(self):
        # Trimming to the first 5 seconds leaves two segments, not three.
        self.assertAlmostEqual(
            clips.voiced_within(self.SEGMENTS, 0.0, 5.0), 4.0)

    def test_a_segment_straddling_the_cut_is_counted_in_part(self):
        # The cut lands mid-segment at 8.0, so 2 of that segment's 4 seconds
        # survive, on top of the 4 before it.
        self.assertAlmostEqual(
            clips.voiced_within(self.SEGMENTS, 0.0, 8.0), 6.0)

    def test_silence_between_segments_is_not_speech(self):
        self.assertAlmostEqual(
            clips.voiced_within(self.SEGMENTS, 2.0, 3.0), 0.0)


class ChoosingAmongAttempts(unittest.TestCase):
    """Which attempt to keep when a clip has been built at several budgets.

    The publish step loses voiced seconds, by 0.34 to 1.78 on the first Hebrew
    batch, because loudnorm lifts the noise floor along with the speech and the
    detector is relative to that floor. The loss is not predictable from the
    source, so the builder plans, measures the encoded file, and retries wider
    when it came up short. These are the rules for picking the keeper.
    """

    def test_the_first_attempt_that_clears_spec_wins(self):
        # No reason to serve a 14 second clip when a 9 second one is in spec.
        attempts = [{"voiced_s": 7.2}, {"voiced_s": 8.4}, {"voiced_s": 11.9}]
        self.assertEqual(clips.pick_best(attempts, 8.0), attempts[1])

    def test_nothing_in_spec_keeps_the_longest(self):
        # Short is better than shorter, and the alternative is no clip at all.
        attempts = [{"voiced_s": 6.1}, {"voiced_s": 7.4}, {"voiced_s": 7.0}]
        self.assertEqual(clips.pick_best(attempts, 8.0), attempts[1])

    def test_no_attempts_means_no_clip(self):
        self.assertIsNone(clips.pick_best([], 8.0))


class Screening(unittest.TestCase):
    """Reject takes whose transcript makes them unusable, before anyone listens.

    EdAcc ships transcripts, so the screen is free. Three of these would break
    the game outright and the rest waste the curator's time.
    """

    def test_a_non_speech_marker_is_rejected(self):
        self.assertIsNotNone(clips.screen("IGNORE_TIME_SEGMENT_IN_SCORING"))

    def test_another_language_in_the_clip_is_fatal(self):
        # <FOREIGN> marks code switching. A clip of Kenyan English with Swahili
        # in it is a wrong answer dressed as a right one.
        reason = clips.screen("AND THEN SHE SAID <FOREIGN> AND I LAUGHED")
        self.assertIn("foreign", reason.lower())

    def test_two_people_talking_at_once_is_rejected(self):
        # <OVERLAP> means two voices, and in a dyadic corpus the other voice is
        # usually a different accent.
        self.assertIsNotNone(clips.screen("YEAH <OVERLAP> I KNOW WHAT YOU MEAN"))

    def test_a_backchannel_is_not_a_language_sample(self):
        # "MM HMM" clears a duration floor and teaches a player nothing.
        self.assertIsNotNone(clips.screen("MM HMM MM HMM MM"))

    def test_a_non_latin_script_is_not_a_backchannel(self):
        # The screen ran on [A-Za-z]+, so a Hebrew sentence tokenised to zero
        # words and every Hebrew take was binned as a backchannel. The corpus
        # is 100 locales and most of them are not Latin script.
        self.assertIsNone(clips.screen(
            "\u05d5\u05d0\u05e0\u05d9 \u05dc\u05d0 \u05d1\u05d8\u05d5\u05d7 \u05e9\u05dc\u05d2\u05d1\u05d9 \u05d4\u05e4\u05e8\u05dc\u05de\u05e0\u05d8 \u05d4\u05d9\u05e4\u05e0\u05d9 \u05d6\u05d4 \u05e1\u05d8\u05d8\u05d5\u05d8\u05d5\u05e8\u05d9"))

    def test_an_unspaced_script_is_judged_on_length(self):
        # Khmer, Thai, Chinese and Japanese put no spaces between words, so
        # token counting cannot work. A whole sentence is one token.
        self.assertIsNone(clips.screen(
            "\u1781\u17d2\u1789\u17bb\u17c6\u1798\u17b7\u1793\u178a\u17b9\u1784\u1790\u17b6\u178f\u17be\u179c\u17b6\u1798\u17b6\u1793\u1793\u17d0\u1799\u17a2\u17d2\u179c\u17b8"))

    def test_a_short_non_latin_utterance_is_still_rejected(self):
        # The length floor must not wave through a two-character backchannel.
        self.assertIsNotNone(clips.screen("\u5bf9\u5bf9\u5bf9"))

    def test_ordinary_conversation_is_kept(self):
        self.assertIsNone(clips.screen(
            "YEAH THE WEATHER'S ACTUALLY QUITE NICE I FEEL LIKE WE HAVEN'T HAD SUN"))

    def test_a_place_name_gives_the_answer_away(self):
        reason = clips.screen("I GREW UP JUST OUTSIDE NAIROBI AND MOVED LATER",
                              giveaways={"nairobi", "kenya"})
        self.assertIn("nairobi", reason.lower())

    def test_a_giveaway_matches_whole_words_only(self):
        # MALI must not fire on MALICIOUS, or half the corpus goes in the bin.
        self.assertIsNone(clips.screen(
            "IT WAS A MALICIOUS RUMOUR AND NOBODY BELIEVED A WORD OF IT",
            giveaways={"mali"}))


class VoicedSpan(unittest.TestCase):
    """What to cut from a take: the speech, plus a little air at each end.

    Cutting each voiced segment separately and butting them together chops the
    rhythm out of a sentence. The span runs from first speech to last, so the
    pauses inside stay where the speaker put them, and only the dead air at the
    ends goes.
    """

    def test_it_spans_first_speech_to_last(self):
        got = clips.voiced_span([(1.0, 2.0), (2.6, 3.5)], pad_s=0.0)
        self.assertEqual(got, (1.0, 3.5))

    def test_it_leaves_a_little_air(self):
        start, end = clips.voiced_span([(1.0, 3.0)], pad_s=0.2)
        self.assertAlmostEqual(start, 0.8)
        self.assertAlmostEqual(end, 3.2)

    def test_the_padding_does_not_run_off_the_start(self):
        start, _end = clips.voiced_span([(0.05, 2.0)], pad_s=0.3)
        self.assertEqual(start, 0.0)

    def test_the_padding_does_not_run_past_the_end(self):
        _start, end = clips.voiced_span([(1.0, 4.9)], pad_s=0.3, duration=5.0)
        self.assertEqual(end, 5.0)

    def test_a_take_with_no_speech_has_no_span(self):
        self.assertIsNone(clips.voiced_span([], pad_s=0.2))


class TrimToTarget(unittest.TestCase):
    """A take longer than the target is cut down, not used whole.

    Measured on the first real build: only 13 of 63 clips landed inside the
    8 to 12 second spec. The median was 15.7 and the longest 51, because a
    single take with 16 voiced seconds was spliced in entire.
    """

    def test_a_long_take_is_cut_at_the_target(self):
        # 15 voiced seconds in three bursts. Eight seconds of it ends inside
        # the second burst.
        segments = [(0.0, 5.0), (6.0, 11.0), (12.0, 17.0)]
        start, end = clips.trim_to_voiced(segments, target_s=8.0, pad_s=0.0)
        self.assertEqual(start, 0.0)
        self.assertAlmostEqual(end, 9.0, delta=0.01)

    def test_a_short_take_is_left_alone(self):
        segments = [(1.0, 3.0)]
        self.assertEqual(clips.trim_to_voiced(segments, target_s=8.0, pad_s=0.0),
                         (1.0, 3.0))

    def test_it_never_cuts_below_a_usable_clip(self):
        segments = [(0.0, 20.0)]
        start, end = clips.trim_to_voiced(segments, target_s=8.0, pad_s=0.0)
        self.assertAlmostEqual(end - start, 8.0, delta=0.01)


class PlanningMargin(unittest.TestCase):
    """Plan above the spec, because the delivered length varies.

    Measured over 67 built clips: the median lands at 8.2 voiced seconds
    against an 8.0 target, so there is no systematic shortfall. The spread is
    the problem. 17 of 67 came in under 8 and the shortest was 6.3, because
    the voiced measurement is taken before the loudness pass and the encode,
    and both move it a little either way.

    Aiming at 9 lifts the whole distribution so the tail clears the spec,
    which costs about a second of audio and buys a floor.
    """

    def test_planning_aims_above_what_a_clip_must_hold(self):
        self.assertGreater(clips.PLAN_VOICED_S, clips.TARGET_VOICED_S)

    def test_the_default_plan_uses_the_margin(self):
        takes = [
            {"id": "a", "speaker": "s1", "voiced_s": 4.5},
            {"id": "b", "speaker": "s1", "voiced_s": 4.5},
            {"id": "c", "speaker": "s1", "voiced_s": 4.5},
        ]
        got = clips.plan_splice(takes, speaker="s1")
        self.assertGreaterEqual(sum(t["voiced_s"] for t in got), clips.PLAN_VOICED_S)


class TightestFit(unittest.TestCase):
    """Given a choice, take the one that fits, not the longest one.

    Sorting longest first minimises joins, which is right when several takes
    are needed. It is wrong when one take alone would do: a 20 second take and
    a 9 second take both clear an 8 second target, and picking the 20 produced
    a 51 second clip.
    """

    TAKES = [
        {"id": "long", "speaker": "s1", "voiced_s": 20.0},
        {"id": "snug", "speaker": "s1", "voiced_s": 9.0},
        {"id": "short", "speaker": "s1", "voiced_s": 3.0},
    ]

    def test_one_take_that_fits_beats_a_much_longer_one(self):
        got = clips.plan_splice(self.TAKES, speaker="s1", target_s=8.0)
        self.assertEqual([t["id"] for t in got], ["snug"])

    def test_several_are_joined_when_none_is_enough_alone(self):
        takes = [
            {"id": "a", "speaker": "s1", "voiced_s": 4.0},
            {"id": "b", "speaker": "s1", "voiced_s": 3.5},
            {"id": "c", "speaker": "s1", "voiced_s": 3.0},
        ]
        got = clips.plan_splice(takes, speaker="s1", target_s=8.0)
        self.assertGreaterEqual(sum(t["voiced_s"] for t in got), 8.0)
        self.assertLessEqual(len(got), 3)


class ClipId(unittest.TestCase):
    """A clip is named for what is in it.

    The design wants filenames that give nothing away and a clip that changes
    when its contents change, so an approval cannot survive a swapped source.
    """

    PARTS = [("edacc/a.wav", 1.0, 4.0), ("edacc/b.wav", 0.5, 5.5)]

    def test_the_same_sources_give_the_same_name(self):
        self.assertEqual(clips.clip_id(self.PARTS), clips.clip_id(list(self.PARTS)))

    def test_a_changed_span_gives_a_different_name(self):
        other = [self.PARTS[0], ("edacc/b.wav", 0.5, 5.6)]
        self.assertNotEqual(clips.clip_id(self.PARTS), clips.clip_id(other))

    def test_the_name_says_nothing_about_the_answer(self):
        name = clips.clip_id([("edacc/kenyan-english-nairobi.wav", 0.0, 8.0)])
        self.assertNotIn("kenyan", name)
        self.assertNotIn("nairobi", name)
        self.assertTrue(name.isalnum())


if __name__ == "__main__":
    unittest.main()
