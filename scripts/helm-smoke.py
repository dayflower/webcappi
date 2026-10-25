#!/usr/bin/env python3
"""Exercise the local chart in an owned kind cluster with an explicit sandbox mode."""

import argparse
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import runpy
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import uuid

from smoke_support import HTTPClient, PYTHON_IMAGE, REDIS_IMAGE, ROOT

TOOL_SETTINGS = runpy.run_path(str(ROOT / "scripts/chart-tools.py"))
TOOLS = ROOT / "bin/chart-tools"
NAMESPACE = "smoke"
TOKEN = "helm-smoke-token"


def run(*args, env=None, input=None, binary=False, check=True, timeout=180):
    result = subprocess.run(
        list(map(str, args)),
        input=input,
        env=env,
        capture_output=True,
        text=not binary,
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"{args!r}: exit {result.returncode}\n{result.stdout!r}\n{result.stderr!r}"
        )
    if not check and result.returncode:
        return None
    return result.stdout if binary else result.stdout.strip()


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"received signal {signum}")


@contextmanager
def image_aliases():
    aliases = []
    try:
        yield aliases
    finally:
        failures = []
        for image in reversed(aliases):
            try:
                if run("docker", "image", "inspect", image, check=False):
                    run("docker", "image", "rm", image)
            except Exception as error:
                failures.append(str(error))
        if failures:
            raise RuntimeError("image cleanup failed: " + "; ".join(failures))


