"""Tests for the name-to-Glottocode resolver.

Seams under test, agreed 2026-11-01:
  1. resolve(), the matching contract
  2. main(), the exit-code build gate

Deliberately not tested: the contents of Glottolog itself. Asserting that Spanish
is stan1288 tests upstream's dataset, not this code.
"""

import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest

from pipeline import glottocodes


def make_index(pairs, macroarea="Eurasia"):
    """Build a name index from (name, glottocode) pairs.

    Fake rows rather than the real 2.4 MB table, so tests stay fast and do not
    depend on a cache file or the network.
    """
    rows = [
        {
            "Name": name,
            "Glottocode": code,
            "Macroarea": macroarea,
            "Level": "language",
            "Latitude": "0",
            "Longitude": "0",
            "ISO639P3code": "",
            "Countries": "",
            "Family_ID": "",
            "Is_Isolate": "false",
        }
        for name, code in pairs
    ]
    return glottocodes.build_index(rows, {"language"})


class ResolveContract(unittest.TestCase):
    def test_alias_pointing_at_a_name_not_in_the_table_is_reported_as_broken(self):
        # Real bug from 2026-11-01. "Sakha" was aliased to "Yakut", which Glottolog
        # does not have. The entry came back unresolved, reading as a missing
        # language when it was actually a mistake in our own alias table.
        index = make_index([("Sakha", "yaku1245")])
        status, row, candidates = glottocodes.resolve(
            "Sakha", index, list(index), cutoff=0.75, aliases={"sakha": "Yakut"}
        )
        self.assertEqual(status, "alias_broken")
        self.assertIsNone(row)
        self.assertIn("Yakut", candidates)

    def test_a_close_but_wrong_name_is_never_auto_accepted(self):
        # The test this suite exists for. On the first real run, fuzzy matching
        # offered Greek -> Creek, a North American language, and Sakha -> Yaaku,
        # an African one. Accepting either would have put a wrong Glottocode into
        # every downstream join, and nothing would have looked broken until a
        # round placed Greek in Oklahoma.
        index = make_index([("Creek", "cree1270")], macroarea="North America")
        status, row, candidates = glottocodes.resolve(
            "Greek", index, list(index), cutoff=0.75, aliases={}
        )
        self.assertEqual(status, "unresolved")
        self.assertIsNone(row, "a fuzzy match must never be returned as a result")
        self.assertIn("Creek", candidates, "but it should still be offered as a candidate")

    def test_several_exact_matches_are_ambiguous_rather_than_picked(self):
        index = make_index([("Mari", "east2328"), ("Mari", "west2392")])
        status, row, candidates = glottocodes.resolve(
            "Mari", index, list(index), cutoff=0.75, aliases={}
        )
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(row)
        self.assertIn("east2328", candidates)
        self.assertIn("west2392", candidates)

    def test_alias_lookup_ignores_surrounding_whitespace(self):
        # Entry names arrive from a tab-separated file, so stray whitespace is
        # routine. Without the strip() on the alias lookup this returns
        # "unresolved", which reads as a gap in Glottolog rather than a stray space.
        index = make_index([("Mandarin Chinese", "mand1415")])
        status, row, _ = glottocodes.resolve(
            "  Mandarin  ", index, list(index), cutoff=0.75,
            aliases={"mandarin": "Mandarin Chinese"},
        )
        self.assertEqual(status, "ok")
        self.assertEqual(row["Glottocode"], "mand1415")


class UpstreamDrift(unittest.TestCase):
    """An approved Glottocode that moves upstream must not pass silently.

    Glottolog re-codes and renames languoids between releases. Without this check,
    a --refresh could quietly rewrite the join key under every clip, polygon and
    approved puzzle, and both the suite and the exit code would stay green.
    """

    def test_a_glottocode_that_moved_upstream_is_reported_as_drift(self):
        records = [{
            "input_name": "Kabyle", "status": "ok", "glottocode": "kaby9999",
            "matched_name": "Kabyle", "candidates": "",
        }]
        lock = {"Kabyle": {"glottocode": "kaby1243", "matched_name": "Kabyle"}}
        glottocodes.apply_lock(records, lock)
        self.assertEqual(records[0]["status"], "drifted")
        self.assertIn("kaby1243", records[0]["candidates"], "the approved code")
        self.assertIn("kaby9999", records[0]["candidates"], "what it resolves to now")

    def test_an_entry_missing_from_the_lock_is_left_alone(self):
        # Guard, green on arrival rather than driven: growing the pool past the
        # original 120 must not trip the drift gate. A newly added entry has no
        # approved code to differ from, so it stays ok and gets locked on the
        # next sign-off.
        records = [{
            "input_name": "Welsh", "status": "ok", "glottocode": "wels1247",
            "matched_name": "Welsh", "candidates": "",
        }]
        glottocodes.apply_lock(records, {"Kabyle": {"glottocode": "kaby1243", "matched_name": "Kabyle"}})
        self.assertEqual(records[0]["status"], "ok")


