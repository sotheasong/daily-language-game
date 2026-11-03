"""Tests for the round scorer.

Seams under test:
  1. haversine(), distance on the globe
  2. nearest_km(), how far a pin sits outside a footprint
  3. score(), the points a pin earns

The constants are deliberately not asserted. They are being tuned, and a test
that pins 5000 to a magic number just has to be edited every time the curve
moves. What is asserted is the shape: the best pin wins, inside beats outside,
and the two meet without a step.
"""

import unittest

from pipeline import scoring


class Haversine(unittest.TestCase):
    def test_a_known_distance(self):
        # London to Paris, about 344 km by great circle.
        got = scoring.haversine(51.5074, -0.1278, 48.8566, 2.3522)
        self.assertAlmostEqual(got, 344, delta=6)

    def test_the_antimeridian_is_not_the_long_way_round(self):
        # Two points either side of the date line are close, not 39,000 km
        # apart. North American English spans it because of Alaska.
        got = scoring.haversine(52.0, 179.0, 52.0, -179.0)
        self.assertLess(got, 200)


class NearestKm(unittest.TestCase):
    SQUARE = [[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]]

    def test_a_pin_inside_is_zero_away(self):
        self.assertEqual(scoring.nearest_km(1.0, 1.0, self.SQUARE), 0.0)

    def test_a_pin_outside_measures_to_the_edge_not_the_centre(self):
        # One degree of longitude at the equator is about 111 km. Measuring to
        # the centroid instead would give about 223.
        got = scoring.nearest_km(1.0, 3.0, self.SQUARE)
        self.assertAlmostEqual(got, 111, delta=8)


class WeightLookup(unittest.TestCase):
    """A pin inside the footprint has to find a weight, even on a coastline.

    The density grid fills only cells whose centre falls inside the polygon, so
    a coastal city's cell can be empty while the polygon plainly contains the
    pin. Kingston, Lima and Bejaia all scored the bare floor for this reason,
    and Kingston came out below Montego Bay, which is the wrong way round for
    Jamaica's capital.
    """

    def test_an_empty_cell_borrows_from_its_neighbours(self):
        grid = {"origin": [0.0, 0.0], "cell": 1.0, "cells": {(1, 1): 0.8}}
        self.assertAlmostEqual(scoring.weight_at(grid, 0.5, 0.5), 0.8)

    def test_a_cell_with_its_own_weight_keeps_it(self):
        grid = {"origin": [0.0, 0.0], "cell": 1.0, "cells": {(0, 0): 0.2, (1, 1): 0.9}}
        self.assertAlmostEqual(scoring.weight_at(grid, 0.5, 0.5), 0.2)

    def test_a_pin_nowhere_near_a_cell_gets_nothing(self):
        grid = {"origin": [0.0, 0.0], "cell": 1.0, "cells": {(50, 50): 1.0}}
        self.assertEqual(scoring.weight_at(grid, 0.5, 0.5), 0.0)


class Score(unittest.TestCase):
    def test_the_densest_cell_earns_the_most_available(self):
        best = scoring.score(inside=True, weight=1.0, distance_km=0.0)
        self.assertEqual(best, scoring.MAX_POINTS)

    def test_inside_always_beats_outside(self):
        worst_inside = scoring.score(inside=True, weight=0.0, distance_km=0.0)
        best_outside = scoring.score(inside=False, weight=0.0, distance_km=1.0)
        self.assertGreater(worst_inside, best_outside)

    def test_the_two_halves_meet_without_a_step(self):
        # A pin on the border and a pin a metre outside should not differ by
        # hundreds of points. A visible cliff at the edge would teach players to
        # aim at borders rather than at where a language is spoken.
        edge = scoring.score(inside=True, weight=0.0, distance_km=0.0)
        just_out = scoring.score(inside=False, weight=0.0, distance_km=0.001)
        self.assertAlmostEqual(edge, just_out, delta=1)

    def test_further_away_always_scores_less(self):
        previous = scoring.score(inside=False, weight=0.0, distance_km=0.0)
        for km in (100, 500, 1500, 4000, 12000):
            here = scoring.score(inside=False, weight=0.0, distance_km=km)
            self.assertLess(here, previous)
            previous = here

    def test_a_pin_on_the_far_side_of_the_planet_still_scores_nothing_negative(self):
        self.assertGreaterEqual(scoring.score(inside=False, weight=0.0, distance_km=20000), 0)


class TierWeighting(unittest.TestCase):
    """A harder tier is worth more, so a small footprint is not a punishment.

    Measured 2026-11-03: the median tier 1 footprint is 370,745 km2, tier 2 is
    91,283 and tier 3 is 50,064. A pin is 4.1 and 7.4 times less likely to land
    inside by area alone, and Maltese is 313 km2 against Spanish at 10 million.

    The weights do not compensate in full. A player does not pin at random, so
    paying out seven times for a tier 3 round would make the other four
    pointless. They are the fourth root of the area ratio, which is a judgement
    anchored on a measurement rather than a derivation.
    """

    def test_a_harder_tier_pays_more_for_the_same_pin(self):
        easy = scoring.score(inside=True, weight=0.5, distance_km=0.0,
                             multiplier=scoring.TIER_WEIGHT["1"])
        hard = scoring.score(inside=True, weight=0.5, distance_km=0.0,
                             multiplier=scoring.TIER_WEIGHT["3"])
        self.assertGreater(hard, easy)

    def test_the_easiest_tier_carries_no_bonus(self):
        self.assertEqual(scoring.TIER_WEIGHT["1"], 1.0)

    def test_an_unknown_tier_gets_no_silent_bonus(self):
        self.assertEqual(scoring.tier_weight("not a tier"), 1.0)


class SessionTotals(unittest.TestCase):
    """Every session is out of the same maximum, whatever tiers it drew.

    Tier weighting makes a round worth more, which would make a tier 3 heavy day
    worth more than a tier 1 day. For a daily game that breaks the one number
    players compare, so the weights are scaled within a session: a hard round is
    worth more than the others *that day*, and the day still totals the same.
    """

    def test_a_session_is_always_out_of_the_same_maximum(self):
        for tiers in (["1", "1", "1", "1", "1"], ["3", "3", "2", "1", "2"]):
            got = scoring.session_multipliers(tiers)
            self.assertAlmostEqual(sum(got), len(tiers))

    def test_the_harder_round_is_still_worth_more_within_its_session(self):
        got = scoring.session_multipliers(["1", "1", "1", "3", "3"])
        self.assertGreater(got[3], got[0])

    def test_a_session_of_one_tier_pays_every_round_the_same(self):
        got = scoring.session_multipliers(["2", "2", "2"])
        self.assertEqual(got, [1.0, 1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
