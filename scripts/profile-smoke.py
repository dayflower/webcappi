#!/usr/bin/env python3
"""Create a profile with the shipped CLI, import it and capture without extensions."""

import io
import json
import os
from pathlib import Path, PurePosixPath
import struct
import tarfile
import tempfile

from smoke_support import (
    DockerResources,
    HTTPClient,
    Settings,
    container_base,
    docker,
    scenario,
)


def smoke_automatic(root, settings, bundles, profile, prefix):
    for named in (False, True):
        name = prefix + ("-named" if named else "-automatic")
        config = root / (name + ".json")
        # Sandbox opt-out is limited to this disposable runtime test.
        cfg = dict(sandbox=False, work_dir="/work/captures")
        if named:
            cfg["profiles"] = dict(alternate=profile)
        config.write_text(json.dumps(cfg))
        client = HTTPClient("", timeout=35)
        with DockerResources(settings) as resources:
            with scenario(
                "named-only profile" if named else "automatic default profile",
                resources,
                client,
            ):
                # Keep the container alive after the API exits, so deletion of its
                # template cannot be hidden by tmpfs/container teardown.
                resources.run(
                    name,
                    "-d",
                    "-p",
                    "127.0.0.1::8080",
                    "--read-only",
                    "--shm-size=512m",
                    "--tmpfs",
                    "/work:uid=10001,gid=10001,mode=700",
                    "--tmpfs",
                    "/tmp",
                    "-v",
                    f"{bundles}:/bundles:ro",
                    "-v",
                    f"{config}:/etc/webcappi/config.json:ro",
                    settings.image,
                    "sleep",
                    "infinity",
                )
                process = resources.exec_process(
                    name,
                    "sh",
                    "-c",
                    "echo $$ > /tmp/api.pid\nexec webcappi serve -config /etc/webcappi/config.json",
                )
                client.base = container_base(name)
                client.wait_ready(process=process)
                roots = docker(
                    "exec",
                    name,
                    "find",
                    "/work/captures",
                    "-mindepth",
                    "1",
                    "-maxdepth",
                    "1",
                ).splitlines()
                assert len(roots) == 1 and Path(roots[0]).name.startswith("profile-"), (
                    roots
                )
                template = roots[0] + "/bundle/default/user-data"
                before = sorted(
                    docker(
                        "exec",
                        name,
                        "find",
                        template,
                        "-type",
                        "f",
                        "-exec",
                        "sha256sum",
                        "{}",
                        "+",
                    ).splitlines()
                )
                for query in (
                    ["", "?profile=default", "?profile=alternate"]
                    if named
                    else ["", "?profile=default"]
                ):
                    status, _, png = client.get(
                        "/capture" + query,
                        data=b"<p>Automatic default</p>",
                        content_type="text/html; charset=utf-8",
                    )
                    assert status == 200 and struct.unpack(">II", png[16:24]) == (
                        1280,
                        720,
                    )
                    (settings.artifacts / "automatic-default.png").write_bytes(png)
                after = sorted(
                    docker(
                        "exec",
                        name,
                        "find",
                        template,
                        "-type",
                        "f",
                        "-exec",
                        "sha256sum",
                        "{}",
                        "+",
                    ).splitlines()
                )
                assert before == after, "automatic template changed during capture"
                assert (
                    docker(
                        "exec",
                        name,
                        "find",
                        "/work/captures",
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                    ).splitlines()
                    == roots
                )
                assert (
                    client.get(
                        "/capture?url=http://example.invalid&profile=missing", timeout=2
                    )[0]
                    == 400
                )
                # Missing generated template files also make readiness fail.
                docker("exec", name, "rm", template + "/Local State")
                assert client.get("/readyz", timeout=2)[0] == 503
                pid = docker("exec", name, "cat", "/tmp/api.pid")
                docker("exec", name, "sh", "-c", 'kill -TERM "$1"', "sh", pid)
                returncode, logs = process.wait()
                assert returncode == 0, logs
                assert logs.count("temporary default profile created") == 1, logs
                assert (
                    docker("exec", name, "find", "/work/captures", "-mindepth", "1")
                    == ""
                ), "SIGTERM leaked template"
                docker(
                    "exec",
                    name,
                    "webcappi",
                    "check",
                    "-config",
                    "/etc/webcappi/config.json",
                )
                assert (
                    docker("exec", name, "find", "/work/captures", "-mindepth", "1")
                    == ""
                ), "check leaked template"
    print(
        "PASS: automatic default, named-only configuration, immutable template, readiness, SIGTERM/check cleanup"
    )


