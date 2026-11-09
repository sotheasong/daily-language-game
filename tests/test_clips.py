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


if __name__ == "__main__":
    unittest.main()
