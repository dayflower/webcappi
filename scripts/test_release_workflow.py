"""Exercise publication routing with skipped, failed and cancelled ancestors."""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = ("chart_archive", "publish_chart", "chart_public", "github_release")


class ReleaseRoutingTest(unittest.TestCase):
    def setUp(self):
        self.jobs = yaml.safe_load(
            (ROOT / ".github/workflows/release.yml").read_text()
        )["jobs"]
        self.results = dict.fromkeys(self.jobs, "success")

    def ancestors(self, job):
        needs = self.jobs[job].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        return set(needs).union(*(self.ancestors(name) for name in needs))

    def runs(self, job, cancelled=False):
        # Evaluate only the boolean subset used by these gates. GitHub's default
        # success() includes skipped ancestors, not just the direct needs jobs.
        ancestors = [self.results[name] for name in self.ancestors(job)]
        expression = self.jobs[job].get("if", "success()")
        expression = expression.removeprefix("${{").removesuffix("}}").strip()
        functions = {
            "cancelled": cancelled,
            "failure": "failure" in ancestors,
            "success": not cancelled and all(item == "success" for item in ancestors),
        }
        if not any(name + "()" in expression for name in functions):
            expression = "success() && (" + expression + ")"
        for name, value in functions.items():
            expression = expression.replace(name + "()", str(value))
        expression = re.sub(
            r"needs\.(\w+)\.result",
            lambda match: repr(self.results[match[1]]),
            expression,
        )
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression).strip()
        return eval(expression, {"__builtins__": {}})

    def test_reuse_and_published_recovery_continue_after_skipped_rebuild(self):
        self.results["candidate"] = "skipped"
        for job in PUBLICATION:
            with self.subTest(job=job):
                self.assertTrue(self.runs(job))

    def test_fully_qualified_fallback_can_publish(self):
        for job in PUBLICATION:
            with self.subTest(job=job):
                self.assertTrue(self.runs(job))

    def test_failed_candidate_or_publication_prevents_later_publication(self):
        for failure in ("candidate", "publish", "chart_archive", "publish_chart"):
            self.results = dict.fromkeys(self.jobs, "success")
            self.results[failure] = "failure"
            for job in PUBLICATION:
                if failure in self.ancestors(job):
                    with self.subTest(failure=failure, job=job):
                        self.assertFalse(self.runs(job))

    def test_skipped_required_job_does_not_authorize_publication(self):
        for job in PUBLICATION:
            for needed in self.jobs[job]["needs"]:
                self.results = dict.fromkeys(self.jobs, "success")
                self.results[needed] = "skipped"
                with self.subTest(job=job, needed=needed):
                    self.assertFalse(self.runs(job))

    def test_cancellation_prevents_publication(self):
        for job in PUBLICATION:
            with self.subTest(job=job):
                self.assertFalse(self.runs(job, cancelled=True))


if __name__ == "__main__":
    unittest.main()
