"""Publish GitHub Releases with binaries recovered from the verified GHCR image."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import urllib.parse

from build_info import ROOT, git, release_version
from release import inspect_release, update_latest
from release_charts import REGISTRY, validate_proof

MACHINES = {"amd64": 62, "arm64": 183}
IMAGE_BINARY = "usr/local/bin/webcappi"
MARKER = "<!-- webcappi-container -->"


def api(repo, method, path, payload=None, missing_ok=False):
    command = ["gh", "api", "--include", "--method", method, f"repos/{repo}/{path}"]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    headers, separator, body = result.stdout.partition("\n\n")
    status = re.match(r"HTTP/\S+ ([0-9]{3})\b", headers)
    if not separator or not status:
        raise RuntimeError(f"GitHub {method} {path} failed: {result.stderr.strip()}")
    code = int(status[1])
    if missing_ok and code == 404:
        return None
    if result.returncode or not 200 <= code < 300:
        raise RuntimeError(f"GitHub {method} {path} failed with HTTP {code}")
    return json.loads(body)


def resolve_tag(repo, tag, expected_revision=""):
    release_version(tag)
    revision = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    if expected_revision and revision != expected_revision:
        raise ValueError("Release tag differs from the verified source commit")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"],
        cwd=ROOT,
        check=True,
    )
    # Verify the remote tag too: the Releases API otherwise creates missing tags.
    reference = api(repo, "GET", f"git/ref/tags/{urllib.parse.quote(tag, safe='')}")[
        "object"
    ]
    for _ in range(10):
        if reference["type"] == "commit":
            if reference["sha"] != revision:
                raise ValueError("Remote release tag differs from the local tag")
            return revision
        if reference["type"] != "tag":
            break
        reference = api(repo, "GET", f"git/tags/{reference['sha']}")["object"]
    raise ValueError("Release tag does not resolve to a commit")


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def find_release(repo, tag):
    published = api(
        repo,
        "GET",
        f"releases/tags/{urllib.parse.quote(tag, safe='')}",
        missing_ok=True,
    )
    if published is not None:
        return published
    # The tag endpoint only guarantees published releases; recover draft retries
    # through the authenticated release listing instead of creating duplicates.
    matches = []
    for page in range(1, 101):
        releases = api(repo, "GET", f"releases?per_page=100&page={page}")
        matches.extend(item for item in releases if item["tag_name"] == tag)
        if len(releases) < 100:
            if len(matches) > 1:
                raise ValueError("Multiple GitHub Releases exist for the same tag")
            return matches[0] if matches else None
    raise ValueError("GitHub Release lookup exceeded the pagination limit")


def read_blob(layout, descriptor):
    algorithm, value = descriptor["digest"].split(":", 1)
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Unsupported OCI blob digest")
    path = layout / "blobs" / algorithm / value
    if checksum(path) != value:
        raise ValueError("Recovered OCI blob digest differs")
    return path


def extract_binary(layout, manifest_digest, arch, destination):
    index = json.loads((layout / "index.json").read_text())
    if (
        len(index["manifests"]) != 1
        or index["manifests"][0]["digest"] != manifest_digest
    ):
        raise ValueError("Recovered image differs from the verified platform manifest")
    manifest = json.loads(read_blob(layout, index["manifests"][0]).read_text())
    for descriptor in reversed(manifest["layers"]):
        with tarfile.open(read_blob(layout, descriptor), "r|*") as layer:
            for entry in layer:
                if entry.name.removeprefix("./") != IMAGE_BINARY:
                    continue
                if not entry.isfile():
                    raise ValueError("Image application binary is not a regular file")
                with (
                    layer.extractfile(entry) as stream,
                    destination.open("wb") as output,
                ):
                    shutil.copyfileobj(stream, output)
                with destination.open("rb") as stream:
                    header = stream.read(20)
                if (
                    len(header) != 20
                    or header[:6] != b"\x7fELF\x02\x01"
                    or struct.unpack("<H", header[18:20])[0] != MACHINES[arch]
                ):
                    raise ValueError(
                        f"Expected a Linux {arch} ELF64 application binary"
                    )
                return
    raise ValueError("No application binary in the published image")


def asset_names(tag, chart=False):
    return [f"webcappi_{tag}_linux_{arch}.tar.gz" for arch in MACHINES] + [
        "release-manifest.json",
        *(["chart-distribution.json"] if chart else []),
        "SHA256SUMS",
    ]


def prepare_assets(tag, revision, image, inspected, output, chart_record=None):
    output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "version": tag,
        "revision": revision,
        "image": f"{image}@{inspected['digest']}",
        "platforms": {},
    }
    for arch in MACHINES:
        with tempfile.TemporaryDirectory(
            prefix="webcappi-release-assets-"
        ) as directory:
            temporary = Path(directory)
            layout = temporary / "oci"
            manifest = inspected["manifests"][arch]
            subprocess.run(
                [
                    "skopeo",
                    "copy",
                    "--preserve-digests",
                    f"docker://{image}@{manifest}",
                    f"oci:{layout}:release",
                ],
                check=True,
            )
            binary = temporary / "webcappi"
            extract_binary(layout, manifest, arch, binary)
            archive = output / f"webcappi_{tag}_linux_{arch}.tar.gz"
            # Fix gzip/tar metadata so retries produce the same release assets.
            with (
                archive.open("wb") as stream,
                gzip.GzipFile(
                    filename="", fileobj=stream, mode="wb", mtime=0
                ) as compressed,
                tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
                ) as tar,
            ):
                entry = tarfile.TarInfo("webcappi")
                entry.mode, entry.size, entry.mtime = 0o755, binary.stat().st_size, 0
                with binary.open("rb") as contents:
                    tar.addfile(entry, contents)
            metadata["platforms"][arch] = {
                "manifest": manifest,
                "binary_sha256": checksum(binary),
            }
    (output / "release-manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    if chart_record is not None:
        destination = output / "chart-distribution.json"
        if chart_record.resolve() != destination.resolve():
            shutil.copyfile(chart_record, destination)
    assets = [output / name for name in asset_names(tag, chart_record is not None)]
    assets[-1].write_text(
        "".join(f"{checksum(path)}  {path.name}\n" for path in assets[:-1])
    )
    return assets


def check_assets(repo, current, assets):
    existing = {item["name"]: item for item in current.get("assets", [])}
    missing = []
    for path in assets:
        found = existing.get(path.name)
        if found is None:
            missing.append(path)
            continue
        expected = "sha256:" + checksum(path)
        if found.get("state") != "uploaded" or found["size"] != path.stat().st_size:
            raise ValueError(f"Release asset conflicts: {path.name}")
        if found.get("digest"):
            matches = found["digest"] == expected
        else:
            with tempfile.TemporaryDirectory(
                prefix="webcappi-release-check-"
            ) as directory:
                downloaded = Path(directory) / "asset"
                with downloaded.open("wb") as stream:
                    subprocess.run(
                        [
                            "gh",
                            "api",
                            f"repos/{repo}/releases/assets/{found['id']}",
                            "--header",
                            "Accept: application/octet-stream",
                        ],
                        stdout=stream,
                        check=True,
                    )
                matches = checksum(downloaded) == expected.removeprefix("sha256:")
        if not matches:
            raise ValueError(f"Release asset digest conflicts: {path.name}")
    if missing and current.get("immutable"):
        raise ValueError(
            "Published release is immutable and missing assets; use a new version"
        )
    return missing


def release_notes(body, tag, revision, image, digest, chart=False):
    section = f"{MARKER}\nContainer image: `{image}:{release_version(tag)}`\n\nPinned image: `{image}@{digest}`\n\nSource commit: `{revision}`\n<!-- /webcappi-container -->"
    if chart:
        section += (
            f"\n\n<!-- webcappi-chart -->\nHelm chart: `{REGISTRY}/webcappi`\n\n"
            f"Chart version: `{release_version(tag)}`. See `chart-distribution.json` for the verified OCI and image digests and native acceptance.\n\n"
            "Prepare the bearer-token Secret, compatible sandbox node policies and operator-values.yaml as described in the "
            f"[chart guide](https://github.com/dayflower/webcappi/blob/{tag}/charts/webcappi/README.md), then install:\n\n"
            f"```sh\nhelm install capture {REGISTRY}/webcappi --version {release_version(tag)} "
            "--namespace captures --create-namespace --set auth.existingSecret=capture-auth -f operator-values.yaml\n```\n<!-- /webcappi-chart -->"
        )
    body = body or ""
    if MARKER in body:
        if section not in body:
            raise ValueError("Release notes contain a conflicting container identity")
        return body
    return body.rstrip() + ("\n\n" if body else "") + section + "\n"


def publish_github(repo, tag, revision, image, inspected, assets):
    chart = any(path.name == "chart-distribution.json" for path in assets)
    current = find_release(repo, tag)
    prerelease = "-" in release_version(tag)
    if current is not None:
        if current["tag_name"] != tag or current["prerelease"] != prerelease:
            raise ValueError("Existing GitHub Release identity differs")
        missing = check_assets(repo, current, assets)
        # Check identity before uploading or changing any existing release.
        release_notes(
            current.get("body"), tag, revision, image, inspected["digest"], chart
        )
    else:
        current = api(
            repo,
            "POST",
            "releases",
            {
                "tag_name": tag,
                "target_commitish": revision,
                "name": tag,
                "draft": True,
                "prerelease": prerelease,
                "generate_release_notes": True,
                "make_latest": "false",
            },
        )
        missing = assets
    for asset in missing:
        subprocess.run(
            ["gh", "release", "upload", tag, str(asset), "--repo", repo], check=True
        )
    current = api(repo, "GET", f"releases/{current['id']}")
    if check_assets(repo, current, assets):
        raise ValueError("GitHub Release assets are incomplete")
    latest = (
        api(repo, "GET", "releases/latest", missing_ok=True) if not prerelease else None
    )
    promote = not prerelease and update_latest(
        tag, latest["tag_name"] if latest else None
    )
    published = api(
        repo,
        "PATCH",
        f"releases/{current['id']}",
        {
            "body": release_notes(
                current.get("body"), tag, revision, image, inspected["digest"], chart
            ),
            "draft": False,
            "prerelease": prerelease,
            "make_latest": str(promote).lower(),
        },
    )
    print(published["html_url"])
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(f"GitHub Release: {published['html_url']}\n")


def chart_required(revision):
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{revision}:scripts/release-charts.py"],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def chart_record(path, tag, revision, digest):
    if path is None:
        raise ValueError(
            "Chart-aware releases require chart-distribution.json acceptance"
        )
    record = json.loads(path.read_text())
    if (
        record["version"] != release_version(tag)
        or record["revision"] != revision
        or record["image_digest"] != digest
        or record["chart"] != REGISTRY + "/webcappi"
    ):
        raise ValueError("Chart distribution identity differs from the release")
    validate_proof(record)
    return record


def reuse_chart_record(repo, current, candidate, tag, revision, digest, output):
    """Keep previously accepted asset bytes stable for drafts and immutable retries."""
    expected = chart_record(candidate, tag, revision, digest)
    found = next(
        (
            item
            for item in (current or {}).get("assets", [])
            if item["name"] == "chart-distribution.json"
        ),
        None,
    )
    if found is None:
        return candidate
    output.mkdir(parents=True, exist_ok=True)
    recovered = output / "previous-chart-distribution.json"
    with recovered.open("wb") as stream:
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/releases/assets/{found['id']}",
                "--header",
                "Accept: application/octet-stream",
            ],
            stdout=stream,
            check=True,
        )
    previous = chart_record(recovered, tag, revision, digest)
    for key in ("manifest_digest", "archive_sha256", "content_sha256"):
        if previous[key] != expected[key]:
            raise ValueError("Existing chart distribution asset conflicts")
    return recovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "dayflower/webcappi")
    )
    parser.add_argument("--image", default="ghcr.io/dayflower/webcappi")
    parser.add_argument("--chart-record", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts" / "github-release"
    )
    args = parser.parse_args()
    revision = resolve_tag(args.repo, args.tag, args.revision)
    if args.digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        raise ValueError("Invalid verified image digest")
    reference = (
        f"{args.image}@{args.digest}"
        if args.digest
        else f"{args.image}:{release_version(args.tag)}"
    )
    inspected = inspect_release(reference, args.tag, revision)
    if args.digest and inspected["digest"] != args.digest:
        raise ValueError("Published image differs from the verified digest")
    existing = find_release(args.repo, args.tag)
    needs_chart = chart_required(revision)
    accepted_chart = None
    if needs_chart:
        accepted_chart = reuse_chart_record(
            args.repo,
            existing,
            args.chart_record,
            args.tag,
            revision,
            inspected["digest"],
            args.output,
        )
    if existing and existing.get("immutable"):
        missing = set(asset_names(args.tag, needs_chart)) - {
            item["name"] for item in existing.get("assets", [])
        }
        if missing:
            raise ValueError(
                "Published release is immutable and missing assets; use a new version"
            )
    assets = prepare_assets(
        args.tag,
        revision,
        args.image,
        inspected,
        args.output,
        **({"chart_record": accepted_chart} if needs_chart else {}),
    )
    publish_github(args.repo, args.tag, revision, args.image, inspected, assets)


if __name__ == "__main__":
    main()
