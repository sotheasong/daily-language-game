#!/usr/bin/env python3
"""Propose days. Never publish them.

A session is five rounds, easy to hard, and the shape is fixed: round 1 is
tier 1, round 5 is tier 3, and rounds 2 and 4 straddle. The ramp exists so a
new player's first thirty seconds are winnable, which is the single biggest
predictor of whether they come back.

Three rules decide what may go in a day:

  * **Tier band**, above. The shape of the session.
  * **No conflict within a day.** Two kinds. The 110 pairs in
    data/overlaps.tsv cover too much of the same ground to have different
    right answers. Accents are the other kind, and not geographic: Irish
    English and Scottish English pins sit 400 km apart, but two English
    varieties in one session read as the same round twice.
  * **No repeat inside the window.** Entries and clips both rest, counted in
    days and enforced across the whole run, not just against history.

Every day comes out a draft carrying a hash of its content. The approval gate
stores a signature over that content rather than a flag on the date, so
swapping a clip under an approved day silently returns it to draft. That is
the behaviour you want: a day approved three weeks ago should not stay
approved after its audio changes.

Filling is a backtracking search over the five rounds. Greedy filling fails
on a thin pool, and the pool is thin: 12 of 136 entries have audio today. The
search is exact, so a reported failure means no legal session existed, not
that the generator gave up early.

    python3 -m pipeline.schedule --days 30 --start 2026-12-01
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import random
import sys

from pipeline import sources

# The ramp from game-design.md. Index is the round, contents are the tiers it
# may draw from.
TIER_BANDS = (("1",), ("1", "2"), ("2",), ("2", "3"), ("3",))
ROUNDS = len(TIER_BANDS)

# How long an entry or a clip rests before it can be proposed again. Tier 1 is
# the binding constraint: 27 entries, and round 1 needs one every day, so the
# ceiling is 27 days and three weeks leaves a little room.
WINDOW_DAYS = 21

# Fields that approval writes onto a day. The content hash must skip them, or
# recording an approval would invalidate the hash it was computed over.
APPROVAL_FIELDS = ("status", "approved_by", "approved_at", "content_hash",
                   "rejected_by", "rejected_at", "reason")

DEFAULT_ENTRIES = os.path.join(sources.DATA_DIR, "entries.tsv")
DEFAULT_OVERLAPS = os.path.join(sources.DATA_DIR, "overlaps.tsv")
DEFAULT_SCHEDULE = os.path.join(sources.DATA_DIR, "schedule.json")
DEFAULT_CLIPS = os.path.join(".cache", "clips", "clips.tsv")


def conflicts(a, b, forbidden):
    """Whether two entries may not sit in the same session.

    `forbidden` holds unordered name pairs from data/overlaps.tsv, listed once
    each in whichever order that file found them, so both orders are checked.

    The accent rule is separate because it is not geographic. It currently
    reduces to "no two accents", since every accent entry is an English
    variety. Give entries a base-variety column before adding a second
    accent family, or Brazilian and European Portuguese will be allowed to
    share a day.
    """
    if a["name"] == b["name"]:
        return True
    if (a["name"], b["name"]) in forbidden or (b["name"], a["name"]) in forbidden:
        return True
    return a.get("kind") == "accent" and b.get("kind") == "accent"


def fresh_clip(entry, clips, recent_clips, taken):
    """A clip for this entry that is neither resting nor already used today.

    Returns None when the entry has no audio at all, which is the common case
    while the corpus is being built out.
    """
    for clip in clips.get(entry["name"], ()):
        if clip not in recent_clips and clip not in taken:
            return clip
    return None


def pick_session(entries, clips, forbidden=frozenset(), recent_entries=frozenset(),
                 recent_clips=frozenset(), seed=0, rounds=ROUNDS):
    """Fill one day, or return None if no legal session exists.

    Backtracking over the rounds, with the candidate order shuffled from
    `seed` so the same seed always gives the same day. A None is a proof of
    impossibility rather than a failed attempt, which matters because the
    caller reports it to a human.
    """
    rng = random.Random(seed)
    pool = [e for e in entries if e["name"] not in recent_entries]

    # How many entries each tier can still offer, counting only those with a
    # clip free to use. Straddle rounds spend the roomier tier first so they
    # stop competing with the rounds that have no choice.
    room = {}
    for tier in {e["tier"] for e in pool}:
        room[tier] = sum(1 for e in pool if e["tier"] == tier
                         and fresh_clip(e, clips, recent_clips, set()))

    by_round = []
    for index in range(rounds):
        band = TIER_BANDS[index]
        candidates = [e for e in pool if e["tier"] in band]
        rng.shuffle(candidates)
        if len(band) > 1:
            # A preference, not a filter. The search still backtracks into the
            # scarce tier when the roomier one leads nowhere.
            candidates.sort(key=lambda e: -room.get(e["tier"], 0))
        by_round.append(candidates)

    chosen, used_clips = [], set()

    def fill(index):
        if index == rounds:
            return True
        for candidate in by_round[index]:
            if any(conflicts(candidate, picked, forbidden) for picked in chosen):
                continue
            clip = fresh_clip(candidate, clips, recent_clips, used_clips)
            if clip is None:
                continue
            chosen.append(dict(candidate, clip=clip, round=index + 1))
            used_clips.add(clip)
            if fill(index + 1):
                return True
            used_clips.discard(clip)
            chosen.pop()
        return False

    return list(chosen) if fill(0) else None


def content_hash(day):
    """A hash over what a player would experience, and nothing else.

    Covers the date, each round's entry and clip, in order. Skips the fields
    approval writes, so an approved day keeps matching its own signature until
    its content actually changes.
    """
    body = {
        "date": day["date"],
        "rounds": [[r["name"], r["tier"], r["clip"]] for r in day["rounds"]],
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def generate(entries, clips, days, start, forbidden=frozenset(),
             window_days=WINDOW_DAYS, seed=0, history=()):
    """Propose `days` consecutive drafts from `start`, stopping when stuck.

    The window is enforced across the run as well as against `history`, so one
    command generating 30 days does not propose the same entry twice in week
    one. Stopping early rather than skipping a date keeps the schedule
    contiguous: a gap in the middle would serve a fallback on a day the queue
    could in fact have covered.
    """
    first = datetime.date.fromisoformat(start)
    # (date, entry names, clip hashes) for everything already scheduled.
    timeline = [(datetime.date.fromisoformat(d["date"]),
                 {r["name"] for r in d["rounds"]},
                 {r["clip"] for r in d["rounds"]}) for d in history]

    out = []
    for offset in range(days):
        date = first + datetime.timedelta(days=offset)
        cutoff = date - datetime.timedelta(days=window_days)
        recent_entries = {n for when, names, _ in timeline if when > cutoff
                          for n in names}
        recent_clips = {c for when, _, cs in timeline if when > cutoff
                        for c in cs}

        session = pick_session(
            entries, clips, forbidden, recent_entries, recent_clips,
            # Vary the seed per day, or every day draws the same shuffle and
            # the schedule is five entries repeated.
            seed=seed * 1_000_003 + offset)
        if session is None:
            break

        day = {"date": date.isoformat(), "status": "draft", "rounds": session}
        day["content_hash"] = content_hash(day)
        out.append(day)
        timeline.append((date, {r["name"] for r in session},
                         {r["clip"] for r in session}))
    return out


# ---------------------------------------------------------------------------
# Reading the hand-authored data.


def read_entries(path=DEFAULT_ENTRIES):
    """Name, tier and kind from data/entries.tsv, skipping # comments."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 2 or not cols[0].strip() or not cols[1].strip():
                continue
            out.append({"name": cols[0].strip(), "tier": cols[1].strip(),
                        "kind": (cols[2].strip() if len(cols) > 2 and cols[2].strip()
                                 else "language")})
    return out


