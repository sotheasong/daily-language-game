"""Tests for the speaker-density grid.

Seams under test:
  1. rasterise(), turning a footprint into grid cells
  2. cell_weight(), population times the share of speakers in that country
  3. normalise(), scaling an entry's grid so the densest cell is 1.0

Deliberately not tested: the population numbers, which are Natural Earth's.
"""

import unittest

from pipeline import weights


class GridSizing(unittest.TestCase):
    """Cells are sized by the footprint's area, not its bounding box.

    Spanish's bbox runs from California to Patagonia and is mostly ocean, so
    sizing to the bbox gave the whole language 181 cells, each about 250 km.
    Sizing to the area it actually covers gives it usable resolution.
    """

    def test_a_sparse_footprint_is_not_punished_for_its_bounding_box(self):
        ocean_bbox = (-118.0, -56.0, 4.0, 44.0)
        tight = weights.grid_for(ocean_bbox, area_km2=200_000, target=2000)
        sprawl = weights.grid_for(ocean_bbox, area_km2=10_000_000, target=2000)
        self.assertLess(tight.cell, sprawl.cell,
                        "less ground covered means finer cells, same bbox")

    def test_a_grid_never_exceeds_the_dimension_cap(self):
        # Yiddish is small patches on three continents, so a cell sized to its
        # area would need thousands of columns to span the bbox.
        grid = weights.grid_for((-100.0, 30.0, 55.0, 55.0), area_km2=5_000, target=2000)
        self.assertLessEqual(grid.cols, weights.MAX_DIM)
        self.assertLessEqual(grid.rows, weights.MAX_DIM)


class Rasterise(unittest.TestCase):
    def test_a_square_fills_the_cells_it_covers(self):
        # A 2x2 degree square on a 1 degree grid covers four cells, and the grid
        # is addressed from the bottom left so y grows north.
        grid = weights.Grid(lon0=0.0, lat0=0.0, cell=1.0, cols=4, rows=4)
        got = weights.rasterise([[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]], grid)
        self.assertEqual(got, {(0, 0), (1, 0), (0, 1), (1, 1)})

    def test_cells_outside_the_polygon_are_left_out(self):
        grid = weights.Grid(lon0=0.0, lat0=0.0, cell=1.0, cols=4, rows=4)
        got = weights.rasterise([[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]], grid)
        self.assertEqual(got, {(0, 0)})

    def test_a_hole_is_not_filled(self):
        # Even-odd, so a ring inside a ring cuts a hole. Lake Victoria sits
        # inside several African footprints and nobody speaks anything in it.
        outer = [(0, 0), (6, 0), (6, 6), (0, 6), (0, 0)]
        inner = [(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)]
        grid = weights.Grid(lon0=0.0, lat0=0.0, cell=1.0, cols=6, rows=6)
        got = weights.rasterise([outer, inner], grid)
        self.assertIn((0, 0), got)
        self.assertNotIn((2, 2), got)
        self.assertNotIn((3, 3), got)


class SharesSetSpeakerDensity(unittest.TestCase):
    """A country's shade is its share of the speakers. Three models to get here.

    1. Population times share left population in charge. Lao authored at 72%
       Laos came out 70% Thailand, because Isan is far more populous than Laos.
    2. Share split across a country's cells is a density. It dilutes large
       countries and crowns small dense ones: El Salvador outshone Mexico for
       Spanish, leaving one cell at 1.0 and the 99th percentile at 0.385.
    3. Share sets the level directly, and population only modulates within a
       country. Cambodia reads 15 times Thailand for Khmer, Phnom Penh reads as
       a hotspot inside Cambodia, and Mexico City beats rural Patagonia.

    The stored weight is linear, so scoring can use it directly. Display applies
    its own gamma, because a 15-to-1 ratio drawn linearly leaves Thailand
    invisible rather than merely lighter.
    """

    def test_area_does_not_change_the_shade(self):
        # Dividing a share by a country's cell count makes it a density, and
        # density picks the wrong winner: El Salvador is small and wholly
        # Spanish-speaking, so it outshone Mexico and left one cell at 1.0 with
        # the 99th percentile at 0.385. The design says pinning Mexico City
        # should beat pinning rural Patagonia, which is speakers near the pin,
        # not speakers per square kilometre.
        cells = {(0, 0): ("AA", 100.0), (1, 0): ("AA", 100.0), (2, 0): ("BB", 100.0)}
        got = weights.apply_shares(cells, {"AA": 0.5, "BB": 0.5})
        self.assertAlmostEqual(got[(2, 0)], got[(0, 0)])

    def test_a_country_with_more_speakers_is_darker(self):
        cells = {(0, 0): ("AA", 100.0), (1, 0): ("BB", 100.0)}
        got = weights.apply_shares(cells, {"AA": 0.9, "BB": 0.1})
        self.assertAlmostEqual(got[(0, 0)] / got[(1, 0)], 9.0)

    def test_a_city_is_darker_than_the_countryside_but_not_by_70_times(self):
        # Phnom Penh holds 1.4M against maybe 20k in a rural cell. Drawn
        # linearly that is 70 to 1, and the whole country goes pale around one
        # dot. Compressed and capped, the city reads as a hotspot instead.
        cells = {(0, 0): ("KH", 1_400_000.0), (1, 0): ("KH", 20_000.0)}
        got = weights.apply_shares(cells, {"KH": 1.0})
        ratio = got[(0, 0)] / got[(1, 0)]
        self.assertGreater(ratio, 1.5, "a city should still read as a hotspot")
        self.assertLess(ratio, 6.0, "but not flatten the country around it")

    def test_a_country_the_row_leaves_out_gets_nothing(self):
        cells = {(0, 0): ("LA", 100.0), (1, 0): ("VN", 9000.0)}
        got = weights.apply_shares(cells, {"LA": 1.0})
        self.assertNotIn((1, 0), got)


