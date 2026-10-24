#!/usr/bin/env python3
"""Exercise the actual REST image using only a private Docker network/fixtures."""
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PREFIX = f"webcappi-smoke-{os.getpid()}"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


def main():
    artifacts = Path(os.environ.get("WEBCAPPI_ARTIFACTS_DIR", ROOT / "artifacts"))
    if not artifacts.is_absolute():
        artifacts = ROOT / artifacts
    image = os.environ.get("WEBCAPPI_IMAGE", "webcappi:dev")
    platform = os.environ.get("PLATFORM")
    if platform and platform not in ("linux/amd64", "linux/arm64"):
        raise SystemExit(f"Unsupported test platform: {platform}")
    platform_args = ["--platform", platform] if platform else []
    image_platform = docker("image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}")
    if platform and image_platform != platform:
        raise SystemExit(f"Expected {platform} image, got {image_platform}")
    template = artifacts / "user-data"
    if not template.is_dir():
        raise SystemExit("Run make browser-test first to create the synthetic template.")
    with tempfile.TemporaryDirectory(prefix="webcappi-smoke-") as tmp:
        data = Path(tmp)
        # Container UID needs traversal/read access to synthetic test inputs only.
        data.chmod(0o755)
        # The fixture has no credentials. Export ownership from docker cp is the
        # host user's, so grant read/traverse access on this temporary test copy.
        shutil.copytree(template, data / "user-data")
        template = data / "user-data"
        for path in [template, *template.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        shutil.copytree(ROOT / "testdata/page", data / "page")
        profile = dict(revision="smoke-1", browser_version="156.0.8078.47",
                       user_data_dir="/template/user-data", profile_directory="Default",
                       extensions=["/src/testdata/extension"],
                       ready_selectors=["#webcappi-extension-ready", "html"])
        (data / "bundle.json").write_text(json.dumps(profile))
        # This exception is confined to disposable tests on this host. Production
        # examples preserve the sandbox unless explicitly configured otherwise.
        cfg = dict(no_sandbox=True, work_dir="/work/captures", concurrency=1,
                   queue_size=1, max_requests=8, capture_timeout_seconds=10,
                   cache_ttl_seconds=2, max_post_bytes=1024, profiles={"reader": profile})
        token = "synthetic-smoke-token"
        (data / "token").write_text(token + "\n")
        (data / "token").chmod(0o644)
        cfg["auth"] = {"bearer_token_file": "/run/secrets/webcappi/token"}
        (data / "config.json").write_text(json.dumps(cfg))
        created = []
        try:
            docker("network", "create", PREFIX)
            docker("run", *platform_args, "-d", "--name", PREFIX + "-page", "--network", PREFIX,
                   "--network-alias", "fixture", "--read-only", "-v", f"{data / 'page'}:/srv:ro",
                   "-w", "/srv", "python:3.13-alpine", "python", "-m", "http.server", "8000")
            created.append(PREFIX + "-page")
            docker("run", *platform_args, "-d", "--name", PREFIX + "-api", "--network", PREFIX,
                   "-p", "127.0.0.1::8080", "--read-only", "--shm-size=512m",
                   "--tmpfs", "/work:uid=10001,gid=10001,mode=700", "--tmpfs", "/tmp",
                   "-v", f"{template}:/template/user-data:ro",
                   "-v", f"{ROOT / 'testdata/extension'}:/src/testdata/extension:ro",
                   "-v", f"{data / 'bundle.json'}:/template/bundle.json:ro",
                   "-v", f"{data / 'token'}:/run/secrets/webcappi/token:ro",
                   "-v", f"{data / 'config.json'}:/etc/webcappi/config.json:ro", image)
            created.append(PREFIX + "-api")
            port = docker("port", PREFIX + "-api", "8080/tcp").rsplit(":", 1)[1]
            base = f"http://127.0.0.1:{port}"

            def get(path, etag=None, authorization=f"Bearer {token}", data=None, content_type=None):
                headers = {"If-None-Match": etag} if etag else {}
                if authorization is not None:
                    headers["Authorization"] = authorization
                if content_type:
                    headers["Content-Type"] = content_type
                req = urllib.request.Request(base + path, headers=headers, data=data)
                try:
                    response = urllib.request.urlopen(req, timeout=15)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, response.headers, response.read()

            for _ in range(100):
                try:
                    if get("/readyz", authorization=None)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.1)
            else:
                logs = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True)
                raise AssertionError("readiness failed: " + logs.stdout + logs.stderr)

            def capture(**kwargs):
                query = dict(profile="reader", url="http://fixture:8000/", width=800, height=480)
                query.update(kwargs)
                return "/capture?" + urllib.parse.urlencode(query)

            assert get("/healthz", authorization=None)[0] == 200
            for authorization in (None, "Bearer wrong-token", "Basic " + token):
                status, headers, body = get(capture(), authorization=authorization)
                assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
                assert headers["Cache-Control"] == "no-store"
                assert json.loads(body) == {"error": "unauthorized"}
            assert get(capture(access_token=token), authorization=None)[0] == 401

            original = hashlib.sha256()
            for path in sorted(template.rglob("*")):
                if path.is_file():
                    original.update(path.read_bytes())
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: get(capture()), range(6)))
            status, headers, png = results[0]
            assert all(r[0] == 200 and r[2] == png for r in results)
            assert headers["Content-Type"] == "image/png"
            assert struct.unpack(">II", png[16:24]) == (800, 480)
            assert get(capture(), headers["ETag"])[0] == 304
            assert get(capture(), headers["ETag"], authorization=None)[0] == 401
            assert get(capture(), headers["ETag"], authorization="Bearer wrong-token")[0] == 401
            # Docker sends slog's stderr separately from stdout.
            logproc = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
            assert (logproc.stdout + logproc.stderr).count("capture started") == 1
            (artifacts / "api.png").write_bytes(png)
            (data / "page/index.html").write_text((data / "page/index.html").read_text().replace("#eef2f6", "#ffcc88"))
            time.sleep(2.1)
            changed = get(capture(), headers["ETag"])
            assert changed[0] == 200, f"refresh returned HTTP {changed[0]}: {changed[2][:1024]!r}"
            assert changed[1]["ETag"] != headers["ETag"], "page change did not change the PNG ETag"
            assert get(capture(ready_selector="#never-ready"))[0] == 504
            assert get(capture(url="http://fixture:8000/missing"))[0] == 502
            assert get(capture(width=99999))[0] == 400

            html_path = "/capture?" + urllib.parse.urlencode(dict(profile="reader", width=320, height=200,
                                                                  ready_selector="#ready"))
            html = ("<!doctype html><style>body{background:#123456}</style>"
                    "<p id=ready>日本語 HTML</p>").encode("utf-8")

            def post_html(body=html, etag=None, authorization=f"Bearer {token}", mode=None):
                path = html_path
                if mode is not None:
                    path += "&" + urllib.parse.urlencode(dict(html_mode=mode))
                return get(path, etag=etag, authorization=authorization,
                           data=body, content_type="text/html; charset=utf-8")

            assert post_html(authorization=None)[0] == 401
            before_html_logs = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
            before_html_renders = (before_html_logs.stdout + before_html_logs.stderr).count("capture started")
            html_status, html_headers, html_png = post_html()
            assert html_status == 200, (html_status, html_png[:1024])
            assert html_headers["Content-Type"] == "image/png"
            assert html_headers["Cache-Control"] == "no-store"
            assert "ETag" not in html_headers and "Age" not in html_headers
            assert struct.unpack(">II", html_png[16:24]) == (320, 200)
            assert post_html()[2] == html_png
            assert post_html(etag="*")[0] == 200
            changed_html = post_html(body=html.replace(b"#123456", b"#abcdef"))
            assert changed_html[0] == 200 and changed_html[2] != html_png
            for mode in ("file", "loopback"):
                mode_status, mode_headers, mode_png = post_html(mode=mode)
                assert mode_status == 200, (mode, mode_status, mode_png[:1024])
                assert mode_headers["Content-Type"] == "image/png"
                assert mode_headers["Cache-Control"] == "no-store"
                assert "ETag" not in mode_headers and "Age" not in mode_headers
                assert struct.unpack(">II", mode_png[16:24]) == (320, 200)
                (artifacts / f"api-html-{mode}.png").write_bytes(mode_png)
            assert post_html(mode="data")[0] == 400
            assert post_html(body=b"x" * (cfg["max_post_bytes"] + 1))[0] == 413
            assert get(html_path, data=b"{}", content_type="application/json")[0] == 415
            after_html_logs = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
            after_html_renders = (after_html_logs.stdout + after_html_logs.stderr).count("capture started")
            assert after_html_renders == before_html_renders + 6, "POST reused a previous render"
            (artifacts / "api-html.png").write_bytes(html_png)
            assert docker("exec", PREFIX + "-api", "find", "/work/captures", "-mindepth", "1") == ""
            after = hashlib.sha256()
            for path in sorted(template.rglob("*")):
                if path.is_file():
                    after.update(path.read_bytes())
            assert after.digest() == original.digest(), "template changed"
            docker("stop", "-t", "10", PREFIX + "-api")
            state = json.loads(docker("inspect", PREFIX + "-api"))[0]["State"]
            assert state["ExitCode"] == 0, state
            print("PASS: real container REST, Bearer authentication, private DNS, URL/HTML PNG, extension, coalescing/cache, fresh ETag, errors, cleanup, shutdown")
        finally:
            for container in reversed(created):
                docker("rm", "-f", container, check=False)
            docker("network", "rm", PREFIX, check=False)


if __name__ == "__main__":
    main()