def read_forbidden(path=DEFAULT_OVERLAPS):
    """The pairs that may not share a session, from data/overlaps.tsv."""
    with open(path, encoding="utf-8") as fh:
        return {(row["entry_a"], row["entry_b"])
                for row in csv.DictReader(fh, delimiter="\t")}


def read_clips(path=DEFAULT_CLIPS):
    """Entry name to its clip hashes, from a clip build's provenance file.

    Keyed by label, which the clip builder maps to an entry name. A clip whose
    label is not an entry never reaches here, because the builder drops it.
    """
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out.setdefault(row["label"], []).append(row["clip"])
    return {name: sorted(hashes) for name, hashes in out.items()}


def shortfall(entries, clips):
    """Per round: how much of the pool it can actually draw on.

    `languages` is counted separately from `with_audio` because a session
    holds at most one accent. A band whose only options are accents can only
    be filled by spending that slot, so two such bands make every session
    impossible however healthy the totals look.
    """
    out = []
    for index, band in enumerate(TIER_BANDS):
        in_band = [e for e in entries if e["tier"] in band]
        with_audio = [e for e in in_band if clips.get(e["name"])]
        out.append({
            "round": index + 1, "band": "/".join(band),
            "entries": len(in_band), "with_audio": len(with_audio),
            "languages": sum(1 for e in with_audio if e.get("kind") != "accent"),
            "clips": sum(len(clips.get(e["name"], ())) for e in with_audio),
        })
    return out


