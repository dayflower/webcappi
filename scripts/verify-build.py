#!/usr/bin/env python3
"""Verify both exported ELF binaries against their OCI runtime image layers."""

import hashlib
import json
import os
from pathlib import Path
import struct
import tarfile

from build_info import identity

ROOT = Path(__file__).resolve().parent.parent
MACHINES = {"amd64": 62, "arm64": 183}
IMAGE_BINARY = "usr/local/bin/webcappi"


def fingerprint(stream, arch):
    header = stream.read(20)
    if (
        len(header) != 20
        or header[:6] != b"\x7fELF\x02\x01"
        or struct.unpack("<H", header[18:20])[0] != MACHINES[arch]
    ):
        raise ValueError(f"Expected a Linux {arch} ELF64 executable")
    digest = hashlib.sha256(header)
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def blob_path(descriptor):
    algorithm, digest = descriptor["digest"].split(":", 1)
    if algorithm != "sha256" or len(digest) != 64:
        raise ValueError(f"Unsupported OCI digest: {descriptor['digest']}")
    int(digest, 16)
    return f"blobs/{algorithm}/{digest}"


def image_fingerprint(archive, manifest, arch):
    # The last layer containing this path supplies the application executable.
    for layer in reversed(manifest["layers"]):
        with archive.extractfile(blob_path(layer)) as compressed:
            with tarfile.open(fileobj=compressed, mode="r|*") as contents:
                for entry in contents:
                    if entry.name.removeprefix("./") == IMAGE_BINARY:
                        if not entry.isfile():
                            raise ValueError("OCI application binary is not a file")
                        with contents.extractfile(entry) as binary:
                            return fingerprint(binary, arch)
    raise ValueError(f"No application binary in the {arch} image")


def verify(artifacts, build_identity=None):
    expected = {}
    for arch in MACHINES:
        with (artifacts / "binaries" / f"linux_{arch}" / "webcappi").open("rb") as f:
            expected[arch] = fingerprint(f, arch)

    report = {}
    with tarfile.open(artifacts / "webcappi.oci.tar") as archive:

        def read_json(path):
            with archive.extractfile(path) as stream:
                return json.load(stream)

        def visit(descriptor):
            annotations = descriptor.get("annotations", {})
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                return
            manifest = read_json(blob_path(descriptor))
            if "manifests" in manifest:
                for child in manifest["manifests"]:
                    visit(child)
                return
            config = read_json(blob_path(manifest["config"]))
            if build_identity is not None:
                labels = config.get("config", {}).get("Labels", {})
                expected_labels = {
                    "org.opencontainers.image.source": "https://github.com/dayflower/webcappi",
                    "org.opencontainers.image.version": build_identity["version"],
                    "org.opencontainers.image.revision": build_identity["revision"],
                    "io.github.dayflower.webcappi.modified": build_identity["modified"],
                }
                if any(
                    labels.get(key) != value for key, value in expected_labels.items()
                ):
                    raise ValueError(f"OCI build identity differs: {labels}")
            arch = config["architecture"]
            if config["os"] != "linux" or arch not in expected:
                raise ValueError(f"Unexpected runtime platform: {config}")
            platform = descriptor.get("platform", {})
            if platform.get("os") != "linux" or platform.get("architecture") != arch:
                raise ValueError(f"OCI descriptor/config platform mismatch: {platform}")
            if arch in report:
                raise ValueError(f"Duplicate runtime platform: {arch}")
            actual = image_fingerprint(archive, manifest, arch)
            if actual != expected[arch]:
                raise ValueError(f"OCI and exported {arch} binaries differ")
            report[arch] = {
                "platform": f"linux/{arch}",
                "manifest": descriptor["digest"],
                "binary_sha256": actual,
            }

        for descriptor in read_json("index.json")["manifests"]:
            visit(descriptor)

    if report.keys() != expected.keys():
        raise ValueError(
            f"Missing runtime platforms: {expected.keys() - report.keys()}"
        )
    return report


def main():
    artifacts = ROOT / "artifacts"
    build_identity = identity()
    for field in build_identity:
        build_identity[field] = os.environ.get(
            f"WEBCAPPI_{field.upper()}", build_identity[field]
        )
    report = verify(artifacts, build_identity)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (artifacts / "build-verification.json").write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
