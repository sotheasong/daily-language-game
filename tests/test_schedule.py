"""The schedule generator proposes days. It never publishes them.

Four things are being checked here:

  1. the difficulty ramp, which is the shape of a session
  2. what may not sit in the same session
  3. what may not come back too soon
  4. that a day which cannot be filled is reported, not quietly shortened

Deliberately not tested: the date arithmetic, which is `datetime`'s job.
"""

import unittest

from pipeline import schedule


def entry(name, tier, kind="language"):
    return {"name": name, "tier": tier, "kind": kind}


# Enough of a pool to fill several days without the tests depending on
# data/entries.tsv, which changes as entries are added.
POOL = (
    [entry(f"One{i}", "1") for i in range(8)]
    + [entry(f"Two{i}", "2") for i in range(8)]
    + [entry(f"Three{i}", "3") for i in range(8)]
)

# One clip per entry unless a test says otherwise.
CLIPS = {e["name"]: [f"{e['name']}-clip"] for e in POOL}


class TheDifficultyRamp(unittest.TestCase):
    """Five rounds, easy to hard, from game-design.md.

    Round 2 and round 4 straddle two tiers on purpose. A session that is
    strictly 1-1-2-2-3 would never offer an easy second round, and the ramp is
    there so a new player's first thirty seconds are winnable.
    """

    def test_the_five_bands_are_the_documented_ramp(self):
        self.assertEqual(schedule.TIER_BANDS,
                         (("1",), ("1", "2"), ("2",), ("2", "3"), ("3",)))

    def test_each_round_draws_from_its_own_band(self):
        session = schedule.pick_session(POOL, CLIPS, seed=1)
        self.assertIsNotNone(session)
        for round_index, round_ in enumerate(session):
            self.assertIn(round_["tier"], schedule.TIER_BANDS[round_index])

    def test_a_session_is_five_rounds(self):
        self.assertEqual(len(schedule.pick_session(POOL, CLIPS, seed=1)), 5)

    def test_no_entry_appears_twice_in_one_session(self):
        session = schedule.pick_session(POOL, CLIPS, seed=1)
        names = [r["name"] for r in session]
        self.assertEqual(len(names), len(set(names)))


class WhatCannotShareASession(unittest.TestCase):
    """Two kinds of conflict, and only one of them is geographic.

    data/overlaps.tsv holds the 110 pairs that cover too much of the same
    ground to have different right answers. Accents are the other kind: Irish
    English and Scottish English do not overlap at all, their pins are 400 km
    apart, but hearing two English varieties in one session reads as a
    repeated round rather than a new one.
    """

    def test_an_overlapping_pair_never_shares_a_session(self):
        # Force the conflict: a pool with one tier 1 and one tier 2 option
        # each, declared as overlapping, has no legal session.
        pool = [entry("Azerbaijani", "1"), entry("Georgian", "2"),
                entry("Three0", "3")]
        clips = {e["name"]: [f"{e['name']}-c"] for e in pool}
        got = schedule.pick_session(
            pool, clips, forbidden={("Azerbaijani", "Georgian")}, seed=1)
        self.assertIsNone(got)

    def test_a_forbidden_pair_is_symmetric(self):
        # overlaps.tsv lists each pair once, in whichever order it found them.
        self.assertTrue(schedule.conflicts(
            entry("Georgian", "2"), entry("Azerbaijani", "1"),
            {("Azerbaijani", "Georgian")}))

    def test_two_accents_never_share_a_session(self):
        self.assertTrue(schedule.conflicts(
            entry("Irish English", "1", "accent"),
            entry("Scottish English", "1", "accent"),
            forbidden=set()))

    def test_an_accent_and_a_language_may_share_a_session(self):
        self.assertFalse(schedule.conflicts(
            entry("Irish English", "1", "accent"),
            entry("Japanese", "1"), forbidden=set()))

    def test_two_languages_with_no_declared_overlap_may_share(self):
        self.assertFalse(schedule.conflicts(
            entry("Japanese", "1"), entry("Finnish", "2"), forbidden=set()))


class WhatCannotComeBackYet(unittest.TestCase):
    """An entry and a clip each have to rest before being used again.

    The window is in days and is checked against what the generator has
    already proposed, so generating 30 days in one command enforces it across
    that run rather than only against history.
    """

    def test_a_recently_used_entry_is_not_offered(self):
        recent = {e["name"] for e in POOL if e["tier"] == "1"}
        recent.discard("One0")
        session = schedule.pick_session(POOL, CLIPS, recent_entries=recent, seed=1)
        # One0 is the only tier 1 entry left, so round 1 has to be it.
        self.assertEqual(session[0]["name"], "One0")

    def test_a_recently_used_clip_is_not_offered(self):
        pool = [entry("One0", "1")]
        clips = {"One0": ["fresh", "stale"]}
        session = schedule.pick_session(
            pool, clips, recent_clips={"stale"}, seed=1, rounds=1)
        self.assertEqual(session[0]["clip"], "fresh")

    def test_an_entry_whose_every_clip_is_spent_is_not_offered(self):
        pool = [entry("One0", "1"), entry("One1", "1")]
        clips = {"One0": ["spent"], "One1": ["ok"]}
        session = schedule.pick_session(
            pool, clips, recent_clips={"spent"}, seed=1, rounds=1)
        self.assertEqual(session[0]["name"], "One1")

    def test_an_entry_with_no_clip_at_all_is_not_offered(self):
        # 12 of 136 entries have audio today, so this is the common case.
        pool = [entry("One0", "1"), entry("One1", "1")]
        session = schedule.pick_session(pool, {"One1": ["ok"]}, seed=1, rounds=1)
        self.assertEqual(session[0]["name"], "One1")


