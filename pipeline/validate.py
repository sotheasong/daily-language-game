#!/usr/bin/env python3
"""Refuse to compile a pool that would break a round later.

With 136 entries and a dozen fields each, the failure mode is not a crash. It
is a silently missing field that breaks one round two months from now, when
nobody remembers touching it. So every rule here is checked for every entry
and every problem is reported at once, rather than failing on the first.

The six rules are from tech-stack.md. Each problem names the entry, the rule
and what to do about it, because whoever reads the output is usually not
whoever broke it.

**Entries with no audio at all are counted separately.** 124 of 136 entries
have none today, and folding that into the problem list would bury every
other finding under work that simply has not started. An entry with even one
clip is in play, and is then held to the full requirement.

    python3 -m pipeline.validate
    python3 -m pipeline.validate --strict      # exit 1 on any problem
"""

import argparse
import csv
import json
import os
import sys

from pipeline import schedule, sources

# Tier 1 carries 4 clips, the rest 2, from audio-sourcing.md. Round 1 draws
# tier 1 every single day, so it burns clips four times faster than tier 3.
CLIPS_BY_TIER = {"1": 4}
CLIPS_DEFAULT = 2

# What a published clip must hold, matching pipeline.clips.TARGET_VOICED_S.
MIN_VOICED_S = 8.0

# data/shares.tsv is hand-estimated to about ten percent, so the tolerance is
# generous on purpose. It is here to catch a row left at 0.7 after an edit,
# not to audit the estimates.
SHARE_TOLERANCE = 0.05

REQUIRED = ("name", "tier", "kind", "glottocode", "status")
APPROVED_STATUS = ("ok", "local")

DEFAULT_RESOLVED = os.path.join(sources.DATA_DIR, "resolved.tsv")
DEFAULT_SHARES = os.path.join(sources.DATA_DIR, "shares.tsv")
DEFAULT_FOOTPRINTS = os.path.join(sources.DATA_DIR, "footprints.geojson")
DEFAULT_CLIPS = schedule.DEFAULT_CLIPS


def _problem(entry, rule, detail, fix):
    return {"entry": entry, "rule": rule, "detail": detail, "fix": fix}


def clips_required(tier):
    """How many clips an entry of this tier needs."""
    return CLIPS_BY_TIER.get(tier, CLIPS_DEFAULT)


def check_fields(entry):
    """Every required field present, and the tier one of three known values."""
    name = entry.get("name") or "(unnamed)"
    out = []
    for field in REQUIRED:
        if not (entry.get(field) or "").strip():
            out.append(_problem(
                name, "required field", f"{field} is empty",
                f"fill {field} in data/entries.tsv, or re-run the resolver"))
    tier = (entry.get("tier") or "").strip()
    if tier and tier not in ("1", "2", "3"):
        out.append(_problem(
            name, "required field", f"tier is {tier!r}, not 1, 2 or 3",
            "tier is geographic distinctiveness; see game-design.md"))
    return out


def check_resolution(entry):
    """A Glottocode that does not resolve cannot be joined to anything.

    `local` counts as resolved: those are the entries Glottolog does not model
    the way this game needs, recorded deliberately in data/local-ids.tsv.
    """
    status = (entry.get("status") or "").strip()
    if status in APPROVED_STATUS:
        return []
    return [_problem(
        entry.get("name") or "(unnamed)", "resolution",
        f"glottocode status is {status!r}",
        "re-run python3 -m pipeline.glottocodes, then record a decision in "
        "data/local-ids.tsv or data/glottocodes.lock.tsv")]


def check_clips(entry, clips, clip_meta):
    """Enough clips, each with a licence and a measured length."""
    name = entry.get("name") or "(unnamed)"
    tier = (entry.get("tier") or "").strip()
    need = clips_required(tier)
    out = []
    if len(clips) < need:
        out.append(_problem(
            name, "clip count", f"{len(clips)} of {need} clips for tier {tier}",
            "pull more of this entry's corpus, or raise --per-speaker"))

    for clip in clips:
        meta = clip_meta.get(clip) or {}
        if not (meta.get("licence") or "").strip():
            out.append(_problem(
                name, "clip licence", f"{clip} has no licence recorded",
                "rebuild with --licence; the pool mixes CC BY-SA with CC0 and "
                "only one needs attributing"))
        raw = (meta.get("voiced_s") or "").strip()
        if not raw:
            out.append(_problem(
                name, "clip length", f"{clip} has no voiced seconds recorded",
                "rebuild it; voiced_s is measured off the encoded file"))
            continue
        try:
            voiced = float(raw)
        except ValueError:
            out.append(_problem(
                name, "clip length", f"{clip} has voiced_s {raw!r}",
                "rebuild it"))
            continue
        if voiced < MIN_VOICED_S:
            out.append(_problem(
                name, "clip length",
                f"{clip} holds {voiced:.1f}s voiced, under the {MIN_VOICED_S}s spec",
                "rebuild with a wider --target, or drop the clip"))
    return out


