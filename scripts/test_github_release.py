"""Exercise GitHub publication without changing any remote release or package."""

import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import github_release as github

REVISION = "a" * 40
IMAGE = "ghcr.io/dayflower/webcappi"


def fixture(layout, arch, invalid=False):
    layout.mkdir(parents=True)

    def blob(data):
        value = hashlib.sha256(data).hexdigest()
        path = layout / "blobs" / "sha256" / value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"digest": "sha256:" + value}

    binary = b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", github.MACHINES[arch])
    if invalid:
        binary = b"not an ELF application"
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w:gz") as contents:
        entry = tarfile.TarInfo(github.IMAGE_BINARY)
        entry.size = len(binary)
        contents.addfile(entry, io.BytesIO(binary))
    manifest = blob(json.dumps({"layers": [blob(layer.getvalue())]}).encode())
    (layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
    return manifest["digest"], binary


class FakeReleases:
    def __init__(self, tag="v0.1.1", latest=None):
        self.tag, self.latest, self.current = tag, latest, None
        self.creates, self.patches, self.uploads = [], [], []
        self.fail_upload = None
        self.restrict_workflow_targets = False

    def api(self, repo, method, path, payload=None, missing_ok=False):
        if method == "GET":
            if path == "releases/latest":
                return copy.deepcopy(self.latest)
            if path.startswith("releases?"):
                return [copy.deepcopy(self.current)] if self.current else []
            if (
                path.startswith("releases/tags/")
                and self.current
                and self.current["draft"]
            ):
                return None
            return copy.deepcopy(self.current)
        if method == "POST":
            if self.restrict_workflow_targets and "target_commitish" in payload:
                raise PermissionError("Workflow modification permission required")
            self.creates.append(copy.deepcopy(payload))
            self.current = dict(
                payload,
                id=123,
                assets=[],
                body="Generated notes",
                html_url="https://github.com/example/test/releases/tag/" + self.tag,
            )
        elif method == "PATCH":
            self.patches.append(copy.deepcopy(payload))
            self.current.update(payload)
        else:
            raise AssertionError((method, path))
        return copy.deepcopy(self.current)

    def run(self, command, **kwargs):
        if command[:3] != ["gh", "release", "upload"]:
            raise AssertionError(command)
        asset = Path(command[4])
        if asset.name == self.fail_upload:
            self.fail_upload = None
            raise subprocess.CalledProcessError(1, command)
        self.uploads.append(asset.name)
        self.current["assets"].append(
            {
                "name": asset.name,
                "size": asset.stat().st_size,
                "state": "uploaded",
                "digest": "sha256:" + github.checksum(asset),
            }
        )


class GithubReleaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inspected = {"digest": "sha256:" + "b" * 64, "manifests": {}}
        self.fixtures = {}
        for arch in github.MACHINES:
            layout = self.root / "fixtures" / arch
            manifest, binary = fixture(layout, arch)
            self.inspected["manifests"][arch] = manifest
            self.fixtures[manifest] = (layout, binary)

    def recover(self, command, **kwargs):
        self.assertEqual(command[:3], ["skopeo", "copy", "--preserve-digests"])
        manifest = command[3].split("@", 1)[1]
        layout = Path(command[4].removeprefix("oci:").removesuffix(":release"))
        shutil.copytree(self.fixtures[manifest][0], layout)

    def assets(self, tag="v0.1.1", directory="assets"):
        with patch.object(github.subprocess, "run", side_effect=self.recover):
            return github.prepare_assets(
                tag, REVISION, IMAGE, self.inspected, self.root / directory
            )

    def publish(self, fake, assets):
        with (
            patch.object(github, "api", side_effect=fake.api),
            patch.object(github, "resolve_tag", return_value=REVISION),
            patch.object(github.subprocess, "run", side_effect=fake.run),
            patch.dict(github.os.environ, {"GITHUB_STEP_SUMMARY": ""}),
        ):
            github.publish_github(
                "example/test", fake.tag, REVISION, IMAGE, self.inspected, assets
            )

    def test_api_distinguishes_absence_from_permission_and_network_errors(self):
        for code in (200, 404, 403, 500):
            with self.subTest(code=code):
                output = f'HTTP/2.0 {code} Status\nContent-Type: application/json\n\n{{"value":1}}'
                response = subprocess.CompletedProcess(
                    [], int(code >= 400), output, "test error"
                )
                with patch.object(github.subprocess, "run", return_value=response):
                    if code == 404:
                        self.assertIsNone(
                            github.api(
                                "example/test",
                                "GET",
                                "releases/latest",
                                missing_ok=True,
                            )
                        )
                    elif code == 200:
                        self.assertEqual(
                            github.api("example/test", "GET", "releases/latest"),
                            {"value": 1},
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, f"HTTP {code}"):
                            github.api(
                                "example/test",
                                "GET",
                                "releases/latest",
                                missing_ok=True,
                            )
        with patch.object(
            github.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "network failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "network failure"):
                github.api("example/test", "GET", "releases/latest", missing_ok=True)

    def test_resolve_annotated_remote_tag_and_reject_moved_tag(self):
        reference = {"object": {"type": "tag", "sha": "annotated-object"}}
        commit = {"object": {"type": "commit", "sha": REVISION}}
        with (
            patch.object(github, "git", return_value=REVISION),
            patch.object(github.subprocess, "run"),
            patch.object(github, "api", side_effect=[reference, commit]),
        ):
            self.assertEqual(
                github.resolve_tag("example/test", "v0.1.1", REVISION), REVISION
            )
        with patch.object(github, "git", return_value=REVISION):
            with self.assertRaisesRegex(ValueError, "verified source"):
                github.resolve_tag("example/test", "v0.1.1", "b" * 40)
        commit["object"]["sha"] = "b" * 40
        with (
            patch.object(github, "git", return_value=REVISION),
            patch.object(github.subprocess, "run"),
            patch.object(github, "api", return_value=commit),
        ):
            with self.assertRaisesRegex(ValueError, "Remote release tag"):
                github.resolve_tag("example/test", "v0.1.1")

    def test_recovered_archives_are_deterministic_and_executable(self):
        first, second = self.assets(), self.assets(directory="again")
        self.assertEqual(
            [path.read_bytes() for path in first],
            [path.read_bytes() for path in second],
        )
        metadata = json.loads(first[2].read_text())
        self.assertEqual(metadata["image"], IMAGE + "@" + self.inspected["digest"])
        for arch, archive in zip(github.MACHINES, first[:2]):
            with tarfile.open(archive, "r:gz") as contents:
                self.assertEqual(contents.getnames(), ["webcappi"])
                self.assertEqual(contents.getmember("webcappi").mode, 0o755)
                binary = contents.extractfile("webcappi").read()
                self.assertEqual(
                    binary, self.fixtures[self.inspected["manifests"][arch]][1]
                )
                self.assertEqual(
                    metadata["platforms"][arch]["binary_sha256"],
                    hashlib.sha256(binary).hexdigest(),
                )
        for line in first[-1].read_text().splitlines():
            expected, name = line.split("  ")
            self.assertEqual(expected, github.checksum(first[-1].parent / name))

    def test_rejects_corrupt_blobs_and_wrong_platform_binary(self):
        manifest, _ = fixture(self.root / "invalid", "arm64", invalid=True)
        with self.assertRaisesRegex(ValueError, "ELF64"):
            github.extract_binary(
                self.root / "invalid", manifest, "arm64", self.root / "binary"
            )
        layout, _ = self.fixtures[self.inspected["manifests"]["amd64"]]
        manifest = self.inspected["manifests"]["amd64"]
        (layout / "blobs" / "sha256" / manifest.split(":")[1]).write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "blob digest differs"):
            github.extract_binary(layout, manifest, "amd64", self.root / "binary")

    def test_first_publication_waits_for_all_assets_and_retries_safely(self):
        assets = self.assets()
        fake = FakeReleases()
        fake.fail_upload = assets[1].name
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish(fake, assets)
        self.assertTrue(fake.current["draft"])
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(fake.patches, [])
        self.publish(fake, assets)
        self.assertFalse(fake.current["draft"])
        self.assertEqual(len(fake.creates), 1)
        self.assertEqual(fake.uploads, [path.name for path in assets])
        self.assertTrue(fake.creates[0]["generate_release_notes"])
        self.assertEqual(fake.patches[-1]["make_latest"], "true")
        self.assertIn("Generated notes", fake.current["body"])
        self.assertIn(IMAGE + "@" + self.inspected["digest"], fake.current["body"])
        fake.current["immutable"] = True
        self.publish(fake, assets)
        self.assertEqual(fake.uploads, [path.name for path in assets])

    def test_prerelease_and_old_version_do_not_replace_latest(self):
        for tag in ("v0.1.1", "v0.3.0-rc.1"):
            with self.subTest(tag=tag):
                assets = self.assets(tag, directory=tag)
                fake = FakeReleases(tag=tag, latest={"tag_name": "v0.2.0"})
                self.publish(fake, assets)
                self.assertEqual(fake.patches[-1]["make_latest"], "false")
                self.assertEqual(fake.current["prerelease"], "-" in tag)

    def test_existing_tag_recovery_does_not_require_workflow_modification(self):
        fake = FakeReleases()
        fake.restrict_workflow_targets = True
        self.publish(fake, self.assets())
        self.assertFalse(fake.current["draft"])
        self.assertEqual(fake.current["tag_name"], fake.tag)
        self.assertIn(REVISION, fake.current["body"])

    def test_tag_mismatch_stops_creation_before_remote_mutation(self):
        fake = FakeReleases()
        assets = self.assets()
        with (
            patch.object(github, "api", side_effect=fake.api),
            patch.object(github, "resolve_tag", side_effect=ValueError("Tag differs")),
        ):
            with self.assertRaisesRegex(ValueError, "Tag differs"):
                github.publish_github(
                    "example/test", fake.tag, REVISION, IMAGE, self.inspected, assets
                )
        self.assertEqual(fake.creates, [])

    def test_published_asset_conflicts_and_immutable_missing_assets_fail(self):
        assets = self.assets()
        fake = FakeReleases()
        self.publish(fake, assets)
        fake.patches.clear()
        fake.current["assets"][0]["digest"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(ValueError, "digest conflicts"):
            self.publish(fake, assets)
        self.assertEqual(fake.patches, [])
        fake.current["immutable"] = True
        fake.current["assets"] = []
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.publish(fake, assets)
        self.assertEqual(fake.patches, [])

    def test_fallback_download_verifies_asset_without_api_digest(self):
        asset = self.root / "asset"
        asset.write_bytes(b"known asset bytes")
        current = {
            "assets": [
                {
                    "id": 123,
                    "name": "asset",
                    "size": asset.stat().st_size,
                    "state": "uploaded",
                }
            ]
        }

        def download(command, stdout, **kwargs):
            self.assertIn("Accept: application/octet-stream", command)
            stdout.write(asset.read_bytes())

        with patch.object(github.subprocess, "run", side_effect=download):
            self.assertEqual(github.check_assets("example/test", current, [asset]), [])

    def test_manual_immutable_release_fails_before_binary_download(self):
        with (
            patch("sys.argv", ["github_release.py", "--tag", "v0.1.0"]),
            patch.object(github, "resolve_tag", return_value=REVISION),
            patch.object(github, "inspect_release", return_value=self.inspected),
            patch.object(github, "api", return_value={"immutable": True, "assets": []}),
            patch.object(github, "prepare_assets") as prepare,
        ):
            with self.assertRaisesRegex(ValueError, "immutable"):
                github.main()
            prepare.assert_not_called()

    def test_notes_preserve_user_content_and_reject_conflicting_identity(self):
        rendered = github.release_notes(
            "User-maintained notes", "v0.1.1", REVISION, IMAGE, self.inspected["digest"]
        )
        self.assertEqual(
            github.release_notes(
                rendered, "v0.1.1", REVISION, IMAGE, self.inspected["digest"]
            ),
            rendered,
        )
        with self.assertRaisesRegex(ValueError, "conflicting container identity"):
            github.release_notes(rendered, "v0.1.1", REVISION, IMAGE, "sha256:other")

    def test_chart_assets_and_installation_notes_are_added_before_draft_publication(
        self,
    ):
        record = self.root / "chart-distribution.json"
        record.write_text('{"accepted":true}\n')
        with patch.object(github.subprocess, "run", side_effect=self.recover):
            assets = github.prepare_assets(
                "v0.1.2",
                REVISION,
                IMAGE,
                self.inspected,
                self.root / "chart-assets",
                chart_record=record,
            )
        self.assertIn("chart-distribution.json", [p.name for p in assets])
        self.assertIn("chart-distribution.json", assets[-1].read_text())
        fake = FakeReleases(tag="v0.1.2")
        self.publish(fake, assets)
        self.assertIn("chart-distribution.json", fake.uploads)
        self.assertIn("helm install capture", fake.current["body"])
        self.assertIn("--version 0.1.2", fake.current["body"])
        self.assertIn("-f operator-values.yaml", fake.current["body"])

    def test_chart_aware_tag_cannot_publish_without_native_distribution_record(self):
        with (
            patch("sys.argv", ["github_release.py", "--tag", "v0.1.2"]),
            patch.object(github, "resolve_tag", return_value=REVISION),
            patch.object(github, "inspect_release", return_value=self.inspected),
            patch.object(github, "find_release", return_value=None),
            patch.object(github, "chart_required", return_value=True),
            patch.object(github, "prepare_assets") as prepare,
        ):
            with self.assertRaisesRegex(ValueError, "chart-distribution.json"):
                github.main()
            prepare.assert_not_called()

    def test_distribution_retry_reuses_previously_accepted_bytes_and_rejects_conflicts(
        self,
    ):
        candidate = self.root / "candidate.json"
        candidate.write_text("new acceptance run")
        current = {"assets": [{"id": 123, "name": "chart-distribution.json"}]}
        identity = {
            "manifest_digest": "sha256:a",
            "archive_sha256": "b",
            "content_sha256": "c",
        }

        def download(command, stdout, **kwargs):
            stdout.write(b"previous acceptance run")

        with (
            patch.object(github, "chart_record", return_value=identity),
            patch.object(github.subprocess, "run", side_effect=download),
        ):
            recovered = github.reuse_chart_record(
                "example/test",
                current,
                candidate,
                "v0.1.2",
                REVISION,
                self.inspected["digest"],
                self.root / "reuse",
            )
        self.assertEqual(recovered.read_bytes(), b"previous acceptance run")
        with (
            patch.object(
                github,
                "chart_record",
                side_effect=[identity, dict(identity, manifest_digest="different")],
            ),
            patch.object(github.subprocess, "run", side_effect=download),
        ):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                github.reuse_chart_record(
                    "example/test",
                    current,
                    candidate,
                    "v0.1.2",
                    REVISION,
                    self.inspected["digest"],
                    self.root / "reuse",
                )


if __name__ == "__main__":
    unittest.main()
