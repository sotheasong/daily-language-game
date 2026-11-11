"""Tests for the session-collision report.

Seams under test:
  1. territories_overlap(), whether two ISO codes cover common ground
  2. collisions(), which entry pairs share enough territory to collide

Deliberately not tested: the territory sets themselves, which come from Glottolog
and from hand-authored footprints and change as the pool grows.
"""

import unittest

from pipeline import overlaps


class TerritoriesOverlap(unittest.TestCase):
    def test_two_subdivisions_of_one_country_do_not_overlap(self):
        # The case the accent layer turns on. Scotland and Wales are both GB and
        # share no ground, so comparing at country level would collide every
        # British accent with every other one.
        self.assertFalse(overlaps.territories_overlap("GB-SCT", "GB-WLS"))

    def test_a_country_contains_its_own_subdivision(self):
        self.assertTrue(overlaps.territories_overlap("GB", "GB-SCT"))
        self.assertTrue(overlaps.territories_overlap("GB-SCT", "GB"))

    def test_different_countries_never_overlap(self):
        self.assertFalse(overlaps.territories_overlap("IE", "GB"))


class Collisions(unittest.TestCase):
    def test_two_entries_on_the_same_single_territory_collide(self):
        # Hindi and Indian English are both India and nothing else, so a session
        # holding both asks the player to pin the same place twice.
        got = overlaps.collisions({"Hindi": {"IN"}, "Indian English": {"IN"}}, threshold=0.5)
        self.assertEqual([(p["a"], p["b"]) for p in got], [("Hindi", "Indian English")])
        self.assertEqual(got[0]["score"], 1.0)

    def test_a_small_language_inside_a_much_larger_range_does_not_collide(self):
        # Catalan sits inside Spanish's range and the correct pins are still
        # Barcelona and Madrid. Worked by hand: 1 code in common, 3 in the union,
        # so 0.33, under the threshold. Range size is the signal here.
        got = overlaps.collisions(
            {"Spanish": {"ES", "MX", "AR"}, "Catalan": {"ES"}}, threshold=0.5)
        self.assertEqual(got, [])

    def test_an_entry_with_no_territory_is_skipped_rather_than_matching_everything(self):
        got = overlaps.collisions({"Esperanto": set(), "Hindi": {"IN"}}, threshold=0.5)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