class Cluster:
    def __init__(self, args, root):
        self.args = args
        self.root = root
        self.name = "webcappi-helm-" + uuid.uuid4().hex[:12]
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("HELM_")
            and k not in ("KUBECONFIG", "KIND_EXPERIMENTAL_DOCKER_NETWORK")
        }
        self.env.update(
            KUBECONFIG=str(root / "kubeconfig"),
            HELM_CONFIG_HOME=str(root / "helm/config"),
            HELM_CACHE_HOME=str(root / "helm/cache"),
            HELM_DATA_HOME=str(root / "helm/data"),
            HELM_REGISTRY_CONFIG=str(root / "helm/registry.json"),
            HELM_DRIVER="secret",
            KIND_EXPERIMENTAL_PROVIDER="docker",
        )

    def kube(self, *args, **kwargs):
        return run(
            self.args.kubectl, "--namespace", NAMESPACE, *args, env=self.env, **kwargs
        )

    def apply(self, *documents):
        self.kube(
            "apply",
            "-f",
            "-",
            input=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}),
        )

    def __enter__(self):
        try:
            print(f"Creating disposable cluster {self.name}", flush=True)
            run(
                self.args.kind,
                "create",
                "cluster",
                "--name",
                self.name,
                "--kubeconfig",
                self.env["KUBECONFIG"],
                "--image",
                TOOL_SETTINGS["NODE_IMAGE"],
                "--wait",
                "120s",
                env=self.env,
                timeout=240,
            )
            self.kube("create", "namespace", NAMESPACE)
            version = json.loads(self.kube("version", "-o", "json"))["serverVersion"][
                "gitVersion"
            ]
            assert version == "v" + TOOL_SETTINGS["KUBERNETES_VERSION"], version
            return self
        except BaseException:
            try:
                self.diagnostics()
            finally:
                self.cleanup()
            raise

    def load(self, *images):
        run(
            self.args.kind,
            "load",
            "docker-image",
            "--name",
            self.name,
            *images,
            env=self.env,
            timeout=240,
        )

    def diagnostics(self):
        self.args.artifacts.mkdir(parents=True, exist_ok=True)
        try:
            output = self.kube(
                "get",
                "deploy,pod,svc,events,configmap",
                "-o",
                "yaml",
                check=False,
                timeout=20,
            )
            (self.args.artifacts / "resources.yaml").write_text(output or "")
            pods = json.loads(
                self.kube("get", "pods", "-o", "json", check=False, timeout=10) or "{}"
            ).get("items", [])
            for pod in pods:
                name = pod["metadata"]["name"]
                for container in (
                    pod["spec"].get("initContainers", []) + pod["spec"]["containers"]
                ):
                    logs = self.kube(
                        "logs", name, "-c", container["name"], check=False, timeout=10
                    )
                    (
                        self.args.artifacts / f"{name}-{container['name']}.log"
                    ).write_text(logs or "")
        except Exception as error:
            (self.args.artifacts / "diagnostic-error.log").write_text(str(error))

    def cleanup(self):
        run(
            self.args.kind,
            "delete",
            "cluster",
            "--name",
            self.name,
            "--kubeconfig",
            self.env["KUBECONFIG"],
            env=self.env,
            timeout=120,
        )

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.diagnostics()
        finally:
            self.cleanup()
        return False

    def helm(self, *args, **kwargs):
        return run(
            self.args.helm, *args, "--namespace", NAMESPACE, env=self.env, **kwargs
        )

    def upgrade(self, release, values):
        path = self.root / f"{release}.json"
        path.write_text(json.dumps(values))
        self.helm(
            "upgrade",
            "--install",
            release,
            self.args.chart,
            "-f",
            path,
            "--wait",
            "--timeout",
            "180s",
            timeout=210,
        )
        self.kube(
            "rollout", "status", f"deployment/{release}-webcappi", "--timeout=90s"
        )

    def pods(self, release):
        pods = json.loads(
            self.kube(
                "get",
                "pods",
                "-l",
                f"app.kubernetes.io/instance={release}",
                "-o",
                "json",
            )
        )["items"]
        return [
            p["metadata"]["name"]
            for p in pods
            if not p["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
        ]

    def uninstall(self, release):
        self.helm("uninstall", release, "--wait", "--timeout", "60s")
        deadline = time.monotonic() + 60
        while json.loads(
            self.kube(
                "get",
                "pods",
                "-l",
                f"app.kubernetes.io/instance={release}",
                "-o",
                "json",
            )
        )["items"]:
            if time.monotonic() >= deadline:
                raise AssertionError(f"uninstall left Pods for {release}")
            time.sleep(0.2)

    @contextmanager
    def forward(self, resource, port=8080, token=TOKEN):
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(
                [
                    str(self.args.kubectl),
                    "--namespace",
                    NAMESPACE,
                    "port-forward",
                    resource,
                    f"0:{port}",
                    "--address",
                    "127.0.0.1",
                ],
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    output = os.pread(log.fileno(), 16384, 0).decode()
                    match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                    if match:
                        client = HTTPClient(
                            f"http://127.0.0.1:{match[1]}",
                            "Bearer " + token,
                            timeout=45,
                        )
                        yield client
                        return
                    if process.poll() is not None:
                        raise RuntimeError(f"port-forward exited: {output}")
                    time.sleep(0.1)
                raise RuntimeError(f"port-forward timed out: {output}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def fixture_resources(cluster):
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "api-auth"},
            "stringData": {"access-token": TOKEN},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "redis-auth"},
            "stringData": {"password": "redis-smoke-password"},
        },
    )
    for name, image, port, command in [
        (
            "fixture",
            PYTHON_IMAGE,
            8000,
            ["python", "-m", "http.server", "8000", "--directory", "/srv"],
        ),
        (
            "redis",
            REDIS_IMAGE,
            6379,
            [
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
                "--maxmemory",
                "64mb",
                "--maxmemory-policy",
                "noeviction",
                "--requirepass",
                "redis-smoke-password",
            ],
        ),
    ]:
        container = {
            "name": name,
            "image": image,
            "command": command,
            "ports": [{"containerPort": port}],
            "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 1},
        }
        pod = {"containers": [container]}
        if name == "fixture":
            cluster.apply(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "fixture"},
                    "data": {
                        p.name: p.read_text()
                        for p in (ROOT / "testdata/page").iterdir()
                        if p.is_file()
                    },
                }
            )
            container["volumeMounts"] = [
                {"name": "page", "mountPath": "/srv", "readOnly": True}
            ]
            pod["volumes"] = [{"name": "page", "configMap": {"name": "fixture"}}]
        cluster.apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"fixture": name}},
                    "template": {
                        "metadata": {"labels": {"fixture": name}},
                        "spec": pod,
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name},
                "spec": {
                    "selector": {"fixture": name},
                    "ports": [{"port": port, "targetPort": port}],
                },
            },
        )
        cluster.kube("rollout", "status", "deployment/" + name, "--timeout=120s")


