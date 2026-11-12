"""The build refuses to compile a pool that would break a round later.

With 136 entries and a dozen fields each, the failure mode is a silently
missing field that breaks a round two months in, when nobody remembers
touching it. The six rules are from tech-stack.md:

  * a required field is absent
  * a clip has no licence recorded, or no voiced seconds
  * region weights do not sum to 1
  * a geometry is missing or fails to parse
  * an entry has fewer clips than its tier requires
  * a Glottocode does not resolve against Glottolog

Each returns a problem naming the entry and what to do, because the person
reading it is usually not the person who broke it.
"""

import unittest

from pipeline import validate


def entry(name="Khmer", tier="1", kind="language", glottocode="cent1989",
          status="ok"):
    return {"name": name, "tier": tier, "kind": kind,
            "glottocode": glottocode, "status": status}


class RequiredFields(unittest.TestCase):

    def test_a_complete_entry_passes(self):
        self.assertEqual(validate.check_fields(entry()), [])

    def test_a_missing_tier_is_a_problem(self):
        problems = validate.check_fields(dict(entry(), tier=""))
        self.assertEqual(len(problems), 1)
        self.assertIn("tier", problems[0]["detail"])

    def test_the_entry_is_named_in_the_problem(self):
        problems = validate.check_fields(dict(entry(name="Occitan"), tier=""))
        self.assertEqual(problems[0]["entry"], "Occitan")

    def test_a_tier_outside_one_to_three_is_a_problem(self):
        self.assertEqual(len(validate.check_fields(dict(entry(), tier="4"))), 1)

    def test_every_missing_field_is_reported_not_just_the_first(self):
        # Fixing them one build at a time is the slow way to find six.
        problems = validate.check_fields(
            {"name": "Mystery", "tier": "", "kind": "", "glottocode": "",
             "status": ""})
        self.assertGreaterEqual(len(problems), 3)


class Resolution(unittest.TestCase):
    """A Glottocode that does not resolve cannot be joined to anything."""

    def test_an_approved_status_passes(self):
        for status in ("ok", "local"):
            self.assertEqual(validate.check_resolution(
                dict(entry(), status=status)), [])

    def test_an_unresolved_entry_is_a_problem(self):
        problems = validate.check_resolution(dict(entry(), status="unresolved"))
        self.assertEqual(len(problems), 1)

    def test_a_drifted_code_is_a_problem(self):
        # Glottolog renumbered it, so the lock no longer matches upstream.
        problems = validate.check_resolution(dict(entry(), status="drifted"))
        self.assertIn("drifted", problems[0]["detail"])

    def test_an_ambiguous_name_is_a_problem(self):
        self.assertEqual(len(validate.check_resolution(
            dict(entry(), status="ambiguous"))), 1)


class ClipsPerTier(unittest.TestCase):
    """Tier 1 carries 4 clips, the rest 2, from audio-sourcing.md.

    Round 1 draws tier 1 every single day, so it burns through its clips
    four times faster than tier 3 does.
    """

    def test_the_requirement_is_four_for_tier_one_and_two_otherwise(self):
        self.assertEqual(validate.clips_required("1"), 4)
        self.assertEqual(validate.clips_required("2"), 2)
        self.assertEqual(validate.clips_required("3"), 2)

    OK_META = {c: {"licence": "CC0 1.0", "voiced_s": "9.0"} for c in "abcd"}

    def test_enough_clips_passes(self):
        self.assertEqual(
            validate.check_clips(entry(tier="1"), ["a", "b", "c", "d"],
                                 self.OK_META), [])

    def test_too_few_clips_is_a_problem(self):
        problems = validate.check_clips(entry(tier="1"), ["a", "b"],
                                        self.OK_META)
        self.assertIn("2 of 4", problems[0]["detail"])

    def test_no_clips_at_all_is_a_problem_at_this_level(self):
        # check_clips is only reached for entries that have some audio; run()
        # counts a completely silent entry as work not started instead.
        self.assertEqual(len(validate.check_clips(entry(), [], {})), 1)

    def test_a_clip_with_no_licence_is_a_problem(self):
        # The pool mixes CC BY-SA with CC0, and only one needs attributing.
        meta = {"a": {"licence": "", "voiced_s": "9.1"}}
        problems = validate.check_clips(entry(tier="2"), ["a", "b"], meta)
        self.assertTrue(any("licence" in p["detail"] for p in problems))

    def test_a_clip_with_no_voiced_seconds_is_a_problem(self):
        meta = {"a": {"licence": "CC0 1.0", "voiced_s": ""}}
        problems = validate.check_clips(entry(tier="2"), ["a", "b"], meta)
        self.assertTrue(any("voiced" in p["detail"] for p in problems))

    def test_a_clip_under_the_spec_is_a_problem(self):
        meta = {"a": {"licence": "CC0 1.0", "voiced_s": "6.2"}}
        problems = validate.check_clips(entry(tier="2"), ["a", "b"], meta)
        self.assertTrue(any("6.2" in p["detail"] for p in problems))


