"""Check candidate integrity, native evidence and failed-job retry boundaries."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ci_artifacts as ci
import test_verify_build as fixtures

ENV = {
    "GITHUB_REPOSITORY": "dayflower/webcappi",
    "GITHUB_RUN_ID": "100",
    "GITHUB_RUN_ATTEMPT": "1",
}
VERSION = "v0.2.0-rc.1"
REVISION = "a" * 40


class CandidateArtifactsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        builder = fixtures.BuildVerificationTest()
        builder.artifacts = self.artifacts
        builder.fixture(
            labels={
                "org.opencontainers.image.source": "https://github.com/dayflower/webcappi",
                "org.opencontainers.image.version": VERSION,
                "org.opencontainers.image.revision": REVISION,
                "io.github.dayflower.webcappi.modified": "false",
            }
        )
        ci.write(
            self.artifacts / "build-verification.json",
            ci.VERIFY["verify"](self.artifacts),
        )
        self.environment = patch.dict(os.environ, ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.candidate = ci.record(self.artifacts, VERSION, REVISION)
        self.args = argparse.Namespace(
            artifacts=self.artifacts,
            version=VERSION,
            revision=REVISION,
            image="webcappi:ci-amd64",
            arch="amd64",
            output=self.root / "output.json",
            metadata=self.artifacts / "candidate.json",
            receipts=self.root / "receipts",
            bundle_id=123,
            loaded=self.root / "loaded.json",
            evidence=self.root / "evidence",
            qualification=self.root / "qualification.json",
            run_id=100,
            run_attempt=1,
        )

    def receipt(self, arch="amd64", attempt=1):
        return {
            "repository": ENV["GITHUB_REPOSITORY"],
            "run_id": 100,
            "run_attempt": attempt,
            "arch": arch,
            "image": "webcappi:ci-" + arch,
            "result": "passed",
            "candidate_sha256": ci.checksum(self.args.metadata),
            "config_digest": self.candidate["configs"][arch],
            "loaded_id": self.candidate["configs"][arch],
        }

    def qualify(self):
        for arch in ci.ARCHES:
            ci.write(self.args.receipts / arch / "runtime.json", self.receipt(arch))
        ci.qualify(self.args)
        return ci.read(self.args.output)

    def test_candidate_checksums_labels_and_report_are_verified(self):
        self.assertEqual(
            ci.verify_files(self.artifacts, VERSION, REVISION), self.candidate
        )
        for version, revision in (("v0.3.0", REVISION), (VERSION, "b" * 40)):
            with self.assertRaisesRegex(ValueError, "version/revision"):
                ci.verify_files(self.artifacts, version, revision)
        for name in ci.FILES:
            path = self.artifacts / name
            original = path.read_bytes()
            path.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum differs"):
                ci.verify_files(self.artifacts, VERSION, REVISION)
            path.write_bytes(original)

    def test_rewriting_checksums_cannot_replace_the_qualified_candidate(self):
        qualification = self.qualify()
        ci.write(self.args.qualification, qualification)
        ci.verify(self.args)
        value = deepcopy(self.candidate)
        value["image_digest"] = "sha256:" + "b" * 64
        ci.write(self.args.metadata, value)
        with self.assertRaisesRegex(ValueError, "qualification/artifact identity"):
            ci.verify(self.args)

    def test_load_verifies_the_config_and_allows_same_run_retry(self):
        with (
            patch.dict(os.environ, GITHUB_RUN_ATTEMPT="2"),
            patch.object(ci.subprocess, "run") as run,
            patch.object(
                ci, "verify_loaded", return_value=self.candidate["configs"]["amd64"]
            ),
        ):
            ci.load(self.args)
        self.assertIn("docker-daemon:" + self.args.image, run.call_args.args[0])
        self.assertEqual(ci.read(self.args.output)["run_attempt"], 2)
        with (
            patch.object(ci.subprocess, "run"),
            patch.object(
                ci,
                "verify_loaded",
                side_effect=ValueError("Loaded Docker image differs"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Loaded Docker image differs"):
                ci.load(self.args)
        with patch.dict(os.environ, GITHUB_RUN_ID="101"):
            with self.assertRaisesRegex(ValueError, "workflow run differs"):
                ci.load(self.args)

    def test_loaded_manifest_ids_require_identical_config_and_rootfs(self):
        actual = {
            "Id": "sha256:manifest",
            "Os": "linux",
            "Architecture": "amd64",
            "RootFS": {"Layers": []},
            "Config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/dayflower/webcappi",
                    "org.opencontainers.image.version": VERSION,
                    "org.opencontainers.image.revision": REVISION,
                    "io.github.dayflower.webcappi.modified": "false",
                }
            },
        }

        def check(value):
            with patch.object(
                ci.subprocess, "check_output", return_value=json.dumps([value])
            ):
                return ci.verify_loaded(
                    self.artifacts,
                    "amd64",
                    self.args.image,
                    self.candidate["configs"]["amd64"],
                )

        self.assertEqual(check(actual), "sha256:manifest")
        classic = dict(actual, Id=self.candidate["configs"]["amd64"])
        classic["Config"] = dict(actual["Config"], AttachStdin=False)
        self.assertEqual(check(classic), self.candidate["configs"]["amd64"])
        for field, value in (
            ("RootFS", {"Layers": ["sha256:other"]}),
            ("Config", {"Cmd": ["other"]}),
            ("Architecture", "arm64"),
        ):
            with self.assertRaisesRegex(ValueError, "config/rootfs"):
                check(dict(actual, **{field: value}))

    def evidence(self):
        value = self.receipt()
        ci.write(self.args.loaded, value)
        for suite in ("enabled", "disabled", "network"):
            root = self.args.evidence / (
                "helm-network" if suite == "network" else "helm-" + suite
            )
            ci.write(
                root / "result.json",
                {
                    "result": "passed",
                    "platform": "linux/amd64",
                    "source_image": value["image"],
                    "images": {"source_id": value["config_digest"]},
                    "default_image": {"passed": True, "image": value["image"]},
                    "sandbox_mode": "disabled" if suite == "disabled" else "enabled",
                    "network": {"result": "passed"},
                },
            )
            ci.write(
                root / "chromium-sandbox.json", {"no_sandbox": suite == "disabled"}
            )
        for case in ("setup-failure", "sigterm"):
            ci.write(
                self.args.evidence / "helm-cleanup" / (case + ".json"),
                {"result": "passed", "node_removed": True},
            )
        return value

    def test_runtime_requires_all_suites_exact_source_and_cleanup(self):
        value = self.evidence()
        with patch.object(ci, "loaded_id", return_value=value["config_digest"]):
            ci.runtime(self.args)
            self.assertEqual(ci.read(self.args.output)["result"], "passed")
            path = self.args.evidence / "helm-disabled/result.json"
            data = ci.read(path)
            data["images"]["source_id"] = "another image"
            ci.write(path, data)
            with self.assertRaisesRegex(ValueError, "Incomplete candidate runtime"):
                ci.runtime(self.args)
            self.assertFalse(self.args.output.exists())
            self.evidence()
            path = self.args.evidence / "helm-cleanup/sigterm.json"
            ci.write(path, {"result": "passed", "node_removed": False})
            with self.assertRaisesRegex(ValueError, "cleanup qualification"):
                ci.runtime(self.args)

    def test_qualification_requires_both_native_platforms_and_exact_attempt(self):
        record = self.qualify()
        ci.validate_qualification(record, VERSION, REVISION)
        for mutate in (
            lambda r: r["runtime"].pop("arm64"),
            lambda r: r["runtime"]["arm64"].update(config_digest="wrong"),
            lambda r: r["runtime"]["arm64"].update(run_id=101),
            lambda r: r["runtime"]["arm64"].update(run_attempt=2),
            lambda r: r["candidate"].update(modified="true"),
            lambda r: r.update(result="failed"),
        ):
            changed = deepcopy(record)
            mutate(changed)
            with self.assertRaises(ValueError):
                ci.validate_qualification(changed, VERSION, REVISION)

    def test_failed_job_retry_reuses_successful_native_receipts(self):
        self.qualify()
        ci.write(
            self.args.receipts / "retry/arm64/runtime.json", self.receipt("arm64", 2)
        )
        with patch.dict(os.environ, GITHUB_RUN_ATTEMPT="2"):
            ci.qualify(self.args)
        record = ci.read(self.args.output)
        self.assertEqual(record["run_attempt"], 2)
        self.assertEqual(record["candidate"]["run_attempt"], 1)
        self.assertEqual(record["runtime"]["amd64"]["run_attempt"], 1)
        self.assertEqual(record["runtime"]["arm64"]["run_attempt"], 2)


if __name__ == "__main__":
    unittest.main()