def capture(client, path, artifacts, name):
    assert client.get("/healthz", authorization=None)[0] == 200
    assert client.get("/readyz", authorization=None)[0] == 200
    assert client.get(path, authorization=None)[0] == 401
    assert client.get(path, authorization="Bearer wrong")[0] == 401
    status, headers, body = client.get(path)
    assert status == 200, (status, body)
    assert (
        body.startswith(b"\x89PNG\r\n\x1a\n") and headers["Content-Type"] == "image/png"
    )
    (artifacts / f"{name}.png").write_bytes(body)
    assert headers["ETag"] and "max-age=" in headers["Cache-Control"]
    return headers["ETag"]


def started(cluster, pod):
    return cluster.kube("logs", pod, "-c", "api").count("capture started")


def image_values(image):
    if "@" in image:
        repository, digest = image.split("@", 1)
        return {
            "repository": repository,
            "tag": "",
            "digest": digest,
            "pullPolicy": "IfNotPresent",
        }
    repository, colon, tag = image.rpartition(":")
    if not colon or "/" in tag:
        raise ValueError("smoke image must have an explicit tag or digest")
    return {
        "repository": repository,
        "tag": tag,
        "digest": "",
        "pullPolicy": "IfNotPresent",
    }


def export_profile(cluster, pod, sandbox):
    wrapper = "/tmp/helm-chrome-test"
    browser_args = []
    try:
        if sandbox == "disabled":
            # init-profile has no config input; opt out only in this disposable test.
            cluster.kube(
                "exec",
                "-i",
                pod,
                "--",
                "sh",
                "-ec",
                f"cat > {wrapper}\nchmod 700 {wrapper}",
                input='#!/bin/sh\nexec /opt/chrome/chrome --no-sandbox "$@"\n',
            )
            browser_args = ["-browser-path", wrapper]
        cluster.kube(
            "exec",
            pod,
            "--",
            "webcappi",
            "init-profile",
            "-out",
            "/tmp/helm-profile.tar",
            "-work-dir",
            "/tmp/helm-profile-init",
            *browser_args,
        )
        return cluster.kube(
            "exec", pod, "--", "cat", "/tmp/helm-profile.tar", binary=True
        )
    finally:
        cluster.kube(
            "exec",
            pod,
            "--",
            "rm",
            "-rf",
            "/tmp/helm-profile.tar",
            "/tmp/helm-profile-init",
            *([wrapper] if sandbox == "disabled" else []),
        )


def prepare_bundle(cluster, pod, image, bundle_image, root, sandbox):
    data = export_profile(cluster, pod, sandbox)
    context = root / "bundle"
    context.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            assert (
                path.parts
                and path.parts[0] == "default"
                and ".." not in path.parts
                and not path.is_absolute()
            )
            assert member.isdir() or member.isfile()
            destination = context.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.write_bytes(archive.extractfile(member).read())
            destination.chmod(member.mode)
    (context / "default/.copy-check").write_text("hidden file\n")
    (context / "default/.copy-check").chmod(0o640)
    (context / "Dockerfile").write_text(
        f"FROM {image}\nCOPY --chown=10001:10001 default/ /bundle/default/\nUSER 10001:10001\n"
    )
    run("docker", "build", "--pull=false", "-t", bundle_image, context, timeout=180)
    cluster.load(bundle_image)
    return json.loads((context / "default/bundle.json").read_text())