class WhenADayCannotBeFilled(unittest.TestCase):
    """Report it. A four-round day is worse than a missing one.

    The generator is the thing that knows the pool is too thin, and it knows
    exactly which round ran out. Returning a short session would push that
    discovery to whoever opens the review page.
    """

    def test_an_unfillable_day_returns_nothing(self):
        self.assertIsNone(schedule.pick_session(
            [entry("One0", "1")], {"One0": ["c"]}, seed=1))

    def test_generate_stops_at_the_first_day_it_cannot_fill(self):
        # 24 entries, one clip each, nothing allowed to repeat inside a year.
        # Five go per day, and rounds 2 and 4 straddle, so they draw from
        # tiers 1 and 3 as well: four whole days, then four entries left over
        # that cannot make a fifth.
        days = schedule.generate(POOL, CLIPS, days=20, start="2026-12-01",
                                 window_days=365, seed=1)
        self.assertEqual(len(days), 4)

    def test_a_short_run_inside_the_pool_fills_completely(self):
        days = schedule.generate(POOL, CLIPS, days=3, start="2026-12-01",
                                 window_days=365, seed=1)
        self.assertEqual(len(days), 3)
        self.assertEqual([d["date"] for d in days],
                         ["2026-12-01", "2026-12-02", "2026-12-03"])


class ExplainingWhyARunStopped(unittest.TestCase):
    """Counts alone mislead, because at most one accent fits in a session.

    On 2026-11-10 the real pool was 12 entries with audio, 10 of them accents.
    Every band had something in it, so the per-round counts looked survivable,
    but rounds 3 and 5 had only accents and a session may hold one. The
    generator correctly proposed nothing, and the report has to say which
    round is the problem or the numbers read as a near miss.
    """

    def test_a_band_holding_only_accents_is_flagged(self):
        entries = [entry("Greek", "1"), entry("Nigerian English", "2", "accent"),
                   entry("Kenyan English", "3", "accent")]
        clips = {e["name"]: ["c"] for e in entries}
        rows = {r["round"]: r for r in schedule.shortfall(entries, clips)}
        self.assertEqual(rows[3]["languages"], 0)
        self.assertEqual(rows[1]["languages"], 1)

    def test_more_than_one_accent_only_band_is_called_impossible(self):
        # Rounds 3 and 5 both needing the session's one accent slot.
        entries = [entry("Greek", "1"), entry("Nigerian English", "2", "accent"),
                   entry("Kenyan English", "3", "accent")]
        clips = {e["name"]: ["c"] for e in entries}
        self.assertIn("accent", schedule.why_stuck(entries, clips).lower())

    def test_a_healthy_pool_has_nothing_to_explain(self):
        self.assertIsNone(schedule.why_stuck(POOL, CLIPS))


class TheWindowAcrossDays(unittest.TestCase):
    """The no-repeat rule has to hold across the run, not just within a day."""

    def test_no_entry_repeats_inside_the_window(self):
        days = schedule.generate(POOL, CLIPS, days=4, start="2026-12-01",
                                 window_days=365, seed=1)
        seen = [r["name"] for d in days for r in d["rounds"]]
        self.assertEqual(len(seen), len(set(seen)))

    def test_an_entry_may_return_once_the_window_has_passed(self):
        # A one day window lets the same entry come back the next day, which
        # is the degenerate case that proves the window is actually read.
        days = schedule.generate(POOL, CLIPS, days=12, start="2026-12-01",
                                 window_days=1, seed=1)
        self.assertEqual(len(days), 12)

    def test_the_same_seed_gives_the_same_schedule(self):
        a = schedule.generate(POOL, CLIPS, days=3, start="2026-12-01", seed=7)
        b = schedule.generate(POOL, CLIPS, days=3, start="2026-12-01", seed=7)
        self.assertEqual(a, b)

    def test_a_different_seed_gives_a_different_schedule(self):
        a = schedule.generate(POOL, CLIPS, days=3, start="2026-12-01", seed=7)
        b = schedule.generate(POOL, CLIPS, days=3, start="2026-12-01", seed=8)
        self.assertNotEqual(a, b)


class TheDraftItWrites(unittest.TestCase):
    """Every day comes out a draft, with a hash over its content.

    The approval gate stores a signature over content rather than a flag on a
    date, so that swapping a clip after approval silently returns the day to
    draft. The generator is what puts that hash there.
    """

    def test_a_generated_day_is_a_draft(self):
        day = schedule.generate(POOL, CLIPS, days=1, start="2026-12-01", seed=1)[0]
        self.assertEqual(day["status"], "draft")

    def test_the_content_hash_covers_the_clips(self):
        day = schedule.generate(POOL, CLIPS, days=1, start="2026-12-01", seed=1)[0]
        swapped = dict(day)
        swapped["rounds"] = [dict(day["rounds"][0], clip="other")] + day["rounds"][1:]
        self.assertNotEqual(schedule.content_hash(day),
                            schedule.content_hash(swapped))

    def test_the_content_hash_ignores_the_approval_fields(self):
        # Writing an approval into the day must not invalidate the hash it was
        # computed over, or no day could ever stay approved.
        day = schedule.generate(POOL, CLIPS, days=1, start="2026-12-01", seed=1)[0]
        before = schedule.content_hash(day)
        day["status"] = "approved"
        day["approved_by"] = "sothea"
        self.assertEqual(schedule.content_hash(day), before)

    def test_the_content_hash_is_stable_across_runs(self):
        a = schedule.generate(POOL, CLIPS, days=1, start="2026-12-01", seed=5)[0]
        b = schedule.generate(POOL, CLIPS, days=1, start="2026-12-01", seed=5)[0]
        self.assertEqual(schedule.content_hash(a), schedule.content_hash(b))


if __name__ == "__main__":
    unittest.main()
