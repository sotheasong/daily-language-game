#!/usr/bin/env python3
"""The approval gate. No session goes live without a human having played it.

Approval is a **signature over content, not a flag on a date**. A boolean
column would let a day approved three weeks ago keep reading as approved
after a clip was swapped underneath it, which is the one failure that puts an
unheard clip in front of players. So an approved day stores a hash of what
the reviewer saw, and `effective_status` recomputes that hash every time it
is asked. Change anything and the day quietly reverts to draft, which is the
behaviour you want.

Rejections are recorded against the **clip hash**, not the day, because the
clip is the thing that was wrong. Record it against the date and the
generator offers the same clip again tomorrow. The reasons double as a
growing specification of what a bad clip looks like, which is worth reading
before the next sourcing pass, so a blank one is refused.

The runway alarm is the part that actually bites. Approving one day at a time
means the game breaks the first week you are busy, so builds read
`runway()` and `runway_state()` and get noisier as the queue drains.

    python3 -m pipeline.approvals --status
"""

import argparse
import datetime
import json
import os
import sys

from pipeline import schedule

# Approved days remaining, from tech-stack.md. Counted forward from today and
# consecutive: if tomorrow is a draft, the nine approved days behind it do not
# help, because the queue breaks tomorrow.
RUNWAY_WARN = 14   # below this, every build warns
RUNWAY_FAIL = 7    # below this, the build fails
STATES = ("fine", "warn", "fail", "dry")

DEFAULT_SCHEDULE = schedule.DEFAULT_SCHEDULE
DEFAULT_REJECTIONS = os.path.join("data", "rejections.json")


def effective_status(day):
    """What a day's status really is, after checking its signature.

    A day claiming approval without a matching hash is a draft. That covers a
    swapped clip, a reordered session, a hand-edited file and a day written
    before the hash existed, all of which should need a fresh listen.
    """
    status = day.get("status", "draft")
    if status != "approved":
        return status
    if day.get("content_hash") != schedule.content_hash(day):
        return "draft"
    return "approved"


def approve(day, who, when):
    """Sign a day as it currently stands.

    The hash is recomputed rather than trusted, so what gets approved is what
    the reviewer just saw. A day whose clip was replaced after generation is
    approved in its new form, not refused for disagreeing with a stale hash.
    """
    out = dict(day)
    out["rounds"] = [dict(r) for r in day["rounds"]]
    out["status"] = "approved"
    out["approved_by"] = who
    out["approved_at"] = when
    out["content_hash"] = schedule.content_hash(out)
    out.pop("reason", None)
    return out


def reject(day, who, when, reason):
    """Mark a whole day as rejected. Its clips are rejected separately.

    Rejecting the day stops it being served. Whether a clip should never be
    offered again is a different judgement, so `reject_clip` is explicit
    rather than implied by this.
    """
    if not reason or not reason.strip():
        raise ValueError("a rejection needs a reason")
    out = dict(day)
    out["rounds"] = [dict(r) for r in day["rounds"]]
    out["status"] = "rejected"
    out["rejected_by"] = who
    out["rejected_at"] = when
    out["reason"] = reason.strip()
    return out


def reject_clip(rejections, clip, reason, when):
    """Record a clip as never to be proposed again, with why.

    Keyed by clip hash, so re-rejecting replaces the entry rather than
    growing the list. The newest reason wins, on the grounds that a second
    look is usually a sharper one.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "a rejection needs a reason. The reasons are the record of what a "
            "bad clip looks like, and a blank one teaches the next sourcing "
            "pass nothing.")
    out = [dict(r) for r in rejections if r["clip"] != clip]
    out.append({"clip": clip, "reason": reason.strip(), "rejected_at": when})
    return sorted(out, key=lambda r: r["clip"])


def rejected_clips(rejections):
    """Just the hashes, for the generator to skip."""
    return {r["clip"] for r in rejections}


def runway(days, today):
    """Approved days remaining, counted forward from `today`, consecutively.

    A hole in the dates breaks the run, because a date with no entry has
    nothing to serve. Days before today are history and do not count.
    """
    by_date = {}
    for day in days:
        by_date[day["date"]] = day

    start = datetime.date.fromisoformat(today)
    count = 0
    while True:
        date = (start + datetime.timedelta(days=count)).isoformat()
        day = by_date.get(date)
        if day is None or effective_status(day) != "approved":
            return count
        count += 1


def runway_state(remaining):
    """What a build should do about that number."""
    if remaining <= 0:
        return "dry"
    if remaining < RUNWAY_FAIL:
        return "fail"
    if remaining < RUNWAY_WARN:
        return "warn"
    return "fine"


# ---------------------------------------------------------------------------
# Reading and writing the two files.


def load_schedule(path=DEFAULT_SCHEDULE):
    if not os.path.exists(path):
        return {"days": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_schedule(doc, path=DEFAULT_SCHEDULE):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def load_rejections(path=DEFAULT_REJECTIONS):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["rejections"]


def save_rejections(rejections, path=DEFAULT_REJECTIONS):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rejections": rejections}, fh, indent=2)
        fh.write("\n")


def report(doc, rejections, today):
    """The lines a build prints about the state of the queue."""
    days = doc.get("days", [])
    remaining = runway(days, today)
    state = runway_state(remaining)
    counts = {}
    for day in days:
        if day["date"] >= today:
            status = effective_status(day)
            counts[status] = counts.get(status, 0) + 1

    lines = [
        f"{remaining} approved days remaining from {today} ({state})",
        "  " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        if counts else "  nothing scheduled from today",
    ]
    # Name the days that claim approval but no longer hold it, since that is
    # invisible in the file itself.
    broken = [d["date"] for d in days
              if d.get("status") == "approved" and effective_status(d) != "approved"]
    if broken:
        lines.append(
            f"  {len(broken)} day(s) lost their approval because the content "
            f"changed: {', '.join(broken[:5])}")
    if rejections:
        lines.append(f"  {len(rejections)} clip(s) rejected and never re-offered")
    if state == "dry":
        lines.append("  nothing approved. The endpoint serves the reserved fallback.")
    elif state == "fail":
        lines.append(f"  under {RUNWAY_FAIL} days. A build should fail here.")
    elif state == "warn":
        lines.append(f"  under {RUNWAY_WARN} days. Review more days soon.")
    return lines


def main():
    ap = argparse.ArgumentParser(
        description="Inspect the approval queue. Reviewing happens in tools/review.py.")
    ap.add_argument("--schedule", default=DEFAULT_SCHEDULE)
    ap.add_argument("--rejections", default=DEFAULT_REJECTIONS)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when the runway is under "
                         f"{RUNWAY_FAIL} days, for use in a build")
    args = ap.parse_args()

    today = args.today or datetime.date.today().isoformat()
    doc = load_schedule(args.schedule)
    rejections = load_rejections(args.rejections)

    for line in report(doc, rejections, today):
        sys.stderr.write(line + "\n")

    if args.strict:
        state = runway_state(runway(doc.get("days", []), today))
        return 1 if state in ("fail", "dry") else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
