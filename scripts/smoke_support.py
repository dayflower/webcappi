"""Shared Docker lifetimes and HTTP diagnostics for disposable smoke tests."""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, check=True, binary=False, timeout=60):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=not binary, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(
            f"docker {args!r} exited {result.returncode}: "
            f"{result.stderr!r}\n{result.stdout!r}"
        )
    return result.stdout if binary else result.stdout.strip()


def container_logs(name, include_stdout=True):
    result = subprocess.run(["docker", "logs", name], capture_output=True, timeout=10)
    output = result.stderr
    if include_stdout:
        output = result.stdout + output
    return output.decode(errors="replace")


@dataclass(frozen=True)
class Settings:
    image: str
    platform_args: tuple
    artifacts: Path

    @classmethod
    def from_env(cls):
        image = os.environ.get("WEBCAPPI_IMAGE", "webcappi:dev")
        platform = os.environ.get("PLATFORM")
        if platform and platform not in ("linux/amd64", "linux/arm64"):
            raise SystemExit(f"Unsupported test platform: {platform}")
        actual = docker(
            "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
        )
        if platform and actual != platform:
            raise SystemExit(f"Expected {platform} image, got {actual}")
        artifacts = Path(os.environ.get("WEBCAPPI_ARTIFACTS_DIR", ROOT / "artifacts"))
        if not artifacts.is_absolute():
            artifacts = ROOT / artifacts
        artifacts.mkdir(parents=True, exist_ok=True)
        return cls(image, ("--platform", platform) if platform else (), artifacts)


class HTTPClient:
    def __init__(self, base, authorization=None, timeout=15):
        self.base = base
        self.authorization = authorization
        self.timeout = timeout
        self.recent = deque(maxlen=12)

    def get(
        self,
        path,
        etag=None,
        authorization=...,
        data=None,
        content_type=None,
        timeout=None,
    ):
        headers = {"If-None-Match": etag} if etag else {}
        if authorization is ...:
            authorization = self.authorization
        if authorization is not None:
            headers["Authorization"] = authorization
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, headers=headers, data=data)
        try:
            try:
                response = urllib.request.urlopen(
                    request, timeout=self.timeout if timeout is None else timeout
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                detail = (
                    repr(body[:256]) if response.status >= 400 else f"{len(body)} bytes"
                )
                self.recent.append(
                    f"{request.get_method()} {path}: HTTP {response.status}, {detail}"
                )
                return response.status, response.headers, body
        except (OSError, http.client.HTTPException) as error:
            self.recent.append(f"{request.get_method()} {path}: {error!r}")
            raise

    def wait_ready(self, timeout=30, process=None):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"readiness deadline exceeded ({timeout}s)")
            if process is not None and process.process.poll() is not None:
                raise AssertionError(f"API exited during readiness: {process.logs()}")
            try:
                if (
                    self.get("/readyz", authorization=None, timeout=min(1, remaining))[
                        0
                    ]
                    == 200
                ):
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


class ExecProcess:
    """Drain exec output to files so a chatty API cannot block on a full pipe."""

    def __init__(self, args):
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                ["docker", *args], stdout=self.stdout, stderr=self.stderr
            )
        except BaseException:
            self.close()
            raise

    def logs(self):
        # pread leaves the file offsets shared with the child untouched.
        return "".join(
            os.pread(stream.fileno(), os.fstat(stream.fileno()).st_size, 0).decode(
                errors="replace"
            )
            for stream in (self.stdout, self.stderr)
        )

    def wait(self, timeout=15):
        self.process.wait(timeout=timeout)
        return self.process.returncode, self.logs()

    def close(self):
        self.stdout.close()
        self.stderr.close()


class DockerResources:
    """Register names before creation; release containers before their networks."""

    def __init__(self, settings):
        self.settings = settings
        self.containers = []
        self.binary_output = set()
        self.networks = []
        self.processes = []

    def __enter__(self):
        return self

    def network(self, name):
        self.networks.append(name)
        docker("network", "create", name)
        return name

    def run(self, name, *args, **kwargs):
        self.containers.append(name)
        if kwargs.get("binary"):
            self.binary_output.add(name)
        return docker(
            "run", *self.settings.platform_args, "--name", name, *args, **kwargs
        )

    def exec_process(self, name, *args):
        process = ExecProcess(("exec", name, *args))
        self.processes.append(process)
        return process

    def diagnostics(self):
        logs = []
        for name in self.containers:
            try:
                state = docker(
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    name,
                    check=False,
                    timeout=10,
                )
                output = container_logs(
                    name, include_stdout=name not in self.binary_output
                )
                logs.append(f"{name} state: {state}\n{output[-16000:]}")
            except Exception as error:
                logs.append(f"{name}: cannot collect logs: {error!r}")
        logs.extend(process.logs()[-16000:] for process in self.processes)
        return "\n".join(logs)

    def __exit__(self, exc_type, exc, traceback):
        failures = []
        for name in reversed(self.containers):
            try:
                docker("rm", "-f", name)
            except Exception as error:
                failures.append(str(error))
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except Exception as error:
                failures.append(str(error))
                try:
                    process.process.kill()
                    process.process.wait(timeout=5)
                except Exception as kill_error:
                    failures.append(str(kill_error))
            finally:
                process.close()
        for name in reversed(self.networks):
            try:
                docker("network", "rm", name)
            except Exception as error:
                failures.append(str(error))
        if failures:
            message = "smoke cleanup failed: " + "\n".join(failures)
            if exc is None:
                raise RuntimeError(message)
            print(message, file=sys.stderr)
        return False


@contextmanager
def scenario(name, resources, client=None):
    if client is not None:
        client.recent.clear()
    try:
        yield
    except Exception as error:
        print(f"FAIL [{name}]: {error!r}", file=sys.stderr)
        if client is not None:
            print("\n".join(client.recent), file=sys.stderr)
        try:
            print(resources.diagnostics(), file=sys.stderr)
        except Exception as diagnostic_error:
            print(f"diagnostics failed: {diagnostic_error!r}", file=sys.stderr)
        raise


def container_base(name):
    port = docker("port", name, "8080/tcp").rsplit(":", 1)[1]
    return f"http://127.0.0.1:{port}"
