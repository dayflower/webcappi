"""Resolve deterministic build identity from a checkout, without a VERSION file."""

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
RELEASE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
)


def release_version(tag):
    if not RELEASE.fullmatch(tag) or len(tag[1:]) > 128:
        raise ValueError(f"Invalid release tag: {tag!r}; expected vX.Y.Z[-prerelease]")
    return tag[1:]


def git(*args, root=ROOT):
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
    ).strip()


def identity(root=ROOT):
    result = {"version": "dev", "revision": "unknown", "modified": "unknown"}
    try:
        result["revision"] = git("rev-parse", "HEAD", root=root)
        dirty = bool(
            git("status", "--porcelain", "--untracked-files=normal", root=root)
        )
        result["modified"] = str(dirty).lower()
        if not dirty:
            tags = git("tag", "--points-at", "HEAD", root=root).splitlines()
            releases = []
            for tag in tags:
                try:
                    release_version(tag)
                    releases.append(tag)
                except ValueError:
                    pass
            # Ambiguous release identities must be supplied explicitly by CD.
            if len(releases) == 1:
                result["version"] = releases[0]
    except (OSError, subprocess.CalledProcessError):
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("field", choices=("version", "revision", "modified"))
    args = parser.parse_args()
    print(identity()[args.field])


if __name__ == "__main__":
    main()