def suite(cluster, args, root, aliases):
    cluster.load(args.image)
    fixture_resources(cluster)
    values = {
        "image": image_values(args.image),
        "browser": {"noSandbox": args.sandbox == "disabled"},
        "auth": {"existingSecret": "api-auth", "tokenKey": "access-token"},
        "capture": {"maxAgeSeconds": 3600},
    }
    path = "/capture?" + urllib.parse.urlencode(
        {
            "url": "http://fixture.smoke.svc.cluster.local:8000/",
            "width": 320,
            "height": 200,
            "ready_selector": "#page-ready",
        }
    )
    print(
        f"Checking memory installation and automatic profile (sandbox={args.sandbox})",
        flush=True,
    )
    cluster.upgrade("first", values)
    effective = json.loads(
        json.loads(
            cluster.kube("get", "configmap", "first-webcappi-config", "-o", "json")
        )["data"]["config.json"]
    )
    assert effective["no_sandbox"] is (args.sandbox == "disabled"), (
        "installed config does not match requested sandbox mode"
    )
    first_pod = cluster.pods("first")[0]
    with cluster.forward("service/first-webcappi") as client:
        capture(client, path, args.artifacts, "automatic")
    cluster.kube(
        "exec",
        first_pod,
        "--",
        "webcappi",
        "check",
        "-config",
        "/etc/webcappi/config.json",
    )
    assert (
        cluster.kube(
            "exec",
            first_pod,
            "--",
            "stat",
            "-Lc",
            "%u:%g %a",
            "/run/secrets/webcappi/token",
        )
        == "0:10001 440"
    )
    assert cluster.kube("exec", first_pod, "--", "id", "-u") == "10001"
    cluster.kube(
        "exec",
        first_pod,
        "--",
        "sh",
        "-ec",
        "test -w /work && test -w /tmp && test -w /dev/shm && ! test -w /etc/webcappi/config.json",
    )
    bundle_image = "webcappi-helm-bundle:" + cluster.name
    aliases.append(bundle_image)
    descriptor = prepare_bundle(
        cluster, first_pod, args.image, bundle_image, root, args.sandbox
    )
    cluster.upgrade("second", values)
    assert set(cluster.pods("first")).isdisjoint(cluster.pods("second"))
    with cluster.forward("service/second-webcappi") as client:
        capture(client, path, args.artifacts, "second-release")
    cluster.uninstall("second")
    assert not json.loads(
        cluster.kube(
            "get",
            "deployment,service,configmap",
            "-l",
            "app.kubernetes.io/instance=second",
            "-o",
            "json",
        )
    )["items"]
    values["capture"]["queueSize"] = 0
    cluster.upgrade("first", values)
    assert cluster.pods("first") != [first_pod], "config checksum did not restart Pod"
    cluster.kube("rollout", "restart", "deployment/first-webcappi")
    cluster.kube("rollout", "status", "deployment/first-webcappi", "--timeout=120s")
    with cluster.forward("service/first-webcappi") as client:
        capture(client, path, args.artifacts, "memory-upgrade")
    print(
        "PASS: memory install, two releases, authentication, probes, config update and restart",
        flush=True,
    )

    values["cache"] = {
        "backend": "redis",
        "redis": {
            "address": "redis:6379",
            "namespace": "helm-smoke",
            "generation": "capture-v1",
            "passwordSecret": {"name": "redis-auth", "key": "password"},
        },
    }
    cluster.upgrade("first", values)
    with cluster.forward("service/first-webcappi") as client:
        tag = capture(client, path, args.artifacts, "redis-one")
    values["replicaCount"] = 2
    cluster.upgrade("first", values)
    pods = cluster.pods("first")
    assert len(pods) == 2, pods
    for pod in pods:
        before = started(cluster, pod)
        with cluster.forward("pod/" + pod) as client:
            status, headers, _ = client.get(path, tag)
            assert status == 304 and "Age" in headers, (status, dict(headers))
        assert started(cluster, pod) == before, "metadata not shared across Pods"
    cluster.kube("rollout", "restart", "deployment/first-webcappi")
    cluster.kube("rollout", "status", "deployment/first-webcappi", "--timeout=120s")
    for pod in cluster.pods("first"):
        with cluster.forward("pod/" + pod) as client:
            assert client.get(path, tag)[0] == 304
        assert started(cluster, pod) == 0, "restart lost Redis metadata"
    with cluster.forward("pod/" + cluster.pods("first")[0]) as client:
        cluster.kube("scale", "deployment/redis", "--replicas=0")
        cluster.kube(
            "wait", "--for=delete", "pod", "-l", "fixture=redis", "--timeout=60s"
        )
        deadline = time.monotonic() + 15
        while client.get("/readyz", authorization=None)[0] != 503:
            if time.monotonic() >= deadline:
                raise AssertionError("Redis outage did not fail readiness")
            time.sleep(0.2)
        assert client.get("/healthz", authorization=None)[0] == 200
        cluster.kube("scale", "deployment/redis", "--replicas=1")
        cluster.kube("rollout", "status", "deployment/redis", "--timeout=90s")
        client.wait_ready(timeout=30)
        tag = capture(client, path, args.artifacts, "redis-recovery")
    print(
        "PASS: authenticated external Redis, scale, cross-Pod metadata, restart reuse and outage probes",
        flush=True,
    )

    image_alias = "webcappi:" + cluster.name
    aliases.append(image_alias)
    run("docker", "tag", args.image, image_alias)
    cluster.load(image_alias)
    values["image"] = image_values(image_alias)
    values["cache"]["redis"]["generation"] = "capture-v2"
    values["profiles"] = {
        "descriptors": {"default": descriptor},
        "bundle": image_values(bundle_image),
    }
    values["service"] = {"port": 9090}
    cluster.upgrade("first", values)
    # Fresh generation must execute Chromium, even for an old content-based ETag.
    pod = cluster.pods("first")[0]
    with cluster.forward("pod/" + pod) as client:
        assert client.get(path, tag)[0] in (200, 304)
    assert started(cluster, pod) == 1
    with cluster.forward("service/first-webcappi", port=9090) as client:
        capture(client, path, args.artifacts, "prepared-bundle")
    for pod in cluster.pods("first"):
        assert (
            cluster.kube(
                "exec",
                pod,
                "--",
                "stat",
                "-c",
                "%u:%g %a",
                "/bundles/default/.copy-check",
            )
            == "10001:10001 640"
        )
        before = cluster.kube(
            "exec",
            pod,
            "--",
            "sh",
            "-ec",
            "find /bundles -type f -exec sha256sum {} +",
        )
        with cluster.forward("pod/" + pod) as client:
            capture(client, path, args.artifacts, "bundle-" + pod)
        assert (
            cluster.kube(
                "exec",
                pod,
                "--",
                "sh",
                "-ec",
                "find /bundles -type f -exec sha256sum {} +",
            )
            == before
        )
        cluster.kube("exec", pod, "--", "sh", "-ec", "! touch /bundles/.must-not-write")
    revision = json.loads(cluster.helm("history", "first", "-o", "json"))[-1][
        "revision"
    ]
    next_bundle = "webcappi-helm-bundle:" + cluster.name + "-next"
    aliases.append(next_bundle)
    run("docker", "tag", bundle_image, next_bundle)
    cluster.load(next_bundle)
    values["profiles"]["bundle"] = image_values(next_bundle)
    values["cache"]["redis"]["generation"] = "capture-v3"
    cluster.upgrade("first", values)
    pod = cluster.pods("first")[0]
    with cluster.forward("pod/" + pod) as client:
        assert client.get(path, tag)[0] in (200, 304)
    assert started(cluster, pod) == 1, "bundle generation update reused metadata"
    with cluster.forward("service/first-webcappi", port=9090) as client:
        capture(client, path, args.artifacts, "bundle-upgrade")
    cluster.helm(
        "rollback", "first", str(revision), "--wait", "--timeout", "180s", timeout=210
    )
    config = json.loads(
        json.loads(
            cluster.kube("get", "configmap", "first-webcappi-config", "-o", "json")
        )["data"]["config.json"]
    )
    assert config["cache"]["redis"]["generation"] == "capture-v2"
    assert config["no_sandbox"] is (args.sandbox == "disabled")
    with cluster.forward("service/first-webcappi", port=9090) as client:
        capture(client, path, args.artifacts, "rollback")
    cluster.uninstall("first")
    assert not json.loads(
        cluster.kube(
            "get",
            "deployment,service,configmap,pod",
            "-l",
            "app.kubernetes.io/instance=first",
            "-o",
            "json",
        )
    )["items"]
    assert json.loads(cluster.kube("get", "secret", "api-auth", "-o", "json"))["data"]
    assert (
        json.loads(cluster.kube("get", "deployment", "redis", "-o", "json"))["spec"][
            "replicas"
        ]
        == 1
    )
    print(
        "PASS: immutable prepared bundles, image/bundle and generation updates, Service port, rollback, uninstall and external resource preservation",
        flush=True,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", default=os.environ.get("WEBCAPPI_IMAGE", "webcappi:dev")
    )
    parser.add_argument("--chart", type=Path, default=ROOT / "charts/webcappi")
    parser.add_argument(
        "--sandbox",
        choices=("enabled", "disabled"),
        default="enabled",
        help="browser sandbox mode; disabling is an explicit opt-out",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
    )
    for tool in ["helm", "kind", "kubectl"]:
        parser.add_argument("--" + tool, default=str(TOOLS / tool))
    args = parser.parse_args(argv)
    # Dockerfile FROM and kubectl arguments must receive one image reference.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]+", args.image):
        parser.error("invalid image reference")
    try:
        image_values(args.image)
    except ValueError as error:
        parser.error(str(error))
    if args.artifacts is None:
        args.artifacts = (
            Path(os.environ.get("WEBCAPPI_ARTIFACTS_DIR", ROOT / "artifacts"))
            / f"helm-{args.sandbox}"
        )
    args.artifacts = args.artifacts.resolve()
    return args


def write_result(args, result, error=None):
    report = {
        "result": result,
        "sandbox": args.sandbox == "enabled",
        "sandbox_mode": args.sandbox,
        "image": args.image,
        "kubernetes": TOOL_SETTINGS["KUBERNETES_VERSION"],
        "platform": args.runtime_platform,
    }
    if error is not None:
        report["error"] = str(error)
    (args.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    args = parse_args()
    native_arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )
    actual = run(
        "docker",
        "image",
        "inspect",
        args.image,
        "--format",
        "{{.Os}}/{{.Architecture}}",
    )
    if platform.system() != "Linux" or actual != "linux/" + str(native_arch):
        raise SystemExit(
            f"native Linux amd64/arm64 runtime required; image is {actual}, host is {platform.machine()}"
        )
    args.runtime_platform = actual
    args.artifacts.mkdir(parents=True, exist_ok=True)
    handlers = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with image_aliases() as aliases:
            with tempfile.TemporaryDirectory(prefix="webcappi-helm-") as temporary:
                with Cluster(args, Path(temporary)) as cluster:
                    suite(cluster, args, Path(temporary), aliases)
        write_result(args, "passed")
    except BaseException as error:
        write_result(args, "failed", error)
        raise
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
