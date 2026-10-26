"""Helm release operations; registry writes are confined to the publish command."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import yaml

from build_info import ROOT, git, release_version
from release import inspect_release

CHART = ROOT / "charts/webcappi"
REGISTRY = "oci://ghcr.io/dayflower/charts"
IMAGE = "ghcr.io/dayflower/webcappi"
HELM = ROOT / "bin/chart-tools/helm"
SUITES = {"enabled": [], "disabled": [], "network": ["--network-only"]}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def metadata(chart, tag):
    version = release_version(tag)
    value = yaml.safe_load((chart / "Chart.yaml").read_text())
    if (
        value.get("name") != "webcappi"
        or value.get("version") != version
        or value.get("appVersion") != version
        or value.get("type") != "application"
    ):
        raise ValueError("Chart version and appVersion must both match the release tag")
    return version


def prepare_version(chart, tag, dry_run=False):
    version = release_version(tag)
    path = chart / "Chart.yaml"
    text = path.read_text()
    for key in ("version", "appVersion"):
        text, count = re.subn(rf"^{key}:.*$", f'{key}: "{version}"', text, flags=re.M)
        if count != 1:
            raise ValueError(f"Expected exactly one {key} in Chart.yaml")
    if not dry_run:
        path.write_text(text)
    print(text, end="")


def archive_files(archive, include_mode=False):
    """Compare every file, ignoring only tar/gzip container metadata."""
    files = {}
    size = 0
    with tarfile.open(archive, "r:gz") as contents:
        for entry in contents:
            path = PurePosixPath(entry.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "webcappi"
                or str(path) != entry.name.rstrip("/")
            ):
                raise ValueError("Unsafe chart archive path")
            if entry.isdir():
                continue
            if not entry.isfile() or entry.name in files:
                raise ValueError("Chart archive requires unique regular files")
            size += entry.size
            if size > 16 * 1024 * 1024:
                raise ValueError("Chart archive exceeds 16 MiB")
            data = contents.extractfile(entry).read()
            files[entry.name] = (entry.mode, data) if include_mode else data
    required = {"Chart.yaml", "values.yaml", "values.schema.json"}
    if not {"webcappi/" + name for name in required} <= files.keys():
        raise ValueError("Incomplete chart archive")
    return files


def content_hash(archive):
    files = archive_files(archive, include_mode=True)
    return sha256(
        json.dumps(
            {
                key: {"mode": mode, "sha256": sha256(data)}
                for key, (mode, data) in files.items()
            },
            sort_keys=True,
        ).encode()
    )


def compare(candidate, downloaded):
    if archive_files(candidate, include_mode=True) != archive_files(
        downloaded, include_mode=True
    ):
        raise ValueError(
            "Published chart contents differ; refusing to overwrite version"
        )


@contextmanager
def chart_source(source):
    """Never fall back to the checkout when an archive was requested."""
    if source.is_dir():
        yield source
    else:
        files = archive_files(source)
        with tempfile.TemporaryDirectory(prefix="webcappi-chart-source-") as directory:
            root = Path(directory)
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            yield root / "webcappi"


def identity(archive, tag, revision, image_digest):
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not DIGEST.fullmatch(
        image_digest
    ):
        raise ValueError("Expected a full source revision and sha256 image digest")
    with chart_source(archive) as chart:
        version = metadata(chart, tag)
        image = yaml.safe_load((chart / "values.yaml").read_text())["image"]
        if (
            image["repository"] != IMAGE
            or image["tag"] != version
            or image["digest"] != image_digest
        ):
            raise ValueError("Distribution chart must pin the verified release image")
    return {
        "version": version,
        "revision": revision,
        "image_digest": image_digest,
        "chart": REGISTRY + "/webcappi",
        "content_sha256": content_hash(archive),
    }


def static_check(source, helm, output):
    with chart_source(source) as chart:
        run(
            sys.executable,
            ROOT / "scripts/chart-check.py",
            "--chart",
            chart,
            "--helm",
            helm,
            "--output",
            output,
        )


def package(args):
    version = release_version(args.tag)
    inspected = inspect_release(f"{IMAGE}@{args.image_digest}", args.tag, args.revision)
    if inspected["digest"] != args.image_digest:
        raise ValueError("Image index differs from the verified digest")
    with tempfile.TemporaryDirectory(prefix="webcappi-chart-package-") as directory:
        root = Path(directory)
        chart = root / "webcappi"
        if args.source_revision:
            # Recover chart sources from the release commit, not moving main.
            result = run(
                "git",
                "archive",
                args.revision,
                "charts/webcappi",
                cwd=ROOT,
                capture_output=True,
            )
            archive = root / "source.tar"
            archive.write_bytes(result.stdout)
            with tarfile.open(archive) as contents:
                contents.extractall(root / "source", filter="data")
            shutil.copytree(root / "source/charts/webcappi", chart)
        else:
            shutil.copytree(args.source, chart)
        metadata(chart, args.tag)
        path = chart / "values.yaml"
        values = yaml.safe_load(path.read_text())
        values["image"].update(repository=IMAGE, tag=version, digest=args.image_digest)
        path.write_text(yaml.safe_dump(values, sort_keys=False))
        static_check(chart, args.helm, args.output)
    candidate = args.output / f"webcappi-{version}.tgz"
    identity(candidate, args.tag, args.revision, args.image_digest)
    return {"archive": str(candidate)}


@contextmanager
def helm_environment():
    """Isolate both Helm and Docker credential discovery, including anonymous pulls."""
    parent = os.environ.get("WEBCAPPI_CHART_AUTH_DIR")
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="webcappi-chart-auth-", dir=parent
    ) as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("HELM_")
        }
        env.update(
            HELM_CONFIG_HOME=str(root / "config"),
            HELM_CACHE_HOME=str(root / "cache"),
            HELM_DATA_HOME=str(root / "data"),
            HELM_REGISTRY_CONFIG=str(root / "registry.json"),
            DOCKER_CONFIG=str(root / "docker"),
        )
        yield env


def manifest(version, credentials=None, missing_ok=False):
    scope = "pull,push" if credentials else "pull"
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:dayflower/charts/webcappi:{scope}"}
    )
    headers = {}
    if credentials:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(credentials.encode()).decode()
        )
    # A denied token request is never evidence that a package is absent.
    request = urllib.request.Request(f"https://ghcr.io/token?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/dayflower/charts/webcappi/manifests/{version}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.manifest.v1+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            details = json.loads(error.read())
            errors = details.get("errors", [])
            if errors and all(
                item.get("code") in {"MANIFEST_UNKNOWN", "NAME_UNKNOWN"}
                for item in errors
            ):
                return None
        raise RuntimeError(
            f"Chart registry manifest check failed with HTTP {error.code}"
        ) from None


def pull(helm, version, destination, env, raw):
    run(
        helm,
        "pull",
        REGISTRY + "/webcappi",
        "--version",
        version,
        "--destination",
        destination,
        env=env,
    )
    archive = destination / f"webcappi-{version}.tgz"
    layers = json.loads(raw)["layers"]
    chart_layers = [
        item
        for item in layers
        if item["mediaType"] == "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
    ]
    if len(chart_layers) != 1 or chart_layers[0]["digest"] != "sha256:" + sha256(
        archive.read_bytes()
    ):
        raise ValueError("Pulled archive differs from the OCI manifest layer")
    return archive


def publish(args):
    expected = identity(args.archive, args.tag, args.revision, args.image_digest)
    credentials = os.environ["GHCR_USER"] + ":" + os.environ["GHCR_TOKEN"]
    with helm_environment() as env:
        run(
            args.helm,
            "registry",
            "login",
            "ghcr.io",
            "--username",
            os.environ["GHCR_USER"],
            "--password-stdin",
            input=os.environ["GHCR_TOKEN"],
            text=True,
            env=env,
            capture_output=True,
        )
        raw = manifest(expected["version"], credentials, missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="webcappi-chart-publish-") as directory:
            if raw is not None:
                downloaded = pull(
                    args.helm, expected["version"], Path(directory), env, raw
                )
                compare(args.archive, downloaded)
            else:
                run(args.helm, "push", args.archive, REGISTRY, env=env)
                raw = manifest(expected["version"], credentials)
                downloaded = pull(
                    args.helm, expected["version"], Path(directory), env, raw
                )
                compare(args.archive, downloaded)
            if manifest(expected["version"], credentials) != raw:
                raise ValueError("Chart manifest changed during publication")
    return {"manifest_digest": "sha256:" + sha256(raw)}


def acceptance(args):
    args.output.mkdir(parents=True, exist_ok=True)
    # Remove a previous success record before any operation that can fail.
    proof = args.output / "acceptance.json"
    proof.unlink(missing_ok=True)
    expected = identity(args.archive, args.tag, args.revision, args.image_digest)
    if args.archive.resolve().is_relative_to(args.output.resolve()):
        raise ValueError(
            "Candidate archive must be outside the acceptance output directory"
        )
    archive = args.archive.resolve()
    expected["stage"] = "public" if args.public else "archive"
    expected["anonymous"] = args.public
    if args.public:
        with helm_environment() as env:
            raw = manifest(expected["version"])
            archive = pull(
                args.helm, expected["version"], args.output, env, raw
            ).resolve()
            compare(args.archive, archive)
            if manifest(expected["version"]) != raw:
                raise ValueError("Public chart manifest changed during acceptance")
        expected["manifest_digest"] = "sha256:" + sha256(raw)
    expected["archive_sha256"] = sha256(archive.read_bytes())
    static_check(archive, args.helm, args.output / "checked")
    if args.validation_only:
        # Static checks are deliberately not an acceptance proof.
        return expected
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )
    if platform.system() != "Linux" or arch is None:
        raise ValueError("Distribution acceptance requires native Linux amd64/arm64")
    expected["platform"] = "linux/" + arch
    run("docker", "pull", f"{IMAGE}@{args.image_digest}")
    expected["suites"] = {}
    for name, options in SUITES.items():
        output = args.output / name
        run(
            sys.executable,
            ROOT / "scripts/helm-smoke.py",
            "--chart",
            archive,
            "--chart-default-image",
            "--helm",
            args.helm,
            "--artifacts",
            output,
            "--sandbox",
            "disabled" if name == "disabled" else "enabled",
            *options,
        )
        result = json.loads((output / "result.json").read_text())
        if result["result"] != "passed" or result["platform"] != expected["platform"]:
            raise ValueError("Missing successful native smoke result")
        if not result.get("default_image", {}).get("passed"):
            raise ValueError("Chart default image was not exercised")
        result["sandbox_evidence"] = json.loads(
            (output / "chromium-sandbox.json").read_text()
        )
        if not result.get("policy"):
            raise ValueError("Sandbox policy evidence is missing")
        expected["suites"][name] = result
    write_json(proof, expected)
    return expected


def validate_acceptance(record, item, stage, arch):
    if item["stage"] != stage or item["platform"] != "linux/" + arch:
        raise ValueError("Acceptance stage/platform mismatch")
    for key in (
        "version",
        "revision",
        "image_digest",
        "content_sha256",
        "chart",
    ):
        if item[key] != record[key]:
            raise ValueError("Acceptance identity differs from distribution")
    if stage == "public" and (
        not item["anonymous"]
        or any(
            item[key] != record[key] for key in ("manifest_digest", "archive_sha256")
        )
    ):
        raise ValueError("Anonymous chart identity differs")
    if set(item["suites"]) != set(SUITES):
        raise ValueError("Acceptance suites are incomplete")
    for name, result in item["suites"].items():
        if (
            result["result"] != "passed"
            or result["platform"] != item["platform"]
            or result["sandbox_mode"]
            != ("disabled" if name == "disabled" else "enabled")
            or not result.get("default_image", {}).get("passed")
        ):
            raise ValueError("Distribution runtime acceptance is incomplete")
        if name != "disabled" and (
            not result.get("policy") or not result.get("sandbox_evidence")
        ):
            raise ValueError("Enabled sandbox evidence is missing")
        if result["default_image"].get("image") != f"{IMAGE}@{record['image_digest']}":
            raise ValueError("Default-image acceptance identity differs")
        if result.get("sandbox_evidence", {}).get("no_sandbox") is not (
            name == "disabled"
        ):
            raise ValueError("Effective browser sandbox mode differs")
        if name == "network" and result.get("network", {}).get("result") != "passed":
            raise ValueError("Network acceptance is missing")


def validate_proof(record):
    for stage in ("archive", "public"):
        for arch in ("amd64", "arm64"):
            validate_acceptance(record, record["acceptance"][stage][arch], stage, arch)


def finalize(args):
    (args.output / "chart-distribution.json").unlink(missing_ok=True)
    expected = identity(args.archive, args.tag, args.revision, args.image_digest)
    expected["acceptance"] = {"archive": {}, "public": {}}
    for path in sorted(args.evidence.rglob("acceptance.json")):
        item = json.loads(path.read_text())
        stage, arch = item["stage"], item["platform"].removeprefix("linux/")
        if stage not in {"archive", "public"} or arch not in {"amd64", "arm64"}:
            raise ValueError("Unexpected acceptance stage/platform")
        if stage == "public":
            for key in ("manifest_digest", "archive_sha256"):
                if key in expected and expected[key] != item[key]:
                    raise ValueError(
                        "Registry identity differs between acceptance attempts"
                    )
                expected[key] = item[key]
        validate_acceptance(expected, item, stage, arch)
        # Failed-job reruns may reuse successful receipts from earlier attempts
        # of the same workflow run, but every receipt must match this candidate.
        expected["acceptance"][stage][arch] = item
    validate_proof(expected)
    write_json(args.output / "chart-distribution.json", expected)
    return {"record": str(args.output / "chart-distribution.json")}


def discover(args):
    revision = git("rev-parse", f"refs/tags/{args.tag}^{{commit}}")
    if args.revision and revision != args.revision:
        raise ValueError("Release tag revision differs")
    inspected = inspect_release(
        f"{IMAGE}:{release_version(args.tag)}", args.tag, revision
    )
    if args.image_digest and inspected["digest"] != args.image_digest:
        raise ValueError("Release image digest differs")
    supported = (
        subprocess.run(
            ["git", "cat-file", "-e", f"{revision}:scripts/release-charts.py"],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )
    return {
        "supported": str(supported).lower(),
        "revision": revision,
        "image_digest": inspected["digest"],
    }


def check(args):
    """Run the qualified checks against an explicit local directory or archive."""
    with chart_source(args.source) as source:
        version = metadata(source, args.tag)
        with tempfile.TemporaryDirectory(prefix="webcappi-local-chart-") as directory:
            chart = Path(directory) / "webcappi"
            shutil.copytree(source, chart)
            if args.local_image:
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]+", args.local_image):
                    raise ValueError("Invalid local image reference")
                if "@" in args.local_image:
                    repository, digest = args.local_image.split("@", 1)
                    if not DIGEST.fullmatch(digest):
                        raise ValueError("Invalid local image digest")
                    tag = ""
                else:
                    repository, separator, tag = args.local_image.rpartition(":")
                    if not separator or "/" in tag:
                        raise ValueError(
                            "Local image must have an explicit tag or digest"
                        )
                    digest = ""
                path = chart / "values.yaml"
                values = yaml.safe_load(path.read_text())
                values["image"].update(repository=repository, tag=tag, digest=digest)
                path.write_text(yaml.safe_dump(values, sort_keys=False))
            static_check(chart, args.helm, args.output / "checked")
    archive = args.output / "checked" / f"webcappi-{version}.tgz"
    # Preserve the actual supplied archive unless a local image copy was requested.
    target = args.source if args.source.is_file() and not args.local_image else archive
    if not args.validation_only:
        for name, options in SUITES.items():
            run(
                sys.executable,
                ROOT / "scripts/helm-smoke.py",
                "--chart",
                target,
                "--chart-default-image",
                "--helm",
                args.helm,
                "--artifacts",
                args.output / name,
                "--sandbox",
                "disabled" if name == "disabled" else "enabled",
                *options,
            )
    return {"archive": str(archive)}


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"Interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "prepare-version",
            "package",
            "publish",
            "accept",
            "finalize",
            "discover",
            "check",
        ),
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", default="")
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--source", type=Path, default=CHART)
    parser.add_argument("--source-revision", action="store_true")
    parser.add_argument("--local-image", default="")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--helm", default=str(HELM))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/chart-release")
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--validation-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    release_version(args.tag)
    if args.validation_only and args.command not in {"check", "accept"}:
        parser.error("--validation-only is supported only by check and accept")
    if args.dry_run and args.command != "prepare-version":
        parser.error("--dry-run is supported only by prepare-version")
    if args.local_image and args.command != "check":
        parser.error(
            "--local-image is supported only by check, not distribution publication"
        )
    if args.command == "finalize" and args.evidence is None:
        parser.error("--evidence is required")
    # Let context managers remove registry credentials and source copies on SIGTERM.
    signal.signal(signal.SIGTERM, interrupted)
    if args.command == "validate":
        result = {"version": metadata(args.source, args.tag)}
    elif args.command == "prepare-version":
        prepare_version(args.source, args.tag, args.dry_run)
        result = {}
    else:
        if args.command in {"publish", "accept", "finalize"} and args.archive is None:
            parser.error("--archive is required")
        result = {
            "package": package,
            "publish": publish,
            "accept": acceptance,
            "finalize": finalize,
            "discover": discover,
            "check": check,
        }[args.command](args)
    if args.github_output:
        with args.github_output.open("a") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
