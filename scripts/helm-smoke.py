#!/usr/bin/env python3
"""Exercise the local chart in an owned kind cluster with an explicit sandbox mode."""

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import runpy
import signal
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import uuid

from smoke_support import HTTPClient, PYTHON_IMAGE, REDIS_IMAGE, ROOT
from sandbox.sandbox_support import POLICY, SandboxPolicy

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
        self.policy = None
        if getattr(args, "sandbox", "disabled") == "enabled":
            self.policy = SandboxPolicy(self.name, root, run, TOOLS / "apparmor_parser")
            args.policy_record = self.policy.record
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
            extra = []
            if self.policy:
                path = self.root / "kind.json"
                path.write_text(json.dumps(self.policy.configuration()))
                extra = ["--config", path]
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
                *extra,
                env=self.env,
                timeout=240,
            )
            if self.policy:
                self.policy.install()
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
            if getattr(self.args, "network_only", False):
                resources = self.kube(
                    "get",
                    "ingress,httproute,gateway,gatewayclass,deploy,pod,svc,events",
                    "--all-namespaces",
                    "-o",
                    "yaml",
                    check=False,
                    timeout=30,
                )
                (self.args.artifacts / "network-resources.yaml").write_text(
                    resources or ""
                )
                pods = json.loads(
                    self.kube("get", "pods", "--all-namespaces", "-o", "json")
                )["items"]
                for pod in pods:
                    namespace = pod["metadata"]["namespace"]
                    if namespace not in ("network-system", "envoy-gateway-system"):
                        continue
                    name = pod["metadata"]["name"]
                    logs = self.kube(
                        "logs",
                        name,
                        "--namespace",
                        namespace,
                        "--all-containers",
                        check=False,
                        timeout=10,
                    )
                    (self.args.artifacts / f"{namespace}-{name}.log").write_text(
                        logs or ""
                    )
        except Exception as error:
            (self.args.artifacts / "diagnostic-error.log").write_text(str(error))

    def cleanup(self):
        try:
            if self.policy:
                self.policy.cleanup()
        finally:
            self.delete_cluster()

    def delete_cluster(self):
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
    def forward(self, resource, port=8080, token=TOKEN, namespace=NAMESPACE):
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(
                [
                    str(self.args.kubectl),
                    "--namespace",
                    namespace,
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
                if getattr(self.args, "network_only", False):
                    path = self.args.artifacts / (
                        "forward-"
                        + namespace
                        + "-"
                        + resource.replace("/", "-")
                        + "-"
                        + uuid.uuid4().hex[:8]
                        + ".log"
                    )
                    path.write_text(
                        os.pread(log.fileno(), 65536, 0).decode(errors="replace")
                    )


def fixture_resources(cluster):
    if getattr(cluster, "fixtures_ready", False) is True:
        return
    certificate = cluster.root / "redis.crt"
    key = cluster.root / "redis.key"
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        key,
        "-out",
        certificate,
        "-days",
        "1",
        "-subj",
        "/CN=redis",
        "-addext",
        "subjectAltName=DNS:redis",
    )
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
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "redis-tls"},
            "stringData": {
                "tls.crt": certificate.read_text(),
                "tls.key": key.read_text(),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "redis-ca"},
            "data": {"ca.crt": certificate.read_text()},
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
                "--tls-port",
                "6380",
                "--tls-cert-file",
                "/tls/tls.crt",
                "--tls-key-file",
                "/tls/tls.key",
                "--tls-ca-cert-file",
                "/tls/tls.crt",
                "--tls-auth-clients",
                "no",
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
        ports = [{"name": "main", "port": port, "targetPort": port}]
        if name == "redis":
            container["volumeMounts"] = [
                {"name": "tls", "mountPath": "/tls", "readOnly": True}
            ]
            pod["volumes"] = [{"name": "tls", "secret": {"secretName": "redis-tls"}}]
            ports.append({"name": "tls", "port": 6380, "targetPort": 6380})
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
                    "ports": ports,
                },
            },
        )
        cluster.kube("rollout", "status", "deployment/" + name, "--timeout=120s")
    cluster.fixtures_ready = True


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


