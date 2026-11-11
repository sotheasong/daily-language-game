"""Reading the Mozilla Data Collective's refusals correctly.

Only one of these means "accept the terms". Guessing wrong is expensive in
the one direction that matters: it sends a person to a browser to accept
terms on datasets they have already accepted, and hides the real cause.

This happened twice. First a hand-rolled urllib POST drew a Cloudflare 403,
code 1010, on browser signature, and all eight locales were reported as
needing terms. Then, routed through the SDK, a rate limit was reported the
same way for six more.
"""

import sys
import types
import unittest

# fetch_cv lives in tools/, which is not a package.
sys.path.insert(0, "tools")
import fetch_cv  # noqa: E402


class ClassifyingARefusal(unittest.TestCase):

    def test_no_error_means_ready(self):
        self.assertEqual(fetch_cv.classify_refusal(None), "ready")

    def test_a_permission_error_means_the_terms(self):
        # The one case that a browser visit actually fixes.
        self.assertEqual(
            fetch_cv.classify_refusal(
                PermissionError("Terms must be accepted before downloading")),
            "terms")

    def test_a_rate_limit_is_not_a_terms_problem(self):
        err = types.new_class("RateLimitError", (Exception,))(
            "Rate limit exceeded. Please try again later.")
        self.assertEqual(fetch_cv.classify_refusal(err), "unknown")

    def test_a_cloudflare_block_is_not_a_terms_problem(self):
        self.assertEqual(
            fetch_cv.classify_refusal(RuntimeError("403 error code: 1010")),
            "unknown")

    def test_an_unrecognised_error_is_unknown_rather_than_terms(self):
        # Defaulting to "terms" is what produced both wrong reports.
        self.assertEqual(fetch_cv.classify_refusal(OSError("connection reset")),
                         "unknown")


if __name__ == "__main__":
    unittest.main()
