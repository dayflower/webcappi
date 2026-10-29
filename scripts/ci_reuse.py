"""Select successful main-push CI artifacts without accepting PR or stale evidence."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode
import zipfile

from build_info import release_version
from ci_artifacts import validate_qualification
from release import inspect_release


class APIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api(path, binary=False):
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace")
        status = re.search(r"\(HTTP (\d{3})\)", message)
        raise APIError(message, int(status[1]) if status else None)
    return result.stdout if binary else json.loads(result.stdout)


def pages(path, key):
    result = []
    for page in range(1, 11):
        value = api(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        result.extend(value[key])
        if len(value[key]) < 100:
            return result
    raise ValueError("Actions query exceeds the bounded pagination limit")


def trusted_run(value, repository, revision, completed=True):
    if (
        value["event"] != "push"
        or value["head_branch"] != "main"
        or value["head_sha"] != revision
        or value["path"] != ".github/workflows/ci.yml"
        or value["repository"]["full_name"] != repository
        or value["head_repository"]["full_name"] != repository
    ):
        raise ValueError("CI run is not the matching repository main push")
    if completed and (
        value["status"] != "completed" or value["conclusion"] != "success"
    ):
        raise ValueError("CI attempt did not succeed")


def qualification(repository, artifact):
    if artifact["size_in_bytes"] > 1024 * 1024:
        raise ValueError("Qualification artifact is unexpectedly large")
    data = api(
        f"repos/{repository}/actions/artifacts/{artifact['id']}/zip", binary=True
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist() != ["qualification.json"]:
            raise ValueError("Unexpected qualification archive contents")
        entry = archive.getinfo("qualification.json")
        if entry.file_size > 1024 * 1024:
            raise ValueError("Qualification receipt is unexpectedly large")
        return json.loads(archive.read(entry))


def verify_attempt(repository, revision, run_id, attempt):
    value = api(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
    trusted_run(value, repository, revision)
    if value["id"] != run_id or value["run_attempt"] != attempt:
        raise ValueError("Successful CI attempt identity differs")


def select(run, args):
    repository = os.environ["GITHUB_REPOSITORY"]
    trusted_run(run, repository, args.revision)
    artifacts = pages(
        f"repos/{repository}/actions/runs/{run['id']}/artifacts", "artifacts"
    )
    receipts = [
        a
        for a in artifacts
        if a["name"] == f"release-qualification-{run['run_attempt']}"
    ]
    if not receipts:
        return {"mode": "fallback", "reason": "qualification artifact missing"}
    if len(receipts) != 1:
        raise ValueError("Ambiguous qualification artifacts")
    receipt = receipts[0]
    if receipt["expired"]:
        return {"mode": "fallback", "reason": "qualification artifact expired"}
    try:
        value = qualification(repository, receipt)
    except APIError as error:
        if error.status in (404, 410):
            return {"mode": "fallback", "reason": "qualification artifact removed"}
        raise
    candidate = validate_qualification(value, args.version, args.revision)
    if (
        value["repository"] != repository
        or value["run_id"] != run["id"]
        or value["run_attempt"] != run["run_attempt"]
    ):
        raise ValueError("Qualification run identity differs")
    bundles = [a for a in artifacts if a["id"] == value["bundle_id"]]
    if not bundles or bundles[0]["expired"]:
        return {"mode": "fallback", "reason": "candidate artifact missing or expired"}
    bundle = bundles[0]
    if bundle["name"] != f"release-candidate-{candidate['run_attempt']}":
        raise ValueError("Candidate artifact name/attempt differs")
    # Re-running a workflow must not silently substitute another attempt.
    verify_attempt(repository, args.revision, run["id"], run["run_attempt"])
    return {
        "mode": "reuse",
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "bundle_id": bundle["id"],
        "qualification_id": receipt["id"],
        "image_digest": candidate["image_digest"],
    }


def resolve(args):
    repository = os.environ["GITHUB_REPOSITORY"]
    published = inspect_release(
        "ghcr.io/dayflower/webcappi:" + release_version(args.version),
        args.version,
        args.revision,
        missing_ok=True,
    )
    if published is not None:
        return {"mode": "published", "image_digest": published["digest"]}
    if args.published_only:
        raise ValueError("Recovery requires an already published matching image")
    started = time.monotonic()
    while True:
        query = urlencode(
            {"event": "push", "branch": "main", "head_sha": args.revision}
        )
        runs = pages(
            f"repos/{repository}/actions/workflows/ci.yml/runs?{query}", "workflow_runs"
        )
        elapsed = time.monotonic() - started
        if not runs:
            if elapsed >= args.discovery_timeout:
                return {"mode": "fallback", "reason": "no matching main CI"}
        else:
            run = max(runs, key=lambda item: item["id"])
            trusted_run(run, repository, args.revision, completed=False)
            if run["status"] == "completed":
                if run["conclusion"] == "cancelled":
                    return {"mode": "fallback", "reason": "main CI cancelled"}
                if run["conclusion"] != "success":
                    raise ValueError(
                        "Matching main CI failed; refusing release fallback"
                    )
                return select(run, args)
        if elapsed >= args.wait_timeout:
            raise TimeoutError("Timed out waiting for matching main CI")
        print("Waiting for matching main push CI", flush=True)
        time.sleep(min(args.poll_interval, args.wait_timeout - elapsed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "verify-run"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--published-only", action="store_true")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--discovery-timeout", type=int, default=120)
    parser.add_argument("--wait-timeout", type=int, default=3600)
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    release_version(args.version)
    if args.command == "resolve":
        result = resolve(args)
    else:
        repository = os.environ["GITHUB_REPOSITORY"]
        verify_attempt(repository, args.revision, args.run_id, args.run_attempt)
        result = {}
    print(json.dumps(result, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
