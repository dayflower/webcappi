"""Exercise chart distribution, retries and failures without publishing packages."""

import argparse
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import yaml

import release_charts as charts
from github_release import reuse_chart_record

REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64
TAG = "v0.1.2-rc.1"


def archive(path, files, stamp=0, mode=0o644):
    with tarfile.open(path, "w:gz") as contents:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mtime, entry.uid = len(data), stamp, stamp
            entry.mode = mode
            contents.addfile(entry, io.BytesIO(data))


class ChartReleaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "chart"
        shutil.copytree(charts.CHART, self.source)
        charts.prepare_version(self.source, TAG)
        values = yaml.safe_load((self.source / "values.yaml").read_text())
        values["image"].update(repository=charts.IMAGE, tag=TAG[1:], digest=DIGEST)
        (self.source / "values.yaml").write_text(yaml.safe_dump(values))
        self.files = {
            "webcappi/" + str(p.relative_to(self.source)): p.read_bytes()
            for p in self.source.rglob("*")
            if p.is_file()
        }
        self.candidate = self.root / "candidate.tgz"
        archive(self.candidate, self.files)
        self.downloaded = self.root / "published.tgz"
        archive(self.downloaded, self.files, stamp=1000)
        self.args = argparse.Namespace(
            archive=self.candidate,
            tag=TAG,
            revision=REVISION,
            image_digest=DIGEST,
            helm="helm",
            output=self.root / "output",
            public=False,
            validation_only=False,
        )

    def test_metadata_and_prepare_version(self):
        self.assertEqual(charts.metadata(self.source, TAG), TAG[1:])
        before = (self.source / "Chart.yaml").read_bytes()
        charts.prepare_version(self.source, "v1.2.3", dry_run=True)
        self.assertEqual((self.source / "Chart.yaml").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "appVersion"):
            charts.metadata(self.source, "v1.2.3")
        for invalid in ("v1.2.3+build", "1.2.3", "v1.2.3-01"):
            with self.assertRaises(ValueError):
                charts.prepare_version(self.source, invalid)

    def test_package_changes_only_temporary_copy(self):
        self.args.source = self.source
        self.args.source_revision = False
        before = (self.source / "values.yaml").read_bytes()

        def check(source, helm, output):
            values = yaml.safe_load((source / "values.yaml").read_text())["image"]
            self.assertEqual(values["digest"], DIGEST)
            self.assertNotEqual(source, self.source)
            output.mkdir()
            shutil.copyfile(self.candidate, output / f"webcappi-{TAG[1:]}.tgz")

        with (
            patch.object(charts, "inspect_release", return_value={"digest": DIGEST}),
            patch.object(charts, "static_check", side_effect=check),
        ):
            charts.package(self.args)
        self.assertEqual((self.source / "values.yaml").read_bytes(), before)

    def test_full_file_comparison_ignores_tar_metadata(self):
        charts.compare(self.candidate, self.downloaded)
        archive(self.downloaded, self.files, mode=0o755)
        with self.assertRaisesRegex(ValueError, "contents differ"):
            charts.compare(self.candidate, self.downloaded)
        for name in self.files:
            changed = dict(self.files)
            changed[name] += b"\n"
            archive(self.downloaded, changed)
            with self.assertRaisesRegex(ValueError, "contents differ"):
                charts.compare(self.candidate, self.downloaded)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            archive(self.downloaded, {"webcappi/../escape": b"data"})
            charts.archive_files(self.downloaded)

    def test_malformed_archive_never_falls_back(self):
        self.downloaded.write_bytes(b"bad archive")
        with self.assertRaises(tarfile.ReadError):
            with charts.chart_source(self.downloaded):
                self.fail("invalid archive accepted")
        with tarfile.open(self.downloaded, "w:gz") as contents:
            entry = tarfile.TarInfo("webcappi/link")
            entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/passwd"
            contents.addfile(entry)
        with self.assertRaisesRegex(ValueError, "regular files"):
            charts.archive_files(self.downloaded)

    def test_first_publish_and_identical_retry(self):
        raw = b'{"layers":[]}'
        for exists in (False, True):
            with (
                self.subTest(exists=exists),
                patch.dict(os.environ, GHCR_USER="user", GHCR_TOKEN="secret"),
                patch.object(
                    charts,
                    "manifest",
                    side_effect=[raw, raw] if exists else [None, raw, raw],
                ),
                patch.object(charts, "pull", return_value=self.downloaded),
                patch.object(charts, "run") as run,
            ):
                result = charts.publish(self.args)
                self.assertEqual(
                    result["manifest_digest"], "sha256:" + charts.sha256(raw)
                )
                writes = [c for c in run.call_args_list if c.args[1] == "push"]
                self.assertEqual(len(writes), 0 if exists else 1)
                env = run.call_args_list[0].kwargs["env"]
                self.assertFalse(Path(env["HELM_REGISTRY_CONFIG"]).parent.exists())

    def test_conflicts_auth_and_partial_failures_stop_publication(self):
        for failure in (RuntimeError("denied"), OSError("network unavailable")):
            with (
                self.subTest(failure=failure),
                patch.dict(os.environ, GHCR_USER="user", GHCR_TOKEN="secret"),
                patch.object(charts, "manifest", side_effect=failure),
                patch.object(charts, "run") as run,
            ):
                with self.assertRaises(type(failure)):
                    charts.publish(self.args)
                self.assertEqual(run.call_count, 1)  # login only
        changed = dict(self.files)
        changed["webcappi/README.md"] += b"changed"
        archive(self.downloaded, changed)
        with (
            patch.dict(os.environ, GHCR_USER="user", GHCR_TOKEN="secret"),
            patch.object(charts, "manifest", return_value=b"manifest"),
            patch.object(charts, "pull", return_value=self.downloaded),
            patch.object(charts, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "overwrite"):
                charts.publish(self.args)
            self.assertEqual(run.call_count, 1)
        with (
            patch.dict(os.environ, GHCR_USER="user", GHCR_TOKEN="secret"),
            patch.object(
                charts,
                "manifest",
                side_effect=[None, RuntimeError("verification failed")],
            ),
            patch.object(charts, "run"),
        ):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                charts.publish(self.args)

    def test_only_structured_manifest_404_is_absence(self):
        for code, body, absent in (
            (404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', True),
            (401, b"{}", False),
            (403, b"{}", False),
            (500, b"{}", False),
            (404, b"{}", False),
            (404, b'{"errors":[{"code":"NAME_UNKNOWN"},{"code":"DENIED"}]}', False),
        ):
            error = urllib.error.HTTPError(
                "https://ghcr.io", code, "failed", {}, io.BytesIO(body)
            )
            with (
                self.subTest(code=code, absent=absent),
                patch.object(
                    charts.urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(b'{"token":"test"}'), error],
                ),
            ):
                if absent:
                    self.assertIsNone(
                        charts.manifest(TAG[1:], "user:token", missing_ok=True)
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {code}"):
                        charts.manifest(TAG[1:], "user:token", missing_ok=True)
        error = urllib.error.HTTPError("https://ghcr.io/token", 404, "failed", {}, None)
        with patch.object(charts.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                charts.manifest(TAG[1:], "user:token", missing_ok=True)

    def test_anonymous_credentials_are_isolated_and_removed_on_failure(self):
        with patch.dict(
            os.environ,
            HELM_REGISTRY_CONFIG="/private/config",
            HELM_KUBECONTEXT="production",
            DOCKER_CONFIG="/private/docker",
        ):
            with self.assertRaises(RuntimeError):
                with charts.helm_environment() as env:
                    root = Path(env["HELM_REGISTRY_CONFIG"]).parent
                    self.assertNotIn("HELM_KUBECONTEXT", env)
                    self.assertTrue(root.exists())
                    self.assertFalse(Path(env["DOCKER_CONFIG"]).exists())
                    raise RuntimeError("interrupted")
            self.assertFalse(root.exists())

    def test_real_sigterm_removes_temporary_registry_credentials(self):
        script = """
import os
from pathlib import Path
import signal
from release_charts import helm_environment, interrupted
signal.signal(signal.SIGTERM, interrupted)
with helm_environment() as env:
    registry = Path(env["HELM_REGISTRY_CONFIG"])
    registry.write_text("temporary fixture credentials")
    print(registry.parent, flush=True)
    os.kill(os.getpid(), signal.SIGTERM)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=charts.ROOT / "scripts",
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Interrupted by signal", result.stderr)
        self.assertFalse(Path(result.stdout.strip()).exists())

    def test_static_only_public_acceptance_cannot_create_proof(self):
        self.args.public = self.args.validation_only = True
        with (
            patch.object(charts, "manifest", return_value=b"{}"),
            patch.object(charts, "pull", return_value=self.downloaded),
            patch.object(charts, "static_check"),
            patch.object(charts, "run") as run,
        ):
            report = charts.acceptance(self.args)
        self.assertTrue(report["anonymous"])
        self.assertFalse((self.args.output / "acceptance.json").exists())
        run.assert_not_called()

    def test_failed_runtime_removes_stale_success_proof(self):
        self.args.output.mkdir()
        proof = self.args.output / "acceptance.json"
        proof.write_text("stale success")
        with (
            patch.object(charts, "static_check"),
            patch.object(charts.platform, "system", return_value="Linux"),
            patch.object(charts.platform, "machine", return_value="x86_64"),
            patch.object(
                charts, "run", side_effect=[None, RuntimeError("capture failed")]
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                charts.acceptance(self.args)
        self.assertFalse(proof.exists())

    def test_pull_checks_actual_oci_layer(self):
        def fetch(*args, **kwargs):
            shutil.copyfile(self.downloaded, self.root / f"webcappi-{TAG[1:]}.tgz")

        with patch.object(charts, "run", side_effect=fetch):
            raw = json.dumps(
                {
                    "layers": [
                        {
                            "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                            "digest": "sha256:"
                            + charts.sha256(self.downloaded.read_bytes()),
                        }
                    ]
                }
            ).encode()
            charts.pull("helm", TAG[1:], self.root, {}, raw)
            with self.assertRaisesRegex(ValueError, "OCI manifest layer"):
                charts.pull(
                    "helm", TAG[1:], self.root, {}, raw.replace(b"sha256:", b"sha512:")
                )

    def test_finalize_requires_both_native_architectures_modes_and_public_identity(
        self,
    ):
        record = self.acceptance_record()
        charts.validate_proof(record)
        self.args.evidence = self.root / "evidence"
        for stage, platforms in record["acceptance"].items():
            for arch, item in platforms.items():
                charts.write_json(
                    self.args.evidence / stage / arch / "acceptance.json", item
                )
        charts.write_json(
            self.args.evidence / "retry/public/amd64/acceptance.json",
            record["acceptance"]["public"]["amd64"],
        )
        charts.finalize(self.args)
        output = json.loads((self.args.output / "chart-distribution.json").read_text())
        self.assertEqual(output["schema_version"], charts.SCHEMA_VERSION)
        charts.validate_proof(output)
        changed = dict(
            record["acceptance"]["public"]["amd64"], manifest_digest="changed"
        )
        charts.write_json(
            self.args.evidence / "retry/public/amd64/acceptance.json", changed
        )
        with self.assertRaisesRegex(ValueError, "Registry identity differs"):
            charts.finalize(self.args)
        for mutate in (
            lambda r: r["acceptance"]["public"].pop("arm64"),
            lambda r: r["acceptance"]["archive"]["amd64"]["suites"].pop("disabled"),
            lambda r: r["acceptance"]["public"]["arm64"].update(anonymous=False),
            lambda r: r["acceptance"]["public"]["arm64"].update(
                manifest_digest="different"
            ),
            lambda r: r["acceptance"]["archive"]["amd64"]["suites"]["enabled"].update(
                policy=None
            ),
        ):
            broken = deepcopy(record)
            mutate(broken)
            with self.assertRaises((ValueError, KeyError)):
                charts.validate_proof(broken)

    def acceptance_record(self, schema=None):
        record = charts.identity(self.candidate, TAG, REVISION, DIGEST)
        record.update(
            manifest_digest="sha256:" + "c" * 64,
            archive_sha256="d" * 64,
            acceptance={"archive": {}, "public": {}},
        )
        if schema is not None:
            record["schema_version"] = schema
        for stage in record["acceptance"]:
            for arch in ("amd64", "arm64"):
                item = {
                    key: value for key, value in record.items() if key != "acceptance"
                }
                item.update(
                    stage=stage,
                    platform="linux/" + arch,
                    anonymous=stage == "public",
                    suites={},
                )
                suites = (
                    charts.PUBLIC_SUITES
                    if stage == "public" and schema == 2
                    else charts.SUITES
                )
                for name in suites:
                    item["suites"][name] = {
                        "result": "passed",
                        "platform": item["platform"],
                        "sandbox_mode": "disabled" if name == "disabled" else "enabled",
                        "default_image": {
                            "passed": True,
                            "image": charts.IMAGE + "@" + DIGEST,
                        },
                        "policy": {"enforced": True},
                        "sandbox_evidence": {"no_sandbox": name == "disabled"},
                        "network": {"result": "passed"} if name == "network" else None,
                        "suite": "distribution" if name == "distribution" else name,
                    }
                record["acceptance"][stage][arch] = item
        return record

    def test_v2_public_smoke_requires_full_archive_and_both_native_platforms(self):
        record = self.acceptance_record(schema=2)
        charts.validate_proof(record)
        self.args.evidence = self.root / "evidence"
        for stage, platforms in record["acceptance"].items():
            for arch, item in platforms.items():
                charts.write_json(
                    self.args.evidence / stage / arch / "acceptance.json", item
                )
        charts.finalize(self.args)
        output = json.loads((self.args.output / "chart-distribution.json").read_text())
        self.assertEqual(output["schema_version"], 2)
        charts.validate_proof(output)
        for mutate in (
            lambda r: r["acceptance"]["archive"]["amd64"].update(
                suites=r["acceptance"]["public"]["amd64"]["suites"]
            ),
            lambda r: r["acceptance"]["archive"]["arm64"]["suites"].pop("network"),
            lambda r: r["acceptance"]["public"].pop("arm64"),
            lambda r: r["acceptance"]["public"]["arm64"].update(anonymous=False),
            lambda r: r["acceptance"]["public"]["amd64"]["suites"][
                "distribution"
            ].update(suite="lifecycle"),
            lambda r: r["acceptance"]["public"]["amd64"]["suites"][
                "distribution"
            ].update(sandbox_evidence={"no_sandbox": True}),
            lambda r: r["acceptance"]["public"]["amd64"]["suites"][
                "distribution"
            ].update(default_image={"passed": True, "image": "another:image"}),
            lambda r: r.pop("schema_version"),
            lambda r: r.update(schema_version=3),
            lambda r: r["acceptance"]["public"]["amd64"].update(schema_version=3),
        ):
            broken = deepcopy(record)
            mutate(broken)
            with self.assertRaises((ValueError, KeyError)):
                charts.validate_proof(broken)

    def test_public_acceptance_only_runs_distribution_smoke_on_downloaded_archive(self):
        self.args.public = True
        item = self.acceptance_record(schema=2)["acceptance"]["public"]["amd64"]

        def smoke(*args, **kwargs):
            if args[:2] == ("docker", "pull"):
                self.assertFalse(Path(kwargs["env"]["DOCKER_CONFIG"]).exists())
                return
            self.assertIn("--distribution-only", args)
            self.assertEqual(args[args.index("--chart") + 1], self.downloaded)
            output = args[args.index("--artifacts") + 1]
            charts.write_json(output / "result.json", item["suites"]["distribution"])
            charts.write_json(output / "chromium-sandbox.json", {"no_sandbox": False})

        with (
            patch.object(charts, "manifest", return_value=b"{}"),
            patch.object(charts, "pull", return_value=self.downloaded),
            patch.object(charts, "static_check") as static,
            patch.object(charts.platform, "system", return_value="Linux"),
            patch.object(charts.platform, "machine", return_value="x86_64"),
            patch.object(charts, "run", side_effect=smoke) as run,
        ):
            report = charts.acceptance(self.args)
        self.assertEqual(run.call_count, 2)
        static.assert_not_called()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(set(report["suites"]), {"distribution"})
        self.assertTrue((self.args.output / "acceptance.json").exists())

    def test_archive_acceptance_still_runs_all_three_full_suites(self):
        item = self.acceptance_record(schema=2)["acceptance"]["archive"]["amd64"]

        def smoke(*args, **kwargs):
            if args[:2] == ("docker", "pull"):
                return
            self.assertNotIn("--distribution-only", args)
            output = args[args.index("--artifacts") + 1]
            result = item["suites"][output.name]
            self.assertEqual(args[args.index("--sandbox") + 1], result["sandbox_mode"])
            self.assertEqual("--network-only" in args, output.name == "network")
            charts.write_json(output / "result.json", result)
            charts.write_json(
                output / "chromium-sandbox.json", result["sandbox_evidence"]
            )

        with (
            patch.object(charts, "static_check") as static,
            patch.object(charts.platform, "system", return_value="Linux"),
            patch.object(charts.platform, "machine", return_value="x86_64"),
            patch.object(charts, "run", side_effect=smoke) as run,
        ):
            report = charts.acceptance(self.args)
        self.assertEqual(run.call_count, 4)
        static.assert_called_once()
        self.assertEqual(set(report["suites"]), {"enabled", "disabled", "network"})

    def test_public_manifest_change_after_smoke_cannot_create_proof(self):
        self.args.public = True
        item = self.acceptance_record(schema=2)["acceptance"]["public"]["amd64"]
        output = self.args.output / "distribution"
        charts.write_json(output / "result.json", item["suites"]["distribution"])
        charts.write_json(output / "chromium-sandbox.json", {"no_sandbox": False})
        proof = self.args.output / "acceptance.json"
        proof.write_text("stale success")
        with (
            patch.object(charts, "manifest", side_effect=[b"{}", b"{}", b"changed"]),
            patch.object(charts, "pull", return_value=self.downloaded),
            patch.object(charts.platform, "system", return_value="Linux"),
            patch.object(charts.platform, "machine", return_value="x86_64"),
            patch.object(charts, "run"),
        ):
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                charts.acceptance(self.args)
        self.assertFalse(proof.exists())

    def test_v2_recovery_preserves_valid_legacy_release_asset_bytes(self):
        legacy = self.acceptance_record()
        candidate = self.root / "chart-distribution.json"
        charts.write_json(candidate, self.acceptance_record(schema=2))
        original = (json.dumps(legacy) + "\n").encode()

        def download(command, stdout, **kwargs):
            stdout.write(original)

        with patch("github_release.subprocess.run", side_effect=download):
            recovered = reuse_chart_record(
                "example/test",
                {"assets": [{"id": 123, "name": "chart-distribution.json"}]},
                candidate,
                TAG,
                REVISION,
                DIGEST,
                self.root / "recovered",
            )
        self.assertEqual(recovered.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
