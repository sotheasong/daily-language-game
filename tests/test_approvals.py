"""No session goes live without a human having played it.

The gate is a signature over content, not a flag on a date. A boolean
`approved` column would let a day approved three weeks ago keep reading as
approved after its clip was swapped underneath it, which is the one failure
that would put an unheard clip in front of players.

Four things here:

  1. verification, which is what makes approval mean anything
  2. approving and rejecting
  3. rejections being sticky, so the generator stops re-offering a bad clip
  4. the runway alarm, which is what actually bites when you are busy
"""

import unittest

from pipeline import approvals, schedule


def day(date="2026-12-01", clip="c1", status="draft"):
    d = {"date": date, "status": status,
         "rounds": [{"name": f"Entry{i}", "tier": "1", "clip": f"{clip}-{i}"}
                    for i in range(5)]}
    d["content_hash"] = schedule.content_hash(d)
    return d


class VerifyingADay(unittest.TestCase):
    """An approval holds only while the content it signed is unchanged."""

    def test_a_draft_is_a_draft(self):
        self.assertEqual(approvals.effective_status(day()), "draft")

    def test_an_untouched_approval_holds(self):
        approved = approvals.approve(day(), "sothea", "2026-11-20T14:02:00Z")
        self.assertEqual(approvals.effective_status(approved), "approved")

    def test_swapping_a_clip_silently_returns_the_day_to_draft(self):
        # The whole reason approval is a hash and not a flag.
        approved = approvals.approve(day(), "sothea", "2026-11-20T14:02:00Z")
        approved["rounds"][2]["clip"] = "something-else"
        self.assertEqual(approvals.effective_status(approved), "draft")

    def test_reordering_the_rounds_returns_the_day_to_draft(self):
        # The ramp is the shape of the session, so order is content.
        approved = approvals.approve(day(), "sothea", "2026-11-20T14:02:00Z")
        approved["rounds"].reverse()
        self.assertEqual(approvals.effective_status(approved), "draft")

    def test_a_day_claiming_approval_with_no_hash_is_not_approved(self):
        # Hand-edited YAML, or a day from before the hash existed.
        forged = dict(day(), status="approved")
        del forged["content_hash"]
        self.assertEqual(approvals.effective_status(forged), "draft")

    def test_a_day_claiming_approval_with_a_wrong_hash_is_not_approved(self):
        forged = dict(day(), status="approved", content_hash="0" * 16)
        self.assertEqual(approvals.effective_status(forged), "draft")


class ApprovingAndRejecting(unittest.TestCase):

    def test_approving_records_who_and_when(self):
        got = approvals.approve(day(), "sothea", "2026-11-20T14:02:00Z")
        self.assertEqual(got["status"], "approved")
        self.assertEqual(got["approved_by"], "sothea")
        self.assertEqual(got["approved_at"], "2026-11-20T14:02:00Z")

    def test_approving_rewrites_the_hash_to_what_was_seen(self):
        # Approve what is in front of the reviewer, not what the generator
        # first proposed: a day whose clip was replaced after generation is
        # approved as it now stands.
        d = day()
        d["rounds"][0]["clip"] = "replaced"
        got = approvals.approve(d, "sothea", "2026-11-20T14:02:00Z")
        self.assertEqual(approvals.effective_status(got), "approved")

    def test_approving_does_not_mutate_the_day_it_was_given(self):
        d = day()
        approvals.approve(d, "sothea", "2026-11-20T14:02:00Z")
        self.assertEqual(d["status"], "draft")

    def test_rejecting_a_day_records_the_reason(self):
        got = approvals.reject(day(), "sothea", "2026-11-20T14:02:00Z",
                               "round 3 clip has a place name in it")
        self.assertEqual(got["status"], "rejected")
        self.assertIn("place name", got["reason"])

    def test_a_rejected_day_is_not_approved(self):
        got = approvals.reject(day(), "sothea", "2026-11-20T14:02:00Z", "no")
        self.assertEqual(approvals.effective_status(got), "rejected")