def check_shares(name, shares):
    """Country shares are a distribution, so they sum to 1.

    An entry with no shares row is fine. Only 28 of 136 have one, because a
    single-country entry does not need one.
    """
    if not shares:
        return []
    total = sum(shares.values())
    out = []
    if abs(total - 1.0) > SHARE_TOLERANCE:
        out.append(_problem(
            name, "region weights", f"shares sum to {total:.2f}, not 1.00",
            "fix the row in data/shares.tsv"))
    negative = sorted(k for k, v in shares.items() if v < 0)
    if negative:
        out.append(_problem(
            name, "region weights", f"negative share for {', '.join(negative)}",
            "fix the row in data/shares.tsv"))
    return out


def check_geometry(entry, footprint_entries):
    """Something to draw, and to score a pin against."""
    name = entry.get("name") or "(unnamed)"
    if name in footprint_entries:
        return []
    return [_problem(
        name, "geometry", "no footprint in data/footprints.geojson",
        "re-run python3 -m pipeline.footprints, or add a source in "
        "data/footprints.tsv")]


def run(entries, clips, clip_meta, shares, footprints):
    """Every rule against every entry, with missing audio counted apart."""
    problems, without_audio = [], 0
    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        problems += check_fields(entry)
        problems += check_resolution(entry)
        problems += check_shares(name, shares.get(name) or {})
        problems += check_geometry(entry, footprints)
        if clips.get(name):
            problems += check_clips(entry, clips[name], clip_meta)
        else:
            without_audio += 1
    return {
        "ok": not problems,
        "problems": problems,
        "entries": len(entries),
        "without_audio": without_audio,
    }


# ---------------------------------------------------------------------------
# Reading the data.


def read_entries(path=DEFAULT_RESOLVED):
    """Entries with their resolution status, from data/resolved.tsv.

    Read from the resolver's output rather than entries.tsv, because the
    status and the Glottocode are what two of the rules check.
    """
    with open(path, encoding="utf-8") as fh:
        return [{"name": row["input_name"], "tier": row["tier"],
                 "kind": row["kind"] or "language",
                 "glottocode": row["glottocode"], "status": row["status"]}
                for row in csv.DictReader(fh, delimiter="\t")]


def read_shares(path=DEFAULT_SHARES):
    """Entry to {country: share}, from data/shares.tsv."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            try:
                share = float(cols[2])
            except ValueError:
                continue
            out.setdefault(cols[0].strip(), {})[cols[1].strip()] = share
    return out


def read_footprints(path=DEFAULT_FOOTPRINTS):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {f["properties"]["entry"] for f in json.load(fh)["features"]}


def read_clip_meta(path=DEFAULT_CLIPS):
    """Clip hash to its recorded licence and length."""
    if not os.path.exists(path):
        return {}, {}
    clips, meta = {}, {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            clips.setdefault(row["label"], []).append(row["clip"])
            meta[row["clip"]] = row
    return clips, meta


def main():
    ap = argparse.ArgumentParser(
        description="Check the pool before anything compiles against it.")
    ap.add_argument("--resolved", default=DEFAULT_RESOLVED)
    ap.add_argument("--shares", default=DEFAULT_SHARES)
    ap.add_argument("--footprints", default=DEFAULT_FOOTPRINTS)
    ap.add_argument("--clips", default=DEFAULT_CLIPS)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on any problem, for use in a build")
    ap.add_argument("--rule", default=None,
                    help="only report this rule, e.g. 'clip licence'")
    args = ap.parse_args()

    entries = read_entries(args.resolved)
    clips, clip_meta = read_clip_meta(args.clips)
    report = run(entries, clips, clip_meta, read_shares(args.shares),
                 read_footprints(args.footprints))

    problems = report["problems"]
    if args.rule:
        problems = [p for p in problems if p["rule"] == args.rule]

    sys.stderr.write(
        f"{report['entries']} entries, {report['entries'] - report['without_audio']} "
        f"with audio, {len(report['problems'])} problems\n")

    by_rule = {}
    for problem in report["problems"]:
        by_rule.setdefault(problem["rule"], []).append(problem)
    if by_rule:
        sys.stderr.write("\n  rule                 entries\n")
        for rule, found in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            sys.stderr.write(f"  {rule:<20} {len(found):>7}\n")

    if problems:
        sys.stderr.write("\n")
        for problem in problems[:40]:
            sys.stderr.write(f"  {problem['entry']}: {problem['detail']}\n"
                             f"      -> {problem['fix']}\n")
        if len(problems) > 40:
            sys.stderr.write(f"  ... {len(problems) - 40} more\n")

    if report["without_audio"]:
        sys.stderr.write(
            f"\n{report['without_audio']} entries have no audio yet, which is "
            f"work not started\nrather than a problem, and is not counted "
            f"above.\n")

    if args.strict:
        return 1 if report["problems"] else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
