"""Exercise release syntax and build identity with disposable Git repositories."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from build_info import identity, release_version


class BuildIdentityTest(unittest.TestCase):
    def test_release_syntax(self):
        for tag in ("v0.1.0", "v1.2.3-rc.1", "v2.0.0-alpha-beta.0"):
            with self.subTest(tag=tag):
                self.assertEqual(release_version(tag), tag[1:])
        for tag in (
            "1.2.3",
            "v01.2.3",
            "v1.2",
            "v1.2.3-01",
            "v1.2.3+meta",
            "v1.2.3\n",
            "v1.2.3-",
            "v1.2.3-" + "a" * 128,
        ):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_version(tag)

    def test_checkout_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
                ).strip()

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
            self.assertEqual(
                identity(root),
                {"version": "dev", "revision": revision, "modified": "false"},
            )
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
            self.assertEqual(identity(root)["version"], "v0.1.0")
            (root / "untracked").write_text("changed")
            self.assertEqual(
                identity(root),
                {"version": "dev", "revision": revision, "modified": "true"},
            )
            (root / "untracked").unlink()
            git("tag", "v0.2.0")
            self.assertEqual(identity(root)["version"], "dev")

    def test_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                identity(Path(directory)),
                {"version": "dev", "revision": "unknown", "modified": "unknown"},
            )


if __name__ == "__main__":
    unittest.main()
