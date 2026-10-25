"""Exercise publication failures, immutable versions and latest promotion rules."""

import argparse
from contextlib import nullcontext
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import build_info
import release


class ReleaseTest(unittest.TestCase):
    def test_oci_extraction_ignores_ownership_and_cleans_up_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.tar"
            with tarfile.open(archive, "w") as contents:
                entry = tarfile.TarInfo("index.json")
                entry.uid = entry.gid = 0
                entry.size = 2
                contents.addfile(entry, io.BytesIO(b"{}"))
            extracted = None
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                with release.oci_source(archive) as source:
                    extracted = Path(source.removeprefix("oci:"))
                    self.assertEqual((extracted / "index.json").read_bytes(), b"{}")
                    self.assertEqual(
                        (extracted / "index.json").stat().st_uid, release.os.getuid()
                    )
                    raise RuntimeError("copy failed")
            self.assertFalse(extracted.exists())
            with tarfile.open(archive, "w") as contents:
                entry = tarfile.TarInfo("link")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                contents.addfile(entry)
            with self.assertRaisesRegex(ValueError, "regular files"):
                with release.oci_source(archive):
                    self.fail("OCI symlink accepted")

    def test_ghcr_first_publication_and_authentication_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text(
                json.dumps({"auths": {"ghcr.io": {"auth": "dXNlcjp0b2tlbg=="}}})
            )
            for code, absent in ((404, True), (403, False), (401, False)):
                with self.subTest(code=code):
                    token = io.BytesIO(b'{"token":"writer-token"}')
                    error = urllib.error.HTTPError(
                        "https://ghcr.io/v2/example/test/manifests/0.1.0",
                        code,
                        "test error",
                        {},
                        io.BytesIO(b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'),
                    )
                    with (
                        patch.dict(
                            release.os.environ, {"REGISTRY_AUTH_FILE": str(auth)}
                        ),
                        patch.object(
                            release.urllib.request,
                            "urlopen",
                            side_effect=[token, error],
                        ) as request,
                    ):
                        if absent:
                            self.assertIsNone(
                                release.ghcr_manifest("ghcr.io/example/test:0.1.0")
                            )
                        else:
                            with self.assertRaisesRegex(RuntimeError, f"HTTP {code}"):
                                release.ghcr_manifest("ghcr.io/example/test:0.1.0")
                        token_request = request.call_args_list[0].args[0]
                        self.assertIn("pull%2Cpush", token_request.full_url)

    def test_tag_commit_and_main_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return build_info.git(*args, root=root)

            git("init", "--initial-branch=main")
            (root / "file").write_text("initial")
            git("add", "file")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "test: initial",
            )
            revision = git("rev-parse", "HEAD")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "tag",
                "-a",
                "v0.1.0",
                "-m",
                "Release v0.1.0",
            )
            git("update-ref", "refs/remotes/origin/main", revision)
            with (
                patch.object(release, "ROOT", root),
                patch.object(release, "git", side_effect=git),
            ):
                self.assertEqual(release.validate("v0.1.0", revision), "0.1.0")
                with self.assertRaisesRegex(ValueError, "event commit"):
                    release.validate("v0.1.0", "b" * 40)
                (root / "file").write_text("changed")
                with self.assertRaisesRegex(ValueError, "dirty"):
                    release.validate("v0.1.0", revision)
                git("add", "file")
                git(
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "test: outside main",
                )
                outside = git("rev-parse", "HEAD")
                with self.assertRaisesRegex(ValueError, "Tag does not point"):
                    release.validate("v0.1.0", outside)
                git("tag", "v0.2.0")
                with self.assertRaises(subprocess.CalledProcessError):
                    release.validate("v0.2.0", outside)

    def test_latest_never_moves_backwards(self):
        for candidate, current, expected in (
            ("v0.1.0", None, True),
            ("v0.2.0-rc.1", None, False),
            ("v0.1.0", "v0.2.0", False),
            ("v0.2.0", "v0.2.0", True),
            ("v0.10.0", "v0.9.0", True),
            ("v1.0.0", "v0.99.99", True),
        ):
            with self.subTest(candidate=candidate, current=current):
                self.assertEqual(release.update_latest(candidate, current), expected)
        with self.assertRaises(ValueError):
            release.update_latest("v0.2.0", "v0.3.0-rc.1")

    def test_missing_manifest_is_not_an_auth_failure(self):
        for error, absent in (
            ("manifest unknown", True),
            ("NAME_UNKNOWN", True),
            ("unauthorized", False),
            ("connection refused", False),
        ):
            with self.subTest(error=error):
                result = subprocess.CompletedProcess([], 1, "", error)
                with patch.object(release.subprocess, "run", return_value=result):
                    if absent:
                        self.assertIsNone(
                            release.raw_manifest(
                                "docker://example/test:v1", missing_ok=True
                            )
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            release.raw_manifest(
                                "docker://example/test:v1", missing_ok=True
                            )

    def test_remote_platform_and_identity_validation(self):
        revision = "a" * 40
        descriptors = [
            {
                "digest": f"sha256:{arch}",
                "platform": {"os": "linux", "architecture": arch},
            }
            for arch in ("amd64", "arm64")
        ]
        raw = json.dumps({"manifests": descriptors})
        configs = {
            arch: {
                "os": "linux",
                "architecture": arch,
                "config": {
                    "Labels": {
                        "org.opencontainers.image.version": "v0.1.0",
                        "org.opencontainers.image.revision": revision,
                        "org.opencontainers.image.source": release.SOURCE,
                        "io.github.dayflower.webcappi.modified": "false",
                    }
                },
            }
            for arch in ("amd64", "arm64")
        }

        def manifest(reference, **kwargs):
            return raw if "@" not in reference else json.dumps({"layers": []})

        def run(*args):
            return json.dumps(configs[args[-1].rsplit(":", 1)[1]])

        with (
            patch.object(release, "raw_manifest", side_effect=manifest),
            patch.object(release, "run", side_effect=run),
        ):
            result = release.inspect_release(
                "ghcr.io/dayflower/webcappi:0.1.0", "v0.1.0", revision
            )
            self.assertEqual(set(result["manifests"]), {"amd64", "arm64"})
            configs["arm64"]["config"]["Labels"][
                "org.opencontainers.image.revision"
            ] = "b" * 40
            with self.assertRaisesRegex(ValueError, "identity differs"):
                release.inspect_release(
                    "ghcr.io/dayflower/webcappi:0.1.0", "v0.1.0", revision
                )

    def test_existing_version_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            (artifacts / "build-verification.json").write_text(
                json.dumps({arch: {"manifest": arch} for arch in release.ARCHES})
            )
            args = argparse.Namespace(
                tag="v0.1.0",
                revision="a" * 40,
                image="ghcr.io/dayflower/webcappi",
                reuse=False,
                artifacts=artifacts,
            )
            with (
                patch.object(
                    release, "inspect_release", return_value={"digest": "sha256:other"}
                ),
                patch.object(release, "raw_manifest", return_value="archive"),
                patch.object(
                    release, "oci_source", return_value=nullcontext("oci:test")
                ),
                patch.object(release, "copy_image") as copy,
            ):
                with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                    release.publish(args)
                copy.assert_not_called()

    def test_reuse_does_not_need_an_archive_or_rebuild(self):
        published = {
            "digest": "sha256:existing",
            "version": "v0.1.0",
            "revision": "a" * 40,
        }
        newer = {"digest": "sha256:newer", "version": "v0.2.0", "revision": "b" * 40}
        args = argparse.Namespace(
            tag="v0.1.0",
            revision="a" * 40,
            image="ghcr.io/dayflower/webcappi",
            reuse=True,
        )
        with (
            patch.object(release, "inspect_release", side_effect=[published, newer]),
            patch.object(release, "copy_image") as copy,
            patch.dict(release.os.environ, {"GITHUB_STEP_SUMMARY": ""}),
        ):
            release.publish(args)
            copy.assert_not_called()
        with patch.object(release, "inspect_release", return_value=None):
            with self.assertRaisesRegex(ValueError, "disappeared"):
                release.publish(args)


if __name__ == "__main__":
    unittest.main()