class LocalIdentifiers(unittest.TestCase):
    """Some entries have no Glottolog node and still belong in the pool.

    Scottish English is the first. Glottolog models Scots as its own language and
    Ulster English as a dialect of English, but has no node for Scottish Standard
    English at all. Naming the entry Scots to borrow a Glottocode would file
    Glaswegian-accented English as a different language, which is exactly the kind
    of quiet wrongness the resolver exists to prevent. So the entry carries a
    recorded local identifier and a hand-drawn footprint instead.
    """

    def test_an_entry_with_no_glottolog_node_takes_its_recorded_local_id(self):
        records = [{
            "input_name": "Scottish English", "status": "unresolved",
            "glottocode": "", "matched_name": "", "candidates": "Scots [scot1243]",
        }]
        local = {"Scottish English": {
            "local_id": "stan1293-scotland",
            "reason": "Glottolog has no Scottish English node, only Scots",
        }}
        glottocodes.apply_local_ids(records, local)
        self.assertEqual(records[0]["status"], "local")
        self.assertEqual(records[0]["glottocode"], "stan1293-scotland")
        self.assertIn("no Scottish English node", records[0]["candidates"])

    def test_a_local_id_never_shadows_a_real_glottolog_match(self):
        # Guard. If Glottolog later adds the node, the real code has to win, or a
        # stale local row would quietly keep a hand-drawn footprint in play.
        records = [{
            "input_name": "Irish English", "status": "ok", "glottocode": "iris1255",
            "matched_name": "Irish English", "candidates": "",
        }]
        glottocodes.apply_local_ids(
            records, {"Irish English": {"local_id": "stan1293-ireland", "reason": "x"}}
        )
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["glottocode"], "iris1255")


class ExitCodeGate(unittest.TestCase):
    """main() must exit non-zero if and only if something needs a human.

    This is what lets the resolver gate a build. A regression here lets a broken
    entry list through unnoticed.
    """

    def run_cli(self, table_rows, entry_names, lock_rows=None, want_rows=False,
                local_rows=None):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "languages.csv")
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(
                    "ID,Name,Macroarea,Latitude,Longitude,Glottocode,ISO639P3code,"
                    "Level,Countries,Family_ID,Language_ID,Closest_ISO369P3code,"
                    "First_Year_Of_Documentation,Last_Year_Of_Documentation,Is_Isolate\n"
                )
                for name, code in table_rows:
                    fh.write(f"{code},{name},Eurasia,0,0,{code},,language,,,,,,,false\n")

            entries = os.path.join(tmp, "entries.tsv")
            with open(entries, "w", encoding="utf-8") as fh:
                fh.write("\n".join(entry_names) + "\n")

            out = os.path.join(tmp, "out.tsv")
            argv = [
                "glottocodes", entries, "-o", out, "--cache", cache,
            ]
            if lock_rows is not None:
                lock = os.path.join(tmp, "lock.tsv")
                with open(lock, "w", encoding="utf-8") as fh:
                    fh.write("input_name\tglottocode\tmatched_name\n")
                    for row in lock_rows:
                        fh.write("\t".join(row) + "\n")
                argv += ["--lock", lock]
            if local_rows is not None:
                local = os.path.join(tmp, "local.tsv")
                with open(local, "w", encoding="utf-8") as fh:
                    fh.write("input_name\tlocal_id\treason\n")
                    for row in local_rows:
                        fh.write("\t".join(row) + "\n")
                argv += ["--local-ids", local]
            old_argv = sys.argv
            sys.argv = argv
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    code = glottocodes.main()
                if want_rows:
                    with open(out, newline="", encoding="utf-8") as fh:
                        rows = list(csv.DictReader(fh, delimiter="\t"))
                    return code, err.getvalue(), rows
                return code, err.getvalue()
            finally:
                sys.argv = old_argv

    def test_exits_zero_when_every_entry_resolves(self):
        code, _ = self.run_cli([("Kabyle", "kaby1243")], ["Kabyle"])
        self.assertEqual(code, 0)

    def test_exits_non_zero_when_an_alias_is_broken(self):
        # "Mandarin" is aliased to "Mandarin Chinese", which this table lacks.
        # A broken alias needs a human, so the gate must hold shut.
        code, err = self.run_cli([("Kabyle", "kaby1243")], ["Mandarin"])
        self.assertNotEqual(code, 0)
        self.assertIn("Mandarin", err)

    def test_an_accent_row_is_carried_through_to_the_output(self):
        # The entry file gained a third column when the accent layer landed.
        # Accents and languages resolve identically, but the scheduler has to tell
        # them apart, and Indian English must not be filed as a language.
        code, _, rows = self.run_cli(
            [("Indian English", "indi1255")], ["Indian English\t1\taccent"], want_rows=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["kind"], "accent")
        self.assertEqual(rows[0]["glottocode"], "indi1255")

    def test_a_row_with_no_kind_column_is_still_a_language(self):
        code, _, rows = self.run_cli(
            [("Kabyle", "kaby1243")], ["Kabyle\t3"], want_rows=True
        )
        self.assertEqual(rows[0]["kind"], "language")

    def test_a_recorded_local_id_does_not_hold_the_build_shut(self):
        # Scottish English will never resolve against Glottolog. The gate is for
        # things no human has looked at, and a local-ids row with a written reason
        # is a human having looked at it.
        code, err = self.run_cli(
            [("Kabyle", "kaby1243")], ["Scottish English\t1\taccent"],
            local_rows=[("Scottish English", "stan1293-scotland",
                         "Glottolog has no Scottish English node, only Scots")],
        )
        self.assertEqual(code, 0)
        self.assertIn("1 local", err)

    def test_exits_non_zero_when_an_approved_glottocode_drifts(self):
        # The table now hands Kabyle a different code than the lock approved.
        # Adopting it silently would repoint every join made under the old one.
        code, err = self.run_cli(
            [("Kabyle", "kaby9999")], ["Kabyle"],
            lock_rows=[("Kabyle", "kaby1243", "Kabyle")],
        )
        self.assertNotEqual(code, 0)
        self.assertIn("drifted", err)


if __name__ == "__main__":
    unittest.main()