class RejectionsAreSticky(unittest.TestCase):
    """A rejected clip must never be proposed again.

    Rejections are recorded against the clip hash rather than the day, because
    the clip is the thing that was wrong and the generator will otherwise
    offer it on a different date tomorrow.
    """

    def test_a_rejected_clip_is_remembered(self):
        out = approvals.reject_clip([], "7c21", "says Algerie at 0:06",
                                    "2026-11-20")
        self.assertEqual(approvals.rejected_clips(out), {"7c21"})

    def test_rejecting_the_same_clip_twice_does_not_duplicate_it(self):
        out = approvals.reject_clip([], "7c21", "first reason", "2026-11-20")
        out = approvals.reject_clip(out, "7c21", "second reason", "2026-11-21")
        self.assertEqual(len(out), 1)

    def test_the_latest_reason_wins(self):
        out = approvals.reject_clip([], "7c21", "first reason", "2026-11-20")
        out = approvals.reject_clip(out, "7c21", "sharper reason", "2026-11-21")
        self.assertEqual(out[0]["reason"], "sharper reason")

    def test_a_reason_is_required(self):
        # The reasons double as a specification of what a bad clip looks like,
        # which is worth reading before the next sourcing pass. A blank one
        # teaches nothing.
        with self.assertRaises(ValueError):
            approvals.reject_clip([], "7c21", "   ", "2026-11-20")

    def test_the_generator_will_not_offer_a_rejected_clip(self):
        pool = [{"name": "One0", "tier": "1", "kind": "language"}]
        clips = {"One0": ["bad", "good"]}
        session = schedule.pick_session(
            pool, clips, recent_clips=approvals.rejected_clips(
                approvals.reject_clip([], "bad", "clipped mid word", "2026-11-20")),
            seed=1, rounds=1)
        self.assertEqual(session[0]["clip"], "good")


class TheRunwayAlarm(unittest.TestCase):
    """Approved days remaining, counted forward from today.

    Consecutive on purpose. If tomorrow is a draft it does not matter that the
    nine days after it are approved, because the queue breaks tomorrow.
    """

    def _days(self, statuses, start="2026-12-01"):
        import datetime
        first = datetime.date.fromisoformat(start)
        out = []
        for offset, status in enumerate(statuses):
            d = day((first + datetime.timedelta(days=offset)).isoformat())
            out.append(approvals.approve(d, "s", "t") if status == "a" else d)
        return out

    def test_an_empty_schedule_has_no_runway(self):
        self.assertEqual(approvals.runway([], "2026-12-01"), 0)

    def test_consecutive_approved_days_count(self):
        self.assertEqual(
            approvals.runway(self._days("aaa"), "2026-12-01"), 3)

    def test_counting_stops_at_the_first_unapproved_day(self):
        self.assertEqual(
            approvals.runway(self._days("aaddaaaa"), "2026-12-01"), 2)

    def test_days_before_today_do_not_count(self):
        days = self._days("aaaa", start="2026-11-01")
        self.assertEqual(approvals.runway(days, "2026-12-01"), 0)

    def test_a_gap_before_today_is_ignored(self):
        # Yesterday being a draft is history, not runway.
        days = self._days("daaa", start="2026-11-30")
        self.assertEqual(approvals.runway(days, "2026-12-01"), 3)

    def test_a_missing_date_breaks_the_run(self):
        # A hole in the dates is a day with nothing to serve.
        days = self._days("aa") + self._days("aa", start="2026-12-05")
        self.assertEqual(approvals.runway(days, "2026-12-01"), 2)

    def test_an_approval_invalidated_by_a_clip_swap_does_not_count(self):
        days = self._days("aaa")
        days[1]["rounds"][0]["clip"] = "swapped"
        self.assertEqual(approvals.runway(days, "2026-12-01"), 1)


class WhatTheRunwayMeans(unittest.TestCase):
    """The thresholds from tech-stack.md, which a build reads to decide."""

    def test_two_weeks_or_more_is_fine(self):
        self.assertEqual(approvals.runway_state(14), "fine")
        self.assertEqual(approvals.runway_state(40), "fine")

    def test_under_two_weeks_warns(self):
        self.assertEqual(approvals.runway_state(13), "warn")
        self.assertEqual(approvals.runway_state(7), "warn")

    def test_under_one_week_fails_the_build(self):
        self.assertEqual(approvals.runway_state(6), "fail")
        self.assertEqual(approvals.runway_state(1), "fail")

    def test_nothing_approved_serves_the_fallback(self):
        self.assertEqual(approvals.runway_state(0), "dry")


if __name__ == "__main__":
    unittest.main()
