"""Bind release candidates to exact OCI images and native runtime qualification."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tarfile

from build_info import ROOT, release_version
from release import oci_source

VERIFY = runpy.run_path(str(ROOT / "scripts/verify-build.py"))
ARCHES = ("amd64", "arm64")
FILES = (
    "webcappi.oci.tar",
    "build-verification.json",
    *(f"binaries/linux_{arch}/webcappi" for arch in ARCHES),
)


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def context():
    return {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    }


def same_source(candidate, version, revision):
    release_version(version)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected a full source revision")
    if candidate["version"] != version or candidate["revision"] != revision:
        raise ValueError("Candidate version/revision differs")
    if candidate["schema_version"] != 1 or candidate["modified"] != "false":
        raise ValueError("Unsupported or modified candidate")


def same_run(candidate, current):
    if (
        candidate["repository"] != current["repository"]
        or candidate["run_id"] != current["run_id"]
        or not 1 <= candidate["run_attempt"] <= current["run_attempt"]
    ):
        raise ValueError("Candidate workflow run differs")


def image_identity(path, report):
    with tarfile.open(path) as archive:
        index = json.load(archive.extractfile("index.json"))
        if len(index["manifests"]) != 1:
            raise ValueError("Expected one multi-platform OCI index")

        def blob(descriptor):
            data = archive.extractfile(VERIFY["blob_path"](descriptor)).read()
            if "sha256:" + hashlib.sha256(data).hexdigest() != descriptor["digest"]:
                raise ValueError("OCI metadata digest differs")
            return json.loads(data)

        root = index["manifests"][0]
        if "manifests" not in blob(root):
            raise ValueError("Candidate must contain a multi-platform index")
        configs = {}
        for arch in ARCHES:
            manifest = blob({"digest": report[arch]["manifest"]})
            config = blob(manifest["config"])
            if config["os"] != "linux" or config["architecture"] != arch:
                raise ValueError("OCI config platform differs")
            configs[arch] = manifest["config"]["digest"]
        return root["digest"], configs


def record(artifacts, version, revision):
    value = {
        "schema_version": 1,
        **context(),
        "version": version,
        "revision": revision,
        "modified": "false",
    }
    same_source(value, version, revision)
    report = VERIFY["verify"](artifacts, value)
    if report != read(artifacts / "build-verification.json"):
        raise ValueError("Build verification report differs")
    value["image_digest"], value["configs"] = image_identity(
        artifacts / "webcappi.oci.tar", report
    )
    value["files"] = {name: checksum(artifacts / name) for name in FILES}
    write(artifacts / "candidate.json", value)
    return value


def verify_files(artifacts, version, revision):
    candidate = read(artifacts / "candidate.json")
    same_source(candidate, version, revision)
    if set(candidate["files"]) != set(FILES):
        raise ValueError("Candidate files are incomplete")
    for name, expected in candidate["files"].items():
        path = artifacts / name
        if not path.is_file() or path.is_symlink() or checksum(path) != expected:
            raise ValueError(f"Candidate checksum differs: {name}")
    report = VERIFY["verify"](artifacts, candidate)
    if report != read(artifacts / "build-verification.json"):
        raise ValueError("Build verification report differs")
    digest, configs = image_identity(artifacts / "webcappi.oci.tar", report)
    if digest != candidate["image_digest"] or configs != candidate["configs"]:
        raise ValueError("Candidate OCI identity differs")
    return candidate


def loaded_id(image):
    return subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()


def verify_loaded(artifacts, arch, image, config_digest):
    actual = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[
        0
    ]
    with tarfile.open(artifacts / "webcappi.oci.tar") as archive:
        expected = json.load(
            archive.extractfile(VERIFY["blob_path"]({"digest": config_digest}))
        )

    # Docker's containerd store reports a manifest ID instead of the config ID.
    # Rootfs diff IDs and the complete execution config must still be identical.
    def nonnull(value):
        return {key: item for key, item in value.items() if item is not None}

    if (
        actual["Os"] != "linux"
        or actual["Architecture"] != arch
        or actual["RootFS"]["Layers"] != expected.get("rootfs", {}).get("diff_ids", [])
        or (
            actual["Id"] != config_digest
            and nonnull(actual["Config"]) != nonnull(expected["config"])
        )
    ):
        raise ValueError("Loaded Docker image differs from candidate OCI config/rootfs")
    return actual["Id"]


def load(args):
    args.output.unlink(missing_ok=True)
    candidate = verify_files(args.artifacts, args.version, args.revision)
    same_run(candidate, context())
    with oci_source(args.artifacts / "webcappi.oci.tar") as source:
        subprocess.run(
            [
                "skopeo",
                "copy",
                "--override-os",
                "linux",
                "--override-arch",
                args.arch,
                source,
                "docker-daemon:" + args.image,
            ],
            check=True,
        )
    image_id = verify_loaded(
        args.artifacts, args.arch, args.image, candidate["configs"][args.arch]
    )
    write(
        args.output,
        {
            **context(),
            "arch": args.arch,
            "image": args.image,
            "config_digest": candidate["configs"][args.arch],
            "loaded_id": image_id,
            "candidate_sha256": checksum(args.artifacts / "candidate.json"),
        },
    )


def runtime(args):
    args.output.unlink(missing_ok=True)
    value = read(args.loaded)
    same_run(value, context())
    arch = value["arch"]
    if loaded_id(value["image"]) != value["loaded_id"]:
        raise ValueError("Runtime image changed after loading")
    for suite in ("enabled", "disabled", "network"):
        root = args.evidence / (
            "helm-network" if suite == "network" else "helm-" + suite
        )
        result = read(root / "result.json")
        sandbox = read(root / "chromium-sandbox.json")
        if (
            result["result"] != "passed"
            or result["platform"] != "linux/" + arch
            or result["source_image"] != value["image"]
            or result["images"]["source_id"] != value["loaded_id"]
            or not result["default_image"]["passed"]
            or result["default_image"]["image"] != value["image"]
            or result["sandbox_mode"]
            != ("disabled" if suite == "disabled" else "enabled")
            or sandbox["no_sandbox"] is not (suite == "disabled")
            or (suite == "network" and result["network"]["result"] != "passed")
        ):
            raise ValueError("Incomplete candidate runtime qualification")
    for case in ("setup-failure", "sigterm"):
        result = read(args.evidence / "helm-cleanup" / (case + ".json"))
        if result["result"] != "passed" or not result["node_removed"]:
            raise ValueError("Incomplete sandbox cleanup qualification")
    value.update(result="passed", run_attempt=context()["run_attempt"])
    write(args.output, value)


def validate_qualification(value, version, revision):
    candidate = value["candidate"]
    same_source(candidate, version, revision)
    same_run(candidate, value)
    if value["schema_version"] != 1 or value["result"] != "passed":
        raise ValueError("Candidate was not fully qualified")
    if set(value["runtime"]) != set(ARCHES):
        raise ValueError("Native runtime qualification requires both architectures")
    for arch, receipt in value["runtime"].items():
        same_run(receipt, value)
        if (
            receipt["arch"] != arch
            or receipt["result"] != "passed"
            or receipt["candidate_sha256"] != value["candidate_sha256"]
            or receipt["config_digest"] != candidate["configs"][arch]
        ):
            raise ValueError("Runtime qualification identity differs")
    return candidate


def qualify(args):
    args.output.unlink(missing_ok=True)
    candidate = read(args.metadata)
    same_run(candidate, context())
    receipts = {}
    metadata_hash = checksum(args.metadata)
    for path in sorted(args.receipts.rglob("runtime.json")):
        item = read(path)
        arch = item["arch"]
        if arch not in ARCHES:
            raise ValueError("Unexpected native qualification platform")
        if item["candidate_sha256"] != metadata_hash:
            continue
        # Earlier successful jobs can supply evidence for a failed-job rerun.
        if arch not in receipts or receipts[arch]["run_attempt"] < item["run_attempt"]:
            receipts[arch] = item
    value = {
        "schema_version": 1,
        **context(),
        "result": "passed",
        "bundle_id": args.bundle_id,
        "candidate": candidate,
        "candidate_sha256": metadata_hash,
        "runtime": receipts,
    }
    validate_qualification(value, args.version, args.revision)
    write(args.output, value)


def verify(args):
    value = read(args.qualification)
    candidate = validate_qualification(value, args.version, args.revision)
    if (
        value["repository"] != os.environ["GITHUB_REPOSITORY"]
        or value["run_id"] != args.run_id
        or value["run_attempt"] != args.run_attempt
        or value["bundle_id"] != args.bundle_id
        or value["candidate_sha256"] != checksum(args.artifacts / "candidate.json")
    ):
        raise ValueError("Selected qualification/artifact identity differs")
    if candidate != verify_files(args.artifacts, args.version, args.revision):
        raise ValueError("Qualified candidate metadata differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("version", "record", "load", "runtime", "qualify", "verify")
    )
    parser.add_argument("--version", default=os.environ.get("WEBCAPPI_VERSION", ""))
    parser.add_argument("--revision", default=os.environ.get("WEBCAPPI_REVISION", ""))
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--arch", choices=ARCHES)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--loaded", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--bundle-id", type=int)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.command == "version":
        import yaml

        value = yaml.safe_load((ROOT / "charts/webcappi/Chart.yaml").read_text())
        version = "v" + value["version"]
        if value["appVersion"] != release_version(version):
            raise ValueError("Chart version/appVersion differ")
        if args.github_output:
            with args.github_output.open("a") as stream:
                stream.write(f"version={version}\n")
        print(version)
    elif args.command == "record":
        record(args.artifacts, args.version, args.revision)
    else:
        {"load": load, "runtime": runtime, "qualify": qualify, "verify": verify}[
            args.command
        ](args)


if __name__ == "__main__":
    main()