class CoastalCities(unittest.TestCase):
    """A city whose cell centre falls offshore must not lose its people.

    Kingston sits on Jamaica's south coast and the cell holding it has its
    centre in the sea, so the rasteriser leaves that cell out. Dropping the city
    with it discarded 1.2 million people and left Jamaica's capital weighted the
    same as Montego Bay, which has a tenth of them.
    """

    def test_a_city_in_an_empty_cell_snaps_to_the_nearest_filled_one(self):
        self.assertEqual(weights.snap_to_filled((5, 4), {(5, 5), (9, 9)}), (5, 5))

    def test_a_city_already_on_land_stays_put(self):
        self.assertEqual(weights.snap_to_filled((5, 5), {(5, 5), (5, 6)}), (5, 5))

    def test_a_city_nowhere_near_the_footprint_is_dropped(self):
        self.assertIsNone(weights.snap_to_filled((0, 0), {(50, 50)}))


class UrbanKernel(unittest.TestCase):
    """A city is a hill, not a dot.

    Dropping a city's whole population into the one cell that contains it makes
    a single bright square with nothing around it, which is what made the
    density plot look binned. Phnom Penh is 1.4 million people spread over a
    catchment tens of kilometres wide, so it should fall off smoothly.

    The kernel is normalised over the land it can reach, so a coastal city
    delivers all of its people onto land instead of losing half into the sea.
    That is why country borders stay a hard step: the kernel runs inside the
    clip, never across it.
    """

    GRID = None

    def setUp(self):
        self.GRID = weights.Grid(lon0=0.0, lat0=0.0, cell=0.05, cols=40, rows=40)
        self.filled = {(x, y) for x in range(40) for y in range(40)}

    def test_a_city_spreads_into_its_neighbours(self):
        field = {}
        weights.splat_city(field, self.GRID, 1.0, 1.0, 500_000, self.filled)
        centre = (int(1.0 / 0.05), int(1.0 / 0.05))
        neighbour = (centre[0] + 1, centre[1])
        self.assertGreater(field.get(neighbour, 0), 0, "the hill has slopes")
        self.assertGreater(field[centre], field[neighbour], "and a summit")

    def test_every_person_is_accounted_for(self):
        field = {}
        weights.splat_city(field, self.GRID, 1.0, 1.0, 500_000, self.filled)
        self.assertAlmostEqual(sum(field.values()), 500_000, delta=1)

    def test_a_coastal_city_puts_all_its_people_on_land(self):
        # Half the kernel falls in the sea. Those people live in the city, so
        # they belong on the land half, not lost overboard.
        land = {(x, y) for x, y in self.filled if x >= 20}
        field = {}
        weights.splat_city(field, self.GRID, 1.0, 1.0, 500_000, land)
        self.assertAlmostEqual(sum(field.values()), 500_000, delta=1)
        self.assertTrue(all(cell in land for cell in field))

    def test_a_bigger_city_reaches_further(self):
        self.assertGreater(weights.city_sigma_km(8_000_000),
                           weights.city_sigma_km(40_000))


class PopulationShares(unittest.TestCase):
    def test_an_unauthored_entry_derives_its_shares_from_population(self):
        # German needs no authored row. Population alone already gives
        # DE 82 / AT 9 / CH 5, which is about right.
        cells = {(0, 0): ("DE", 300.0), (1, 0): ("AT", 100.0)}
        got = weights.population_shares(cells)
        self.assertAlmostEqual(got["DE"], 0.75)
        self.assertAlmostEqual(got["AT"], 0.25)


class Normalise(unittest.TestCase):
    def test_the_densest_cell_becomes_one(self):
        got = weights.normalise({(0, 0): 20.0, (1, 0): 5.0, (2, 0): 0.0})
        self.assertEqual(got[(0, 0)], 1.0)
        self.assertEqual(got[(1, 0)], 0.25)

    def test_an_empty_grid_does_not_divide_by_zero(self):
        self.assertEqual(weights.normalise({(0, 0): 0.0}), {})


if __name__ == "__main__":
    unittest.main()