def docker_policy_diagnosis(cluster, args):
    cases = [("runtime-default", [])]
    seccomp = ["--security-opt", f"seccomp={POLICY / 'chrome-seccomp.json'}"]
    cases.append(("seccomp-only", seccomp))
    if cluster.policy.apparmor:
        cases.append(
            (
                "seccomp-and-apparmor",
                [*seccomp, "--security-opt", "apparmor=" + cluster.policy.name],
            )
        )
    records = []
    for label, policy in cases:
        name = cluster.name + "-" + label
        try:
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,mode=1777",
                "--tmpfs",
                "/work:rw,uid=10001,gid=10001,mode=700",
                *policy,
                "--entrypoint",
                "/opt/chrome/chrome",
                args.image,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--user-data-dir=/tmp/diagnose",
                "--dump-dom",
                "about:blank",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            records.append(
                {
                    "case": label,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            if label == cases[-1][0]:
                assert result.returncode == 0, records[-1]
        finally:
            run("docker", "rm", "-f", name, check=False)
    (args.artifacts / "docker-layers.json").write_text(
        json.dumps(records, indent=2) + "\n"
    )


def diagnostic_image(args, root, aliases):
    context = root / "diagnostic-image"
    context.mkdir()
    shutil.copytree(ROOT / "testdata", context / "testdata")
    (context / "capture-cwd").mkdir()
    (context / "capture-cwd/.keep").touch()
    run(
        *shlex.split(os.environ.get("GO", "go")),
        "build",
        "-o",
        context / "webcappi-sandbox-check",
        ROOT / "scripts/sandbox/sandbox-check.go",
        ROOT / "scripts/sandbox/sandbox-process.go",
        env=dict(os.environ, CGO_ENABLED="0"),
    )
    run(
        *shlex.split(os.environ.get("GO", "go")),
        "test",
        "-c",
        "./internal/capture",
        "-o",
        context / "webcappi-capture-test",
        env=dict(os.environ, CGO_ENABLED="0"),
    )
    run(
        "cc",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-static",
        "-o",
        context / "webcappi-policy-probe",
        ROOT / "scripts/sandbox/policy-probe.c",
    )
    (context / "Dockerfile").write_text(
        f"FROM {args.image}\n"
        "COPY --chmod=0555 --chown=0:0 webcappi-sandbox-check webcappi-policy-probe webcappi-capture-test /usr/local/bin/\n"
        "COPY --chown=0:0 testdata /bundles/test-source/testdata/\n"
        "COPY --chown=0:0 capture-cwd /bundles/test-source/internal/capture/\n"
    )
    image = "webcappi-helm-diagnostic:" + uuid.uuid4().hex[:12]
    aliases.append(image)
    run("docker", "build", "--pull=false", "-t", image, context, timeout=180)
    args.source_image = args.image
    args.image = image
    args.image_record = {
        "source_ref": args.source_image,
        "source_id": run(
            "docker", "image", "inspect", args.source_image, "--format", "{{.Id}}"
        ),
        "diagnostic_ref": image,
        "diagnostic_id": run(
            "docker", "image", "inspect", image, "--format", "{{.Id}}"
        ),
        "diagnostics_sha256": {
            name: hashlib.sha256((context / name).read_bytes()).hexdigest()
            for name in (
                "webcappi-sandbox-check",
                "webcappi-policy-probe",
                "webcappi-capture-test",
            )
        },
    }


def docker_browser_policy_tests(cluster, args):
    arch = args.runtime_platform.split("/")[1]
    headed_image = f"webcappi-test-browser:156.0.8078.47-{arch}"
    if run("docker", "image", "inspect", headed_image, check=False) is None:
        run(
            "docker",
            "buildx",
            "build",
            "--load",
            "--target",
            "test-browser",
            "-t",
            headed_image,
            ROOT,
            timeout=600,
        )
    name = cluster.name + "-extension-preparation"
    prepared = cluster.root / "prepared-extension"
    prepared.mkdir()
    try:
        # Headed fixture installation is separate from sandbox-enabled acceptance.
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--shm-size=512m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/work:uid=10001,gid=10001,mode=700",
            "--tmpfs",
            "/tmp",
            "-v",
            f"{cluster.root / 'diagnostic-image/webcappi-capture-test'}:/app/capture.test:ro",
            "-v",
            f"{cluster.root / 'diagnostic-image/testdata'}:/bundles/test-source/testdata:ro",
            "-w",
            "/bundles/test-source/internal/capture",
            "-e",
            "WEBCAPPI_TEST_BROWSER=/opt/chrome/chrome",
            "-e",
            "WEBCAPPI_TEST_NO_SANDBOX=1",
            "-e",
            "WEBCAPPI_TEST_ARTIFACTS=/home/webcappi",
            headed_image,
            "xvfb-run",
            "-a",
            "/app/capture.test",
            "-test.run=^TestExtensionProfileLifecycle$",
            "-test.v",
            "-test.timeout=180s",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=210)
        (args.artifacts / "extension-fixture-preparation.json").write_text(
            json.dumps(
                {
                    "sandbox": False,
                    "purpose": "external headed fixture preparation",
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
            + "\n"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        run("docker", "cp", name + ":/home/webcappi/user-data", prepared)
    finally:
        run("docker", "rm", "-f", name, check=False)
    for path in [prepared, *prepared.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    name = cluster.name + "-confined-browser-tests"
    try:
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--security-opt",
            f"seccomp={POLICY / 'chrome-seccomp.json'}",
            "--shm-size=512m",
            "--tmpfs",
            "/work:uid=10001,gid=10001,mode=700",
            "--tmpfs",
            "/tmp:mode=1777",
            "-v",
            f"{prepared}:/bundles/prepared-extension:ro",
            "-w",
            "/bundles/test-source/internal/capture",
            "-e",
            "WEBCAPPI_TEST_BROWSER=/opt/chrome/chrome",
            "-e",
            "WEBCAPPI_TEST_NO_SANDBOX=0",
            "-e",
            "WEBCAPPI_TEST_PREPARED_USER_DATA=/bundles/prepared-extension/user-data",
        ]
        if cluster.policy.apparmor:
            command += ["--security-opt", "apparmor=" + cluster.policy.name]
        command += [
            "--entrypoint",
            "/usr/local/bin/webcappi-capture-test",
            args.image,
            "-test.run=^Test(ExtensionProfileLifecycle|CaptureFullPage|RenderedFontFamilies|MultilingualRenderedFonts|LoopbackHTMLCharset|HTMLCapture|ProfileReadySelectors|TemporaryProfileLifecycle|NewProfileCleansUpFailedStartup)$",
            "-test.v",
            "-test.timeout=180s",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=210)
        (args.artifacts / "confined-browser-tests.json").write_text(
            json.dumps(
                {
                    "sandbox": True,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
            + "\n"
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        run("docker", "rm", "-f", name, check=False)


def docker_policy_denials(cluster, args):
    canary = cluster.root / "outside-canary"
    canary.write_text("independent policy validation\n")
    canary.chmod(0o666)
    common = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        f"seccomp={POLICY / 'chrome-seccomp.json'}",
        "--tmpfs",
        "/work:rw,exec,uid=10001,gid=10001,mode=700",
        "--tmpfs",
        "/tmp:rw,exec,mode=1777",
        "--tmpfs",
        "/dev/shm:rw,exec,mode=1777,size=256m",
        "--mount",
        f"type=bind,source={canary},target=/review-deny/canary",
    ]
    baseline = (
        ": >> /review-deny/canary; "
        "for area in /work /tmp /dev/shm; do "
        'cp /usr/local/bin/webcappi-policy-probe "$area/baseline"; '
        'if "$area/baseline" --execution-canary; then exit 1; '
        'else test "$?" = 42; fi; rm "$area/baseline"; done'
    )
    cases = [
        (
            "runtime-default-apparmor-baseline",
            ["--entrypoint", "/bin/sh"],
            ["-ec", baseline],
        ),
        (
            "application-policy",
            [
                "--security-opt",
                "apparmor=" + cluster.policy.name,
                "--entrypoint",
                "/usr/local/bin/webcappi-policy-probe",
            ],
            ["--canary", "/review-deny/canary"],
        ),
    ]
    records = []
    for label, options, command in cases:
        name = cluster.name + "-negative-" + label
        try:
            argv = [*common, "--name", name, *options, args.image, *command]
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
            )
            records.append(
                {
                    "case": label,
                    "command": argv,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            (args.artifacts / "docker-policy-denials.json").write_text(
                json.dumps(records, indent=2) + "\n"
            )
            assert result.returncode == 0, records[-1]
        finally:
            run("docker", "rm", "-f", name, check=False)


def sandbox_evidence(cluster, pod, args):
    output = cluster.kube(
        "exec", pod, "--", "/usr/local/bin/webcappi-sandbox-check", args.sandbox
    )
    evidence = json.loads(output)
    status = cluster.kube("exec", pod, "--", "cat", "/proc/self/status")
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert fields["NoNewPrivs"].strip() == "1"
    assert int(fields["CapEff"].strip(), 16) == 0
    evidence["container_status"] = {
        key: fields[key].strip() for key in ("NoNewPrivs", "CapEff", "Seccomp")
    }
    if cluster.policy and cluster.policy.apparmor:
        label = cluster.kube("exec", pod, "--", "cat", "/proc/self/attr/current")
        assert label == cluster.policy.name + " (enforce)", label
        evidence["apparmor_label"] = label
        output = cluster.kube("exec", pod, "--", "/usr/local/bin/webcappi-policy-probe")
        (args.artifacts / "policy-probes.log").write_text(output + "\n")
    assert evidence["no_sandbox"] is (args.sandbox == "disabled")
    (args.artifacts / "chromium-sandbox.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )


def policy_failures(cluster, args):
    spec = json.loads(
        cluster.kube("get", "deployment", "first-webcappi", "-o", "json")
    )["spec"]["template"]["spec"]
    cases = [
        (
            "missing-seccomp",
            "seccompProfile",
            {"type": "Localhost", "localhostProfile": "webcappi/missing.json"},
        ),
        ("incompatible-seccomp", "seccompProfile", {"type": "RuntimeDefault"}),
    ]
    if cluster.policy.apparmor:
        cases.append(
            (
                "incompatible-apparmor",
                "appArmorProfile",
                cluster.policy.incompatible_apparmor(),
            )
        )
        cases.append(
            (
                "missing-apparmor",
                "appArmorProfile",
                {
                    "type": "Localhost",
                    "localhostProfile": cluster.policy.name + "-missing",
                },
            )
        )
    for name, field, profile in cases:
        pod_spec = json.loads(json.dumps(spec))
        pod_spec["restartPolicy"] = "Never"
        api = pod_spec["containers"][0]
        api["securityContext"][field] = profile
        api["command"] = ["/opt/chrome/chrome"]
        api["args"] = [
            "--headless=new",
            "--no-first-run",
            "--disable-gpu",
            "--user-data-dir=/tmp/failure",
            "--dump-dom",
            "about:blank",
        ]
        for key in ("startupProbe", "readinessProbe", "livenessProbe"):
            api.pop(key, None)
        cluster.apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name},
                "spec": pod_spec,
            }
        )
        try:
            deadline = time.monotonic() + 45
            while True:
                pod = json.loads(cluster.kube("get", "pod", name, "-o", "json"))
                status = pod.get("status", {})
                states = [
                    c.get("state", {}) for c in status.get("containerStatuses", [])
                ]
                if status.get("phase") == "Succeeded":
                    raise AssertionError(f"{name} unexpectedly ran Chrome successfully")
                rejected = status.get("phase") == "Failed" or any(
                    s.get("waiting", {}).get("reason")
                    in ("CreateContainerError", "CreateContainerConfigError")
                    for s in states
                )
                if rejected:
                    logs = cluster.kube("logs", name, check=False) or ""
                    if name.startswith("incompatible-"):
                        assert (
                            "No usable sandbox" in logs
                            or "Operation not permitted" in logs
                            or "Permission denied" in logs
                        ), logs
                    (args.artifacts / (name + ".json")).write_text(
                        json.dumps(
                            {
                                "status": status,
                                "logs": logs,
                                "browser_arguments": api["args"],
                                "no_sandbox": False,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"{name} did not produce a useful policy rejection: {status}"
                    )
                time.sleep(0.5)
        finally:
            cluster.kube("delete", "pod", name, "--wait=true", "--timeout=45s")
    print(
        "PASS: missing/incompatible policy fails without an unsandboxed retry",
        flush=True,
    )


def suite(cluster, args, root, aliases):
    cluster.load(args.image)
    info = json.loads(cluster.kube("get", "nodes", "-o", "json"))["items"][0]["status"][
        "nodeInfo"
    ]
    args.runtime_record = {
        key: info[key]
        for key in (
            "architecture",
            "osImage",
            "kernelVersion",
            "containerRuntimeVersion",
            "kubeletVersion",
        )
    }
    if not cluster.policy:
        args.policy_record = {
            "preparation": "none",
            "seccomp": "RuntimeDefault",
            "apparmor": "runtime default",
        }
    if cluster.policy:
        docker_policy_diagnosis(cluster, args)
        docker_browser_policy_tests(cluster, args)
        if cluster.policy.apparmor:
            docker_policy_denials(cluster, args)
    fixture_resources(cluster)
    values = {
        "image": image_values(args.image),
        "browser": {
            "sandbox": args.sandbox == "enabled",
            **(cluster.policy.values if cluster.policy else {}),
        },
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
    assert effective["sandbox"] is (args.sandbox == "enabled"), (
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
    sandbox_evidence(cluster, first_pod, args)
    if cluster.policy:
        policy_failures(cluster, args)
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
    values["cache"]["redis"].update(
        address="redis:6380",
        tls=True,
        ca={"kind": "ConfigMap", "name": "redis-ca", "key": "ca.crt"},
    )
    cluster.upgrade("first", values)
    with cluster.forward("service/first-webcappi") as client:
        tag = capture(client, path, args.artifacts, "redis-tls")
    pod = cluster.pods("first")[0]
    invalid = json.loads(
        cluster.kube("exec", pod, "--", "cat", "/etc/webcappi/config.json")
    )
    invalid["cache"]["redis"]["address"] = "redis.smoke.svc:6380"
    try:
        cluster.kube(
            "exec",
            "-i",
            pod,
            "--",
            "sh",
            "-ec",
            "cat > /tmp/invalid-redis-tls.json",
            input=json.dumps(invalid),
        )
        result = subprocess.run(
            [
                str(cluster.args.kubectl),
                "--namespace",
                NAMESPACE,
                "exec",
                pod,
                "--",
                "webcappi",
                "check",
                "-config",
                "/tmp/invalid-redis-tls.json",
            ],
            env=cluster.env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        record = {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        (args.artifacts / "redis-tls-hostname-rejection.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        assert result.returncode != 0 and "certificate" in (
            result.stdout + result.stderr
        ), record
    finally:
        cluster.kube("exec", pod, "--", "rm", "-f", "/tmp/invalid-redis-tls.json")
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
        "PASS: authenticated plain/TLS Redis, custom CA, hostname rejection, scale, shared metadata, restart and outage probes",
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
        cluster.kube(
            "exec", pod, "--", "sh", "-ec", "! ( : > /bundles/.must-not-write )"
        )
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
    assert config["sandbox"] is (args.sandbox == "enabled")
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--network-only",
        action="store_true",
        help="run real Ingress/Gateway acceptance with the sandbox enabled",
    )
    mode.add_argument(
        "--distribution-only",
        action="store_true",
        help="only check distribution defaults and the enabled Chromium sandbox",
    )
    parser.add_argument("--chart", type=Path, default=ROOT / "charts/webcappi")
    parser.add_argument(
        "--chart-default-image",
        action="store_true",
        help="also install and capture with the chart's unmodified default image",
    )
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
    if args.distribution_only and (
        not args.chart_default_image or args.sandbox != "enabled"
    ):
        parser.error(
            "distribution smoke requires --chart-default-image and --sandbox=enabled"
        )
    if args.chart_default_image:
        import yaml

        defaults = yaml.safe_load(run(args.helm, "show", "values", args.chart))
        image = defaults["image"]
        args.image = (
            f"{image['repository']}@{image['digest']}"
            if image.get("digest")
            else f"{image['repository']}:{image['tag']}"
        )
    if args.network_only and args.sandbox != "enabled":
        parser.error("network acceptance requires --sandbox=enabled")
    # Dockerfile FROM and kubectl arguments must receive one image reference.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]+", args.image):
        parser.error("invalid image reference")
    try:
        image_values(args.image)
    except ValueError as error:
        parser.error(str(error))
    if args.artifacts is None:
        args.artifacts = Path(
            os.environ.get("WEBCAPPI_ARTIFACTS_DIR", ROOT / "artifacts")
        ) / (
            "helm-distribution"
            if args.distribution_only
            else "helm-network"
            if args.network_only
            else f"helm-{args.sandbox}"
        )
    args.artifacts = args.artifacts.resolve()
    return args


def write_result(args, result, error=None):
    report = {
        "result": result,
        "sandbox": args.sandbox == "enabled",
        "sandbox_mode": args.sandbox,
        "image": args.image,
        "source_image": getattr(args, "source_image", args.image),
        "kubernetes": TOOL_SETTINGS["KUBERNETES_VERSION"],
        "platform": args.runtime_platform,
        "suite": (
            "distribution"
            if getattr(args, "distribution_only", False)
            else "network"
            if getattr(args, "network_only", False)
            else "lifecycle"
        ),
    }
    report["network"] = getattr(args, "network_record", None)
    report["policy"] = getattr(args, "policy_record", None)
    report["images"] = getattr(args, "image_record", None)
    report["environment"] = getattr(args, "runtime_record", None)
    report["default_image"] = getattr(args, "default_image_record", None)
    if error is not None:
        report["error"] = str(error)
    (args.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def default_image_capture(cluster, args):
    """Exercise the exact distribution default before diagnostic-image overrides."""
    source = args.source_image
    if "@" not in source:
        cluster.load(source)
    fixture_resources(cluster)
    values = {
        "auth": {"existingSecret": "api-auth", "tokenKey": "access-token"},
        "browser": {
            "sandbox": args.sandbox == "enabled",
            **(cluster.policy.values if cluster.policy else {}),
        },
    }
    cluster.upgrade("distribution", values)
    pod = cluster.pods("distribution")[0]
    data = json.loads(cluster.kube("get", "pod", pod, "-o", "json"))
    container = next(c for c in data["spec"]["containers"] if c["name"] == "api")
    assert container["image"] == source, "chart default image was overridden"
    path = "/capture?" + urllib.parse.urlencode(
        {
            "url": "http://fixture.smoke.svc.cluster.local:8000/",
            "width": 320,
            "height": 200,
            "ready_selector": "#page-ready",
        }
    )
    with cluster.forward("service/distribution-webcappi") as client:
        capture(client, path, args.artifacts, "distribution-default")
    status = next(c for c in data["status"]["containerStatuses"] if c["name"] == "api")
    args.default_image_record = {
        "passed": True,
        "image": source,
        "image_id": status["imageID"],
    }
    cluster.helm("uninstall", "distribution", "--wait", "--timeout", "90s")


def distribution_smoke(cluster, args):
    """Check active sandbox evidence separately from the standard-image capture."""
    cluster.load(args.image)
    cluster.upgrade(
        "sandbox",
        {
            "image": image_values(args.image),
            "browser": {"sandbox": True, **cluster.policy.values},
            "auth": {"existingSecret": "api-auth", "tokenKey": "access-token"},
        },
    )
    sandbox_evidence(cluster, cluster.pods("sandbox")[0], args)
    cluster.uninstall("sandbox")


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
                diagnostic_image(args, Path(temporary), aliases)
                with Cluster(args, Path(temporary)) as cluster:
                    if args.chart_default_image:
                        default_image_capture(cluster, args)
                    if args.distribution_only:
                        distribution_smoke(cluster, args)
                    elif args.network_only:
                        from helm_network import network_suite

                        network_suite(
                            cluster,
                            args,
                            run,
                            capture,
                            sandbox_evidence,
                            fixture_resources,
                            image_values,
                            TOKEN,
                        )
                    else:
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