def why_stuck(entries, clips):
    """One sentence naming the blocker, or None if the pool looks fine.

    Checked in the order a reader would want: an empty band first, then the
    accent squeeze, which is the one the raw counts hide.
    """
    rows = shortfall(entries, clips)
    empty = [r["round"] for r in rows if r["with_audio"] == 0]
    if empty:
        return (f"Round {', '.join(str(r) for r in empty)} has no entry with "
                f"audio at all, so no session can be built.")

    accent_only = [r for r in rows if r["languages"] == 0]
    if len(accent_only) > 1:
        rounds = ", ".join(str(r["round"]) for r in accent_only)
        return (f"Rounds {rounds} have only accents with audio, and a session "
                f"holds at most one accent, so no session can be built. "
                f"Every other round is fine. The fix is a non-accent entry "
                f"with audio in those bands, which means more Common Voice "
                f"locales rather than more clips.")
    if accent_only:
        return (f"Round {accent_only[0]['round']} has only accents with audio, "
                f"so every session must spend its one accent slot there.")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Propose draft sessions. Approval is a separate step.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start", default=None,
                    help="first date, YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--entries", default=DEFAULT_ENTRIES)
    ap.add_argument("--overlaps", default=DEFAULT_OVERLAPS)
    ap.add_argument("--clips", default=DEFAULT_CLIPS)
    ap.add_argument("--out", default=DEFAULT_SCHEDULE)
    ap.add_argument("--rejections", default=os.path.join(sources.DATA_DIR,
                                                        "rejections.json"),
                    help="clips rejected at review, never proposed again")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS,
                    help=f"days an entry and a clip rest (default {WINDOW_DAYS})")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the proposal without writing it")
    args = ap.parse_args()

    start = args.start or (
        datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    entries = read_entries(args.entries)
    forbidden = read_forbidden(args.overlaps)
    if not os.path.exists(args.clips):
        sys.stderr.write(
            f"no clip provenance at {args.clips}. Build clips first:\n"
            f"    python3 -m pipeline.clips <corpus> --manifest ... --build .cache/clips\n")
        return 1
    clips = read_clips(args.clips)

    # A rejected clip must never be proposed again, or the generator offers it
    # on a different date tomorrow. Imported here rather than at module scope
    # because approvals imports this module for the content hash.
    from pipeline import approvals
    rejected = approvals.rejected_clips(approvals.load_rejections(args.rejections))
    if rejected:
        clips = {name: [c for c in hashes if c not in rejected]
                 for name, hashes in clips.items()}
        clips = {name: hashes for name, hashes in clips.items() if hashes}

    # Keep days already proposed or approved that fall before the new run, so
    # the window is enforced against them.
    history = []
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            history = [d for d in json.load(fh)["days"] if d["date"] < start]

    days = generate(entries, clips, args.days, start, forbidden,
                    args.window, args.seed, history)

    sys.stderr.write(
        f"{len(entries)} entries, {len(clips)} with audio, "
        f"{sum(len(v) for v in clips.values())} clips, {len(forbidden)} forbidden pairs"
        + (f", {len(rejected)} clips rejected\n" if rejected else "\n"))
    sys.stderr.write(f"proposed {len(days)} of {args.days} days from {start}\n\n")

    if len(days) < args.days:
        sys.stderr.write("  round  band   entries  with audio  of those, languages  clips\n")
        for row in shortfall(entries, clips):
            sys.stderr.write(
                f"  {row['round']:>5}  {row['band']:<5}  {row['entries']:>7}  "
                f"{row['with_audio']:>10}  {row['languages']:>19}  "
                f"{row['clips']:>5}\n")
        reason = why_stuck(entries, clips)
        sys.stderr.write(f"\n{reason}\n" if reason else
                         "\nThe run stopped because a round had no entry left "
                         "with an unused clip.\nMore locales, or more clips "
                         "per entry, extend it.\n")

    for day in days[:5]:
        names = ", ".join(f"{r['name']} ({r['tier']})" for r in day["rounds"])
        sys.stderr.write(f"  {day['date']}  {names}\n")
    if len(days) > 5:
        sys.stderr.write(f"  ... {len(days) - 5} more\n")

    if args.dry_run:
        return 0

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.datetime.now(
                       datetime.timezone.utc).isoformat(timespec="seconds"),
                   "window_days": args.window, "seed": args.seed,
                   "days": history + days}, fh, indent=2)
        fh.write("\n")
    sys.stderr.write(f"\nwrote {args.out}. Every day is a draft; nothing is live.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
