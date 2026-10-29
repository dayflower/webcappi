"""Reject failed, stale, forked or mismatched CI artifacts during release reuse."""

import argparse
from copy import deepcopy
import io
import os
import unittest
from unittest.mock import patch
import zipfile

import ci_reuse as reuse

REPO = "dayflower/webcappi"
VERSION = "v0.2.0"
REVISION = "a" * 40


class CIReuseTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, GITHUB_REPOSITORY=REPO)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.args = argparse.Namespace(
            version=VERSION,
            revision=REVISION,
            discovery_timeout=120,
            wait_timeout=3600,
            poll_interval=20,
            published_only=False,
        )
        self.run = {
            "id": 100,
            "run_attempt": 2,
            "event": "push",
            "head_branch": "main",
            "head_sha": REVISION,
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
            "status": "completed",
            "conclusion": "success",
        }
        candidate = {
            "schema_version": 1,
            "repository": REPO,
            "run_id": 100,
            "run_attempt": 1,
            "version": VERSION,
            "revision": REVISION,
            "modified": "false",
            "configs": {arch: "sha256:" + arch for arch in ("amd64", "arm64")},
            "image_digest": "sha256:" + "b" * 64,
        }
        self.proof = {
            "schema_version": 1,
            "repository": REPO,
            "run_id": 100,
            "run_attempt": 2,
            "result": "passed",
            "bundle_id": 3,
            "candidate": candidate,
            "candidate_sha256": "c" * 64,
            "runtime": {
                arch: {
                    "arch": arch,
                    "repository": REPO,
                    "run_id": 100,
                    "run_attempt": 2 if arch == "arm64" else 1,
                    "result": "passed",
                    "candidate_sha256": "c" * 64,
                    "config_digest": candidate["configs"][arch],
                }
                for arch in ("amd64", "arm64")
            },
        }
        self.artifacts = [
            {
                "id": 2,
                "name": "release-qualification-2",
                "size_in_bytes": 500,
                "expired": False,
            },
            {
                "id": 3,
                "name": "release-candidate-1",
                "size_in_bytes": 500,
                "expired": False,
            },
        ]

    def api(self, path, **kwargs):
        if "/workflows/ci.yml/runs?" in path:
            self.assertIn("event=push", path)
            self.assertIn("branch=main", path)
            self.assertIn("head_sha=" + REVISION, path)
            return {"workflow_runs": [self.run]}
        if "/runs/100/artifacts?" in path:
            return {"artifacts": self.artifacts}
        if path.endswith("/runs/100/attempts/2"):
            return self.run
        self.fail("unexpected API call: " + path)

    def resolve(self):
        with (
            patch.object(reuse, "inspect_release", return_value=None),
            patch.object(reuse, "api", side_effect=self.api),
            patch.object(reuse, "qualification", return_value=self.proof),
        ):
            return reuse.resolve(self.args)

    def test_reuses_exact_successful_main_attempt_and_original_build_bundle(self):
        result = self.resolve()
        self.assertEqual(
            result,
            {
                "mode": "reuse",
                "run_id": 100,
                "run_attempt": 2,
                "bundle_id": 3,
                "qualification_id": 2,
                "image_digest": self.proof["candidate"]["image_digest"],
            },
        )

    def test_failed_main_never_falls_back(self):
        for conclusion in (
            "failure",
            "timed_out",
            "action_required",
            "neutral",
            "skipped",
        ):
            self.run["conclusion"] = conclusion
            with (
                self.subTest(conclusion=conclusion),
                self.assertRaisesRegex(ValueError, "main CI failed"),
            ):
                self.resolve()
        self.run["conclusion"] = "cancelled"
        self.assertEqual(self.resolve()["mode"], "fallback")

    def test_pr_forks_other_shas_and_workflows_are_rejected(self):
        original = deepcopy(self.run)
        for mutate in (
            lambda r: r.update(event="pull_request"),
            lambda r: r.update(event="workflow_dispatch"),
            lambda r: r.update(head_branch="topic"),
            lambda r: r.update(head_sha="d" * 40),
            lambda r: r.update(path=".github/workflows/other.yml"),
            lambda r: r["head_repository"].update(full_name="fork/webcappi"),
        ):
            self.run = deepcopy(original)
            mutate(self.run)
            with self.assertRaisesRegex(ValueError, "matching repository main push"):
                self.resolve()

    def test_missing_and_expired_artifacts_use_full_fallback(self):
        original = deepcopy(self.artifacts)
        for artifacts in (
            [],
            original[:1],
            [dict(original[0], expired=True), original[1]],
            [original[0], dict(original[1], expired=True)],
        ):
            self.artifacts = artifacts
            self.assertEqual(self.resolve()["mode"], "fallback")

    def test_mismatched_qualification_is_not_treated_as_missing(self):
        original = deepcopy(self.proof)
        for mutate in (
            lambda r: r.update(run_attempt=1),
            lambda r: r.update(repository="fork/webcappi"),
            lambda r: r["candidate"].update(version="v0.3.0"),
            lambda r: r["runtime"].pop("arm64"),
            lambda r: r["runtime"]["arm64"].update(candidate_sha256="wrong"),
        ):
            self.proof = deepcopy(original)
            mutate(self.proof)
            with self.assertRaises(ValueError):
                self.resolve()

    def test_auth_and_transport_errors_never_fall_back(self):
        for status in (401, 403, 500, None):
            with (
                patch.object(reuse, "api", side_effect=self.api),
                patch.object(
                    reuse, "qualification", side_effect=reuse.APIError("failed", status)
                ),
            ):
                with self.assertRaises(reuse.APIError):
                    reuse.select(self.run, self.args)
        for status in (404, 410):
            with (
                patch.object(reuse, "api", side_effect=self.api),
                patch.object(
                    reuse, "qualification", side_effect=reuse.APIError("gone", status)
                ),
            ):
                self.assertEqual(reuse.select(self.run, self.args)["mode"], "fallback")

    def test_waits_for_same_commit_ci_and_has_a_deadline(self):
        original_api = self.api
        calls = 0

        def api(path, **kwargs):
            nonlocal calls
            if "/workflows/ci.yml/runs?" in path:
                calls += 1
                if calls == 1:
                    return {
                        "workflow_runs": [
                            dict(self.run, status="in_progress", conclusion=None)
                        ]
                    }
            return original_api(path, **kwargs)

        with (
            patch.object(reuse, "inspect_release", return_value=None),
            patch.object(reuse, "api", side_effect=api),
            patch.object(reuse, "qualification", return_value=self.proof),
            patch.object(reuse.time, "sleep") as sleep,
        ):
            self.assertEqual(reuse.resolve(self.args)["mode"], "reuse")
            sleep.assert_called_once()
        self.run.update(status="in_progress", conclusion=None)
        self.args.wait_timeout = 0
        with self.assertRaises(TimeoutError):
            self.resolve()

    def test_allows_discovery_grace_then_falls_back_for_absent_main_ci(self):
        with (
            patch.object(reuse, "inspect_release", return_value=None),
            patch.object(reuse, "api", return_value={"workflow_runs": []}),
            patch.object(reuse.time, "monotonic", side_effect=[0, 0, 121]),
            patch.object(reuse.time, "sleep") as sleep,
        ):
            self.assertEqual(reuse.resolve(self.args)["mode"], "fallback")
            sleep.assert_called_once()

    def test_published_image_recovery_does_not_depend_on_ci_retention(self):
        self.args.published_only = True
        with (
            patch.object(
                reuse, "inspect_release", return_value={"digest": "sha256:published"}
            ),
            patch.object(reuse, "api") as api,
        ):
            self.assertEqual(
                reuse.resolve(self.args),
                {"mode": "published", "image_digest": "sha256:published"},
            )
            api.assert_not_called()

    def test_recovery_requires_existing_image_without_ci_or_fallback(self):
        self.args.published_only = True
        with (
            patch.object(reuse, "inspect_release", return_value=None),
            patch.object(reuse, "api") as api,
        ):
            with self.assertRaisesRegex(ValueError, "already published"):
                reuse.resolve(self.args)
            api.assert_not_called()

    def test_recovery_does_not_ignore_image_identity_or_transport_errors(self):
        self.args.published_only = True
        for error in (ValueError("revision differs"), RuntimeError("HTTP 403")):
            with (
                patch.object(reuse, "inspect_release", side_effect=error),
                patch.object(reuse, "api") as api,
            ):
                with self.assertRaises(type(error)):
                    reuse.resolve(self.args)
                api.assert_not_called()

    def test_qualification_zip_cannot_extract_extra_files(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("qualification.json", "{}")
            archive.writestr("../other", "unsafe")
        with patch.object(reuse, "api", return_value=data.getvalue()):
            with self.assertRaisesRegex(ValueError, "archive contents"):
                reuse.qualification(REPO, self.artifacts[0])

    def test_rechecks_the_pinned_successful_attempt(self):
        with patch.object(reuse, "api", return_value=self.run):
            reuse.verify_attempt(REPO, REVISION, 100, 2)
            with self.assertRaisesRegex(ValueError, "attempt identity differs"):
                reuse.verify_attempt(REPO, REVISION, 100, 1)


if __name__ == "__main__":
    unittest.main()
