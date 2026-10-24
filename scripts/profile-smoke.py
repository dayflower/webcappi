#!/usr/bin/env python3
"""Create a profile with the shipped CLI, import it and capture without extensions."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


def smoke_automatic(root, image, platform_args, bundles, profile, artifacts, prefix):
    for named in (False, True):
        name = prefix + ("-named" if named else "-automatic")
        config = root / (name + ".json")
        # Sandbox opt-out is limited to this disposable runtime test.
        cfg = dict(no_sandbox=True, work_dir="/work/captures")
        if named:
            cfg["profiles"] = dict(alternate=profile)
        config.write_text(json.dumps(cfg))
        process = None
        try:
            # Keep the container alive after the API exits, so deletion of its
            # template cannot be hidden by tmpfs/container teardown.
            docker("run", *platform_args, "-d", "--name", name,
                   "-p", "127.0.0.1::8080", "--read-only", "--shm-size=512m",
                   "--tmpfs", "/work:uid=10001,gid=10001,mode=700", "--tmpfs", "/tmp",
                   "-v", f"{bundles}:/bundles:ro", "-v", f"{config}:/etc/webcappi/config.json:ro",
                   image, "sleep", "infinity")
            process = subprocess.Popen([
                "docker", "exec", name, "sh", "-c",
                "echo $$ > /tmp/api.pid\nexec webcappi serve -config /etc/webcappi/config.json",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            port = docker("port", name, "8080/tcp").rsplit(":", 1)[1]
            base = f"http://127.0.0.1:{port}"
            for _ in range(300):
                try:
                    with urllib.request.urlopen(base + "/readyz", timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                if process.poll() is not None:
                    raise AssertionError(process.communicate()[1].decode(errors="replace"))
                time.sleep(0.1)
            else:
                raise AssertionError("automatic default failed startup")
            roots = docker("exec", name, "find", "/work/captures", "-mindepth", "1", "-maxdepth", "1").splitlines()
            assert len(roots) == 1 and Path(roots[0]).name.startswith("profile-"), roots
            template = roots[0] + "/bundle/default/user-data"
            before = sorted(docker("exec", name, "find", template, "-type", "f", "-exec", "sha256sum", "{}", "+").splitlines())
            for query in (["", "?profile=default", "?profile=alternate"] if named else ["", "?profile=default"]):
                request = urllib.request.Request(base + "/capture" + query, data=b"<p>Automatic default</p>",
                                                 headers={"Content-Type": "text/html; charset=utf-8"})
                with urllib.request.urlopen(request, timeout=35) as response:
                    png = response.read()
                    assert response.status == 200 and struct.unpack(">II", png[16:24]) == (1280, 720)
                    (artifacts / "automatic-default.png").write_bytes(png)
            after = sorted(docker("exec", name, "find", template, "-type", "f", "-exec", "sha256sum", "{}", "+").splitlines())
            assert before == after, "automatic template changed during capture"
            assert docker("exec", name, "find", "/work/captures", "-mindepth", "1", "-maxdepth", "1").splitlines() == roots
            try:
                urllib.request.urlopen(base + "/capture?url=http://example.invalid&profile=missing", timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError("unknown profile accepted")
            # Missing generated template files also make readiness fail.
            docker("exec", name, "rm", template + "/Local State")
            try:
                urllib.request.urlopen(base + "/readyz", timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 503
            else:
                raise AssertionError("missing automatic template considered ready")
            pid = docker("exec", name, "cat", "/tmp/api.pid")
            docker("exec", name, "sh", "-c", 'kill -TERM "$1"', "sh", pid)
            _, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert stderr.count(b"temporary default profile created") == 1, stderr
            assert docker("exec", name, "find", "/work/captures", "-mindepth", "1") == "", "SIGTERM leaked template"
            docker("exec", name, "webcappi", "check", "-config", "/etc/webcappi/config.json")
            assert docker("exec", name, "find", "/work/captures", "-mindepth", "1") == "", "check leaked template"
        finally:
            docker("rm", "-f", name, check=False)
            if process is not None:
                process.communicate(timeout=10)
    print("PASS: automatic default, named-only configuration, immutable template, readiness, SIGTERM/check cleanup")


def main():
    image = os.environ.get("WEBCAPPI_IMAGE", "webcappi:dev")
    platform = os.environ.get("PLATFORM")
    platform_args = ["--platform", platform] if platform else []
    artifacts = Path(os.environ.get("WEBCAPPI_ARTIFACTS_DIR", ROOT / "artifacts"))
    if not artifacts.is_absolute():
        artifacts = ROOT / artifacts
    artifacts.mkdir(parents=True, exist_ok=True)
    prefix = f"webcappi-profile-smoke-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        # Only this disposable test wrapper disables the browser sandbox.
        wrapper = root / "chrome-test"
        wrapper.write_text('#!/bin/sh\nexec /opt/chrome/chrome --no-sandbox "$@"\n')
        wrapper.chmod(0o755)
        try:
            result = subprocess.run([
                "docker", "run", *platform_args, "--rm", "--name", prefix + "-init",
                "--network=none", "--read-only", "--shm-size=512m",
                "--tmpfs", "/work:uid=10001,gid=10001,mode=700", "--tmpfs", "/tmp",
                "-v", f"{wrapper}:/test/chrome:ro", image,
                "webcappi", "init-profile", "-browser-path", "/test/chrome",
            ], capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            (artifacts / "default-profile.tar").write_bytes(result.stdout)
            bundles = root / "bundles"
            bundles.mkdir(mode=0o755)
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                for member in archive:
                    parts = PurePosixPath(member.name).parts
                    assert parts and parts[0] == "default" and ".." not in parts
                    assert member.isdir() or member.isfile(), member.name
                    assert member.uid == 10001 and member.gid == 10001
                    assert member.mode & 0o077 == 0, member.name
                    assert parts[-1] not in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort")
                    destination = bundles.joinpath(*parts)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True, mode=0o755)
                    else:
                        # These newly generated test inputs contain no credentials.
                        destination.write_bytes(archive.extractfile(member).read())
                        destination.chmod(0o644)
            profile = json.loads((bundles / "default/bundle.json").read_text())
            expected = dict(user_data_dir="/bundles/default/user-data", profile_directory="Default", extensions=[])
            assert profile == expected
            assert profile["extensions"] == [] and "ready_selectors" not in profile
            assert (bundles / "default/user-data/Local State").is_file()
            assert (bundles / "default/user-data/Default/Preferences").is_file()
            assert not list(bundles.joinpath("default/extensions").iterdir())
            cfg = dict(no_sandbox=True, work_dir="/work/captures", profiles={"default": profile})
            config = root / "config.json"
            config.write_text(json.dumps(cfg))
            docker("run", *platform_args, "-d", "--name", prefix + "-api",
                   "-p", "127.0.0.1::8080", "--read-only", "--shm-size=512m",
                   "--tmpfs", "/work:uid=10001,gid=10001,mode=700", "--tmpfs", "/tmp",
                   "-v", f"{bundles}:/bundles:ro", "-v", f"{config}:/etc/webcappi/config.json:ro", image)
            port = docker("port", prefix + "-api", "8080/tcp").rsplit(":", 1)[1]
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base + "/readyz", timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            else:
                logs = subprocess.run(["docker", "logs", prefix + "-api"], capture_output=True, text=True)
                raise AssertionError("generated profile failed startup: " + logs.stdout + logs.stderr)
            before = {p.relative_to(bundles): p.read_bytes() for p in bundles.rglob("*") if p.is_file()}
            request = urllib.request.Request(base + "/capture", data="<p>日本語: initialized profile</p>".encode(),
                                             headers={"Content-Type": "text/html; charset=utf-8"})
            with urllib.request.urlopen(request, timeout=35) as response:
                png = response.read()
                assert response.status == 200 and response.headers["Content-Type"] == "image/png"
                assert struct.unpack(">II", png[16:24]) == (1280, 720)
                (artifacts / "initialized-profile.png").write_bytes(png)
            after = {p.relative_to(bundles): p.read_bytes() for p in bundles.rglob("*") if p.is_file()}
            assert before == after, "imported template changed during capture"
            assert docker("exec", prefix + "-api", "find", "/work/captures", "-mindepth", "1") == ""
            print("PASS: offline headless init-profile, tar export/import, extension-free API capture, immutable template")
            smoke_automatic(root, image, platform_args, bundles, profile, artifacts, prefix)
        finally:
            docker("rm", "-f", prefix + "-init", check=False)
            docker("rm", "-f", prefix + "-api", check=False)


if __name__ == "__main__":
    main()