def export_profile(resources, root, settings, prefix):
    # Only this disposable test wrapper disables the browser sandbox.
    wrapper = root / "chrome-test"
    wrapper.write_text('#!/bin/sh\nexec /opt/chrome/chrome --no-sandbox "$@"\n')
    wrapper.chmod(0o755)
    archive_bytes = resources.run(
        prefix + "-init",
        "--network=none",
        "--read-only",
        "--shm-size=512m",
        "--tmpfs",
        "/work:uid=10001,gid=10001,mode=700",
        "--tmpfs",
        "/tmp",
        "-v",
        f"{wrapper}:/test/chrome:ro",
        settings.image,
        "webcappi",
        "init-profile",
        "-browser-path",
        "/test/chrome",
        binary=True,
        timeout=45,
    )
    (settings.artifacts / "default-profile.tar").write_bytes(archive_bytes)
    bundles = root / "bundles"
    bundles.mkdir(mode=0o755)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            assert parts and parts[0] == "default" and ".." not in parts
            assert member.isdir() or member.isfile(), member.name
            assert member.uid == 10001 and member.gid == 10001
            assert member.mode & 0o077 == 0, member.name
            assert parts[-1] not in (
                "SingletonLock",
                "SingletonSocket",
                "SingletonCookie",
                "DevToolsActivePort",
            )
            destination = bundles.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                # These newly generated test inputs contain no credentials.
                destination.write_bytes(archive.extractfile(member).read())
                destination.chmod(0o644)
    profile = json.loads((bundles / "default/bundle.json").read_text())
    expected = dict(
        user_data_dir="/bundles/default/user-data",
        profile_directory="Default",
        extensions=[],
    )
    assert profile == expected
    assert profile["extensions"] == [] and "ready_selectors" not in profile
    assert (bundles / "default/user-data/Local State").is_file()
    assert (bundles / "default/user-data/Default/Preferences").is_file()
    assert not list(bundles.joinpath("default/extensions").iterdir())
    return bundles, profile


def smoke_initialized(resources, root, settings, bundles, profile, prefix, client):
    cfg = dict(sandbox=False, work_dir="/work/captures", profiles={"default": profile})
    config = root / "config.json"
    config.write_text(json.dumps(cfg))
    resources.run(
        prefix + "-api",
        "-d",
        "-p",
        "127.0.0.1::8080",
        "--read-only",
        "--shm-size=512m",
        "--tmpfs",
        "/work:uid=10001,gid=10001,mode=700",
        "--tmpfs",
        "/tmp",
        "-v",
        f"{bundles}:/bundles:ro",
        "-v",
        f"{config}:/etc/webcappi/config.json:ro",
        settings.image,
    )
    client.base = container_base(prefix + "-api")
    client.wait_ready()
    before = {
        p.relative_to(bundles): p.read_bytes()
        for p in bundles.rglob("*")
        if p.is_file()
    }
    status, headers, png = client.get(
        "/capture",
        data="<p>日本語: initialized profile</p>".encode(),
        content_type="text/html; charset=utf-8",
    )
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert struct.unpack(">II", png[16:24]) == (1280, 720)
    (settings.artifacts / "initialized-profile.png").write_bytes(png)
    after = {
        p.relative_to(bundles): p.read_bytes()
        for p in bundles.rglob("*")
        if p.is_file()
    }
    assert before == after, "imported template changed during capture"
    assert (
        docker("exec", prefix + "-api", "find", "/work/captures", "-mindepth", "1")
        == ""
    )
    print(
        "PASS: offline headless init-profile, tar export/import, extension-free API capture, immutable template"
    )


def main():
    settings = Settings.from_env()
    prefix = f"webcappi-profile-smoke-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        with DockerResources(settings) as resources:
            with scenario("offline profile export/tar attributes", resources):
                bundles, profile = export_profile(resources, root, settings, prefix)
            client = HTTPClient("", timeout=35)
            with scenario("imported profile capture/immutability", resources, client):
                smoke_initialized(
                    resources, root, settings, bundles, profile, prefix, client
                )
        smoke_automatic(root, settings, bundles, profile, prefix)


if __name__ == "__main__":
    main()
