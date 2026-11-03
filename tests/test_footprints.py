"""Tests for footprint assembly.

Seams under test:
  1. pick_source(), which dataset and layer an entry's polygon comes from
  2. simplify(), the build-time geometry reduction
  3. match_selector(), how a hand-drawn entry picks its admin boundaries

Deliberately not tested: the polygons themselves, which are upstream data.
"""

import unittest

from pipeline import footprints


KINDS = {
    "asher2007world/contemporary": "language-area",
    "asher2007world/traditional": "historical-area",
    "rantanen2021uralic": "language-area",
    "grierson1903lsi": "historical-area",
    "carling2025diacl": "reconstructed",
    "bouckaert2012indoeuropean": "reconstructed",
    "wikipedia2024officiallang": "country-shape",
}


class PickSource(unittest.TestCase):
    def test_the_world_atlas_wins_when_several_datasets_draw_an_entry(self):
        # Six datasets draw French. Mixing geometry from different atlases gives
        # neighbouring languages footprints on different conventions, so one has
        # to win, and asher2007world is the only global contemporary one.
        got = footprints.pick_source(
            ["carling2025diacl", "asher2007world/contemporary",
             "asher2007world/traditional", "wikipedia2024officiallang"], KINDS)
        self.assertEqual(got, "asher2007world/contemporary")

    def test_contemporary_beats_traditional(self):
        # Measured 2026-11-02: the two layers are identical for 94 of 99 entries.
        # Where they differ it is colonial spread, and traditional Portuguese is
        # Portugal alone, which would mark a Brazil pin wrong.
        got = footprints.pick_source(
            ["asher2007world/traditional", "asher2007world/contemporary"], KINDS)
        self.assertEqual(got, "asher2007world/contemporary")

    def test_a_reconstruction_loses_to_a_dated_but_observed_survey(self):
        # Punjabi is drawn by DiACL and by Grierson's 1903 survey of India. 1903
        # is old, but someone went and listened. A phylogeographic reconstruction
        # is an inference about where speakers used to be.
        got = footprints.pick_source(["carling2025diacl", "grierson1903lsi"], KINDS)
        self.assertEqual(got, "grierson1903lsi")

    def test_a_reconstruction_loses_even_to_a_national_outline(self):
        # The failure that drove this. DiACL's Yiddish is a blob in eastern
        # Poland, the pre-war heartland, and Yiddish is not spoken there now.
        # For a game asking where a language is spoken today, a modern border is
        # closer to the truth than an ancestral range.
        got = footprints.pick_source(
            ["carling2025diacl", "wikipedia2024officiallang"], KINDS)
        self.assertEqual(got, "wikipedia2024officiallang")

    def test_an_unclassified_dataset_is_not_silently_trusted(self):
        # A dataset added to the registry without a kind must not outrank a
        # reviewed one just by sorting first.
        got = footprints.pick_source(
            ["aaa_unclassified", "asher2007world/contemporary"], KINDS)
        self.assertEqual(got, "asher2007world/contemporary")


class SourceForEntry(unittest.TestCase):
    """A written decision outranks an automatic pick, and records a sign-off.

    The automatic pick is a preference order over datasets. It cannot know that
    DiACL's Yiddish is the pre-war heartland, or that an entry resolved to North
    Azerbaijani should not carry Iranian Azerbaijan too. Those are curation calls,
    so data/footprints.tsv overrides, and the override wins whether or not an
    atlas already draws the entry.
    """

    def test_a_hand_written_selector_beats_an_atlas_that_already_draws_it(self):
        spec = {"selector": "admin0 ISO_A2=AZ", "review": ""}
        self.assertTrue(footprints.use_override(spec, coverage="direct"))

    def test_an_entry_with_no_row_uses_the_atlas(self):
        self.assertFalse(footprints.use_override({}, coverage="direct"))

    def test_a_territory_only_row_does_not_override(self):
        # Welsh and Irish have rows carrying territory for the overlap report,
        # with no selector. Those must not start redrawing their footprints.
        spec = {"selector": "", "review": ""}
        self.assertFalse(footprints.use_override(spec, coverage="direct"))

    def test_an_approved_entry_stops_being_flagged(self):
        # Indonesian rests on a country outline, and for a national language the
        # outline is the speech area. Once a human says so it is not a finding.
        self.assertFalse(footprints.needs_review("country-shape", {"review": "approved 2026-11-02"}))
        self.assertTrue(footprints.needs_review("country-shape", {}))

    def test_an_override_is_never_flagged(self):
        self.assertFalse(footprints.needs_review("admin", {"selector": "admin0 ISO_A2=AZ"}))


class Simplify(unittest.TestCase):
    """Douglas-Peucker at build time, so the Worker never simplifies per request."""

    def test_a_point_on_a_straight_line_is_dropped(self):
        ring = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        self.assertEqual(footprints.simplify(ring, 0.1), [(0.0, 0.0), (2.0, 0.0)])

    def test_a_corner_further_than_the_tolerance_is_kept(self):
        ring = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
        self.assertEqual(footprints.simplify(ring, 0.1), ring)

    def test_a_ring_is_never_simplified_below_a_triangle(self):
        # A small island inside the tolerance reduces to two points, which is a
        # line, not a polygon. Point-in-polygon on a degenerate ring silently
        # reports every pin as outside, so the whole footprint goes quietly wrong.
        ring = [(0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.0)]
        got = footprints.simplify_ring(ring, tolerance=10.0)
        self.assertGreaterEqual(len(got), 4, "a closed ring needs 3 corners plus the repeat")
        self.assertEqual(got[0], got[-1], "and it has to stay closed")

    def test_a_simplified_ring_is_reclosed(self):
        ring = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (2.5, 2.5), (0.0, 0.0)]
        got = footprints.simplify_ring(ring, tolerance=0.1)
        self.assertEqual(got[0], got[-1])


class Selectors(unittest.TestCase):
    """Natural Earth admin-1 is province level, so regions are selected by field.

    There is no GB-SCT or ES-GA feature to look up. Scotland is 32 council areas
    sharing gu_a3=SCT, and Galicia is 4 provinces sharing region=Galicia.
    """

    def test_every_key_has_to_match(self):
        kind, filters = footprints.parse_selector("admin1 iso_a2=GB gu_a3=SCT")
        self.assertEqual(kind, "admin1")
        self.assertTrue(footprints.match_selector({"iso_a2": "GB", "gu_a3": "SCT"}, filters))
        self.assertFalse(footprints.match_selector({"iso_a2": "GB", "gu_a3": "WLS"}, filters))

    def test_a_bar_means_any_of_these(self):
        # Southeastern English is three English regions, not all of England.
        _, filters = footprints.parse_selector(
            "admin1 iso_a2=GB region=Greater London|South East|East")
        for region in ("Greater London", "South East", "East"):
            self.assertTrue(footprints.match_selector({"iso_a2": "GB", "region": region}, filters))
        self.assertFalse(footprints.match_selector({"iso_a2": "GB", "region": "North West"}, filters))

    def test_a_value_the_feature_does_not_carry_never_matches(self):
        _, filters = footprints.parse_selector("admin0 ISO_A2=JM")
        self.assertFalse(footprints.match_selector({"NAME": "Jamaica"}, filters))


if __name__ == "__main__":
    unittest.main()