class RegionWeights(unittest.TestCase):
    """Shares are a probability distribution, so they sum to 1.

    data/shares.tsv is hand-estimated to about ten percent, so the tolerance
    is generous. It is there to catch a row that was edited and left at 0.7,
    not to audit the estimates.
    """

    def test_shares_summing_to_one_pass(self):
        self.assertEqual(
            validate.check_shares("Khmer", {"KH": 0.9, "TH": 0.07, "VN": 0.03}),
            [])

    def test_shares_summing_well_under_one_are_a_problem(self):
        problems = validate.check_shares("Khmer", {"KH": 0.7})
        self.assertIn("0.70", problems[0]["detail"])

    def test_shares_summing_over_one_are_a_problem(self):
        self.assertEqual(len(validate.check_shares("Khmer", {"KH": 0.9, "TH": 0.4})), 1)

    def test_a_small_rounding_error_is_tolerated(self):
        self.assertEqual(
            validate.check_shares("Khmer", {"KH": 0.705, "TH": 0.3}), [])

    def test_an_entry_with_no_shares_is_not_a_problem(self):
        # Only 28 of 136 entries have a shares row; a single-country entry
        # does not need one.
        self.assertEqual(validate.check_shares("Icelandic", {}), [])

    def test_a_negative_share_is_a_problem(self):
        self.assertEqual(
            len(validate.check_shares("Khmer", {"KH": 1.1, "TH": -0.1})), 1)


class Geometry(unittest.TestCase):

    def test_an_entry_with_a_footprint_passes(self):
        self.assertEqual(validate.check_geometry(entry(), {"Khmer"}), [])

    def test_a_missing_footprint_is_a_problem(self):
        problems = validate.check_geometry(entry(name="Occitan"), set())
        self.assertIn("footprint", problems[0]["detail"])


class TheWholeRun(unittest.TestCase):
    """Problems are grouped so one bad entry does not read as six failures."""

    def test_a_clean_pool_has_no_problems(self):
        report = validate.run(
            entries=[entry(tier="1")],
            clips={"Khmer": ["a", "b", "c", "d"]},
            clip_meta={c: {"licence": "CC0 1.0", "voiced_s": "9.0"}
                       for c in "abcd"},
            shares={"Khmer": {"KH": 1.0}},
            footprints={"Khmer"})
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])

    def test_a_pool_with_a_problem_is_not_ok(self):
        # A missing footprint, rather than missing audio, which run() counts
        # separately as work not started.
        report = validate.run(
            entries=[entry(tier="1")], clips={}, clip_meta={},
            shares={}, footprints=set())
        self.assertFalse(report["ok"])

    def test_problems_carry_the_entry_and_the_rule(self):
        report = validate.run(
            entries=[entry(tier="1")], clips={}, clip_meta={},
            shares={}, footprints=set())
        self.assertEqual(report["problems"][0]["entry"], "Khmer")
        self.assertIn("rule", report["problems"][0])

    def test_missing_audio_is_counted_apart_from_the_rest(self):
        # 124 of 136 entries have no audio yet, so mixing that into the same
        # list would bury every other problem under it.
        report = validate.run(
            entries=[entry(tier="1"), entry(name="Occitan", tier="3",
                                            glottocode="occi1241")],
            clips={}, clip_meta={}, shares={},
            footprints={"Khmer", "Occitan"})
        self.assertEqual(report["without_audio"], 2)
        self.assertEqual(report["problems"], [])

    def test_an_entry_with_some_audio_is_held_to_the_full_rule(self):
        # Having one clip means the entry is in play, so the shortfall is a
        # real problem rather than work not started.
        report = validate.run(
            entries=[entry(tier="1")],
            clips={"Khmer": ["a"]},
            clip_meta={"a": {"licence": "CC0 1.0", "voiced_s": "9.0"}},
            shares={}, footprints={"Khmer"})
        self.assertEqual(report["without_audio"], 0)
        self.assertTrue(any("1 of 4" in p["detail"] for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
