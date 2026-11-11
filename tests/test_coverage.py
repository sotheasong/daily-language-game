"""Tests for the language-area coverage report.

Seams under test, mirroring the resolver's:
  1. classify(), which polygon an entry can actually use
  2. main(), the exit-code gate on entries with no polygon anywhere

Deliberately not tested: which Glottocodes Glottography happens to draw. That is
upstream's data, and it changes every time a dataset is added to the registry.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

from pipeline import coverage


class Classify(unittest.TestCase):
    def test_an_entry_absent_from_every_dataset_has_no_polygon(self):
        # The list this report exists to produce. These entries need an admin
        # boundary drawn by hand, or they come out of the pool.
        status, srcs = coverage.classify("kaby1243", parent="", children=(), index={})
        self.assertEqual(status, "none")
        self.assertEqual(srcs, "")

    def test_a_dialect_falls_back_to_its_parent_language_area(self):
        # Half the alias table resolves to a dialect node on purpose, because
        # Glottolog has no node for "the language": Serbian Standard and Croatian
        # Standard both sit under Serbian-Croatian-Bosnian. The atlases usually
        # draw the parent, so calling these uncovered would be wrong.
        index = {"sout1528": ["asher2007world/traditional"]}
        status, srcs = coverage.classify(
            "serb1264", parent="sout1528", children=(), index=index
        )
        self.assertEqual(status, "via_parent")
        self.assertIn("asher2007world/traditional", srcs)
        self.assertIn("sout1528", srcs, "the report has to say whose area it borrowed")

    def test_dialect_areas_beat_the_parent_area_as_a_tighter_footprint(self):
        # Some languages are drawn only as their dialects. The union of those is
        # a real footprint, and a much better one than the family polygon sitting
        # above them: Indo-European as Hindi's area would hand the player Europe.
        index = {
            "indo1319": ["asher2007world/traditional"],
            "nort2641": ["grierson1903lsi"],
            "sout2222": ["grierson1903lsi"],
        }
        status, srcs = coverage.classify(
            "hind1269", parent="indo1319",
            children=("nort2641", "sout2222"), index=index,
        )
        self.assertEqual(status, "via_dialects")
        self.assertIn("nort2641", srcs)
        self.assertIn("sout2222", srcs)
        self.assertNotIn("indo1319", srcs, "never widen to the parent when dialects exist")

    def test_an_accent_never_inherits_its_base_language_area(self):
        # 15 of the 16 accent entries resolve to dialect nodes of English, and
        # Glottography draws an area for English itself. Inheriting it would score
        # a London pin as perfect for Jamaican English. An accent exists precisely
        # because its footprint is not the base language's, so the parent fallback
        # is not coverage here, it is a wrong answer wearing a right one's clothes.
        index = {"stan1293": ["asher2007world/contemporary"]}
        status, srcs = coverage.classify(
            "jama1265", parent="stan1293", children=(), index=index, kind="accent"
        )
        self.assertEqual(status, "none")
        self.assertEqual(srcs, "")

    def test_an_accent_with_its_own_drawn_area_still_counts(self):
        # Irish English is drawn directly by the Britain and Ireland atlas.
        index = {"iris1255": ["dedio2019britain"], "stan1293": ["asher2007world/contemporary"]}
        status, srcs = coverage.classify(
            "iris1255", parent="stan1293", children=(), index=index, kind="accent"
        )
        self.assertEqual(status, "direct")
        self.assertIn("dedio2019britain", srcs)


class FootprintQuality(unittest.TestCase):
    """A footprint made only of country outlines is not a speech area.

    wikipedia2024officiallang draws the countries where a language is official.
    For Standard Arabic that is arguably the right shape. For most entries it
    would score a pin against a border instead of a speech community, so the
    report has to say which footprints rest on that dataset alone.
    """

    COARSE = {"wikipedia2024officiallang"}

    def test_a_footprint_from_country_outlines_alone_is_flagged(self):
        self.assertTrue(coverage.is_country_shape_only(
            "wikipedia2024officiallang", self.COARSE))

    def test_one_real_language_area_is_enough_to_clear_the_flag(self):
        self.assertFalse(coverage.is_country_shape_only(
            "wikipedia2024officiallang; asher2007world/contemporary", self.COARSE))

    def test_labels_carrying_a_borrowed_code_still_parse(self):
        # via_parent and via_dialects append the code the area was borrowed from.
        self.assertTrue(coverage.is_country_shape_only(
            "wikipedia2024officiallang [sout1528]", self.COARSE))


class InheritedCoordinates(unittest.TestCase):
    """A dialect node usually carries its parent's point, not one of its own.

    Glottolog gives all 114 English dialect nodes English's own coordinate,
    53.0 / -1.0, which is in Derbyshire. Jamaican English's fallback centre is
    therefore the English Midlands, wrong by about 7,500 km while looking like
    perfectly ordinary data. The resolved record carries these as fallback
    coordinates, so they have to be flagged before something scores against one.
    """

    def test_a_point_identical_to_the_parents_is_flagged_as_inherited(self):
        self.assertTrue(
            coverage.has_inherited_coordinate("53.0", "-1.0", "53.0", "-1.0"))

    def test_an_entry_placed_on_its_own_is_not_flagged(self):
        # Hong Kong English is the one English dialect Glottolog places itself.
        self.assertFalse(
            coverage.has_inherited_coordinate("22.30173", "114.17452", "53.0", "-1.0"))

    def test_an_entry_with_no_parent_point_is_not_flagged(self):
        self.assertFalse(coverage.has_inherited_coordinate("40.0", "-3.0", "", ""))


class ExitCodeGate(unittest.TestCase):
    """main() must exit non-zero when an entry has no polygon to score against.

    An uncovered entry is unscoreable. Shipping one means a round where every pin
    is wrong by an amount nobody can compute.
    """

    HEADER = (
        "ID,Name,Macroarea,Latitude,Longitude,Glottocode,ISO639P3code,"
        "Level,Countries,Family_ID,Language_ID,Closest_ISO369P3code,"
        "First_Year_Of_Documentation,Last_Year_Of_Documentation,Is_Isolate\n"
    )
    AREA_HEADER = (
        "ID,Name,Macroarea,Latitude,Longitude,Glottocode,ISO639P3code,"
        "Feature_IDs,Glottolog_Languoid_Level,Family,Speaker_Area\n"
    )

    def run_cli(self, glottolog_rows, drawn_codes, entry_codes):
        """glottolog_rows: (code, name, level, language_id). entry_codes: codes to report on."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "cache")
            os.makedirs(os.path.join(cache, "glottography"))
            with open(os.path.join(cache, "languages.csv"), "w", encoding="utf-8") as fh:
                fh.write(self.HEADER)
                for code, name, level, parent in glottolog_rows:
                    fh.write(f"{code},{name},Eurasia,0,0,{code},,{level},XX,fam1234,{parent},,,,false\n")

            # A cached index file is never re-fetched, so the fake URL is never hit.
            with open(os.path.join(cache, "glottography", "fake.csv"), "w", encoding="utf-8") as fh:
                fh.write(self.AREA_HEADER)
                for code in drawn_codes:
                    fh.write(f"{code},{code},Eurasia,0,0,{code},,1 2,language,Fam,languages\n")

            registry = os.path.join(tmp, "sources.tsv")
            with open(registry, "w", encoding="utf-8") as fh:
                fh.write("dataset\tlayer\tlicence\turl\n")
                fh.write("fake\t\tCC-BY-4.0\thttps://example.invalid/languages.csv\n")

            resolved = os.path.join(tmp, "resolved.tsv")
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write("input_name\ttier\tstatus\tglottocode\tmatched_name\tlevel\tmacroarea\tcountries\n")
                for code in entry_codes:
                    fh.write(f"{code}\t1\tok\t{code}\t{code}\tlanguage\tEurasia\tXX\n")

            out = os.path.join(tmp, "coverage.tsv")
            old_argv = sys.argv
            sys.argv = ["coverage", resolved, "-o", out,
                        "--sources", registry, "--cache-dir", cache]
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    code = coverage.main()
                return code, err.getvalue()
            finally:
                sys.argv = old_argv

    def test_exits_zero_when_every_entry_has_an_area(self):
        code, _ = self.run_cli(
            [("kaby1243", "Kabyle", "language", "")], ["kaby1243"], ["kaby1243"]
        )
        self.assertEqual(code, 0)

    def test_exits_non_zero_and_names_the_entry_with_no_area_anywhere(self):
        code, err = self.run_cli(
            [("kaby1243", "Kabyle", "language", "")], [], ["kaby1243"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("kaby1243", err, "the report has to name what needs drawing")


if __name__ == "__main__":
    unittest.main()
