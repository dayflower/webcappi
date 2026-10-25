"""Validate release tags and publish verified OCI archives without rebuilding."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from build_info import ROOT, git, release_version

ARCHES = {"amd64", "arm64"}
SOURCE = "https://github.com/dayflower/webcappi"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def ghcr_manifest(image):
    """Request a writer-scoped token so an unpublished package can return 404.

    A pull-only token request for a new GHCR package can be denied before its
    manifest is checked. Authentication failures must still stop publication.
    """
    repository, tag = image.removeprefix("ghcr.io/").rsplit(":", 1)
    auths = json.loads(Path(os.environ["REGISTRY_AUTH_FILE"]).read_text())["auths"]
    credentials = auths["ghcr.io"]["auth"]
    # Validate the stored credentials without including them in diagnostics.
    base64.b64decode(credentials, validate=True)
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repository}:pull,push"}
    )
    request = urllib.request.Request(
        f"https://ghcr.io/token?{query}",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ", ".join(
                    (
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                    )
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            details = json.loads(error.read())
            if any(
                item.get("code") in {"MANIFEST_UNKNOWN", "NAME_UNKNOWN"}
                for item in details.get("errors", [])
            ):
                return None
        raise RuntimeError(
            f"GHCR manifest check failed with HTTP {error.code}"
        ) from None


def raw_manifest(reference, missing_ok=False):
    if (
        missing_ok
        and reference.startswith("docker://ghcr.io/")
        and os.environ.get("REGISTRY_AUTH_FILE")
    ):
        return ghcr_manifest(reference.removeprefix("docker://"))
    result = subprocess.run(
        ["skopeo", "inspect", "--raw", reference],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Authentication and transport failures must never be treated as absence.
        missing = (
            "manifest unknown",
            "name unknown",
            "manifest_unknown",
            "name_unknown",
        )
        if missing_ok and any(item in result.stderr.lower() for item in missing):
            return None
        raise RuntimeError(f"Cannot inspect {reference}: {result.stderr.strip()}")
    return result.stdout


def digest(raw):
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def inspect_release(image, version=None, revision=None, missing_ok=False):
    raw = raw_manifest(f"docker://{image}", missing_ok=missing_ok)
    if raw is None:
        return None
    repository = image.split("@", 1)[0].rsplit(":", 1)[0]
    manifests = {}
    labels_by_arch = {}

    def visit(manifest):
        if "manifests" not in manifest:
            raise ValueError("Release image must contain a multi-platform index")
        for descriptor in manifest["manifests"]:
            if (
                descriptor.get("annotations", {}).get("vnd.docker.reference.type")
                == "attestation-manifest"
            ):
                continue
            reference = f"docker://{repository}@{descriptor['digest']}"
            child = json.loads(raw_manifest(reference))
            if "manifests" in child:
                visit(child)
                continue
            config = json.loads(run("skopeo", "inspect", "--config", reference))
            arch = config["architecture"]
            if config["os"] != "linux" or arch not in ARCHES or arch in manifests:
                raise ValueError(f"Unexpected or duplicate release platform: {config}")
            platform = descriptor.get("platform", {})
            if platform.get("os") != "linux" or platform.get("architecture") != arch:
                raise ValueError("Release descriptor/config platform mismatch")
            labels = config.get("config", {}).get("Labels", {})
            release_version(labels.get("org.opencontainers.image.version", ""))
            expected = {
                "org.opencontainers.image.source": SOURCE,
                "io.github.dayflower.webcappi.modified": "false",
            }
            if version is not None:
                expected["org.opencontainers.image.version"] = version
            if revision is not None:
                expected["org.opencontainers.image.revision"] = revision
            if any(labels.get(key) != value for key, value in expected.items()):
                raise ValueError(f"Release identity differs for {arch}: {labels}")
            manifests[arch] = descriptor["digest"]
            labels_by_arch[arch] = labels

    visit(json.loads(raw))
    if manifests.keys() != ARCHES:
        raise ValueError(f"Missing release platforms: {ARCHES - manifests.keys()}")
    identities = {
        (
            labels["org.opencontainers.image.version"],
            labels.get("org.opencontainers.image.revision"),
        )
        for labels in labels_by_arch.values()
    }
    if len(identities) != 1:
        raise ValueError("Release identities differ between platforms")
    found_version, found_revision = identities.pop()
    return {
        "digest": digest(raw),
        "version": found_version,
        "revision": found_revision,
        "manifests": manifests,
    }


def validate(tag, revision):
    version = release_version(tag)
    if git("rev-parse", "HEAD") != revision:
        raise ValueError("Checkout does not match the event commit")
    if git("rev-parse", f"refs/tags/{tag}^{{commit}}") != revision:
        raise ValueError("Tag does not point at the event commit")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release checkout is dirty")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"],
        cwd=ROOT,
        check=True,
    )
    return version


def update_latest(version, current):
    candidate = release_version(version)
    if "-" in candidate:
        return False
    if current is None:
        return True
    previous = release_version(current)
    if "-" in previous:
        raise ValueError("latest unexpectedly points at a prerelease")
    return tuple(map(int, candidate.split("."))) >= tuple(map(int, previous.split(".")))


def copy_image(source, destination):
    subprocess.run(
        ["skopeo", "copy", "--all", "--preserve-digests", source, destination],
        check=True,
    )


@contextmanager
def oci_source(archive):
    # Skopeo's oci-archive transport restores tar ownership and may need root.
    # An OCI directory avoids that requirement and preserves the same blobs.
    with tempfile.TemporaryDirectory(prefix="webcappi-release-") as directory:
        with tarfile.open(archive) as contents:
            if any(
                not (entry.isfile() or entry.isdir()) for entry in contents.getmembers()
            ):
                raise ValueError(
                    "OCI archive must contain only regular files and directories"
                )
            contents.extractall(directory, filter="data")
        yield f"oci:{directory}"


def publish(args):
    version = "v" + release_version(args.tag)
    image = f"{args.image}:{version[1:]}"
    existing = inspect_release(image, version, args.revision, missing_ok=True)
    if args.reuse:
        if existing is None:
            raise ValueError(
                "Previously published version disappeared; refusing to rebuild"
            )
    else:
        report = json.loads((args.artifacts / "build-verification.json").read_text())
        expected = {arch: item["manifest"] for arch, item in report.items()}
        if expected.keys() != ARCHES:
            raise ValueError("Build report does not cover both release platforms")
        with oci_source(args.artifacts / "webcappi.oci.tar") as source:
            local_digest = digest(raw_manifest(source))
            if existing is not None and existing["digest"] != local_digest:
                raise ValueError(
                    "Version already exists with another digest; refusing to overwrite"
                )
            if existing is None:
                copy_image(source, f"docker://{image}")
        existing = inspect_release(image, version, args.revision)
        if existing["digest"] != local_digest or existing["manifests"] != expected:
            raise ValueError("Published image differs from the verified OCI archive")

    # All publishing jobs share a concurrency group, including this read/update.
    if "-" not in release_version(version):
        latest = inspect_release(f"{args.image}:latest", missing_ok=True)
        if update_latest(version, latest["version"] if latest else None):
            source = f"docker://{args.image}@{existing['digest']}"
            copy_image(source, f"docker://{args.image}:latest")
            promoted = inspect_release(f"{args.image}:latest", version, args.revision)
            if promoted["digest"] != existing["digest"]:
                raise ValueError("latest digest differs from the published version")
    result = f"{args.image}@{existing['digest']}"
    print(result)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(f"Published `{image}`\n\nPinned image: `{result}`\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "check", "publish"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", default="ghcr.io/dayflower/webcappi")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    version = release_version(args.tag)
    if args.command == "validate":
        validate(args.tag, args.revision)
        outputs = {"version": args.tag, "revision": args.revision}
    elif args.command == "check":
        existing = inspect_release(
            f"{args.image}:{version}", args.tag, args.revision, missing_ok=True
        )
        outputs = {"exists": str(existing is not None).lower()}
    else:
        publish(args)
        return
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output:
        with args.github_output.open("a") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
