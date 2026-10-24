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
import sys
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
    subprocess.run([sys.executable, str(ROOT / "scripts/profile-smoke.py")], check=True)
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
        (data / "page/fixture-server.py").write_text(
            "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
            "class Handler(SimpleHTTPRequestHandler):\n"
            "    def translate_path(self, path):\n"
            "        return super().translate_path('/' + path[len('/plain/'):] if path.startswith('/plain/') else path)\n"
            "    def send_header(self, name, value):\n"
            "        if self.path == '/plain/' and name.lower() == 'last-modified':\n"
            "            return\n"
            "        super().send_header(name, value)\n"
            "ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()\n")
        profile = dict(user_data_dir="/template/user-data", profile_directory="Default",
                       extensions=["/src/testdata/extension"],
                       ready_selectors=["#webcappi-extension-ready", "html"])
        (data / "bundle.json").write_text(json.dumps(profile))
        # This exception is confined to disposable tests on this host. Production
        # examples preserve the sandbox unless explicitly configured otherwise.
        cfg = dict(no_sandbox=True, work_dir="/work/captures", concurrency=1,
                   queue_size=8, max_requests=8, capture_timeout_seconds=10,
                   max_age_seconds=2, max_post_bytes=1024, profiles={"default": profile})
        token = "synthetic-smoke-token"
        (data / "token").write_text(token + "\n")
        (data / "token").chmod(0o644)
        cfg["auth"] = {"bearer_token_file": "/run/secrets/webcappi/token"}
        (data / "config.json").write_text(json.dumps(cfg))
        filters = data / "resize-filters"
        filters.mkdir()
        algorithms = ("catmull_rom", "approx_bilinear", "nearest_neighbor")
        for algorithm in algorithms:
            (filters / f"{algorithm}.json").write_text(json.dumps(dict(cfg, resize_algorithm=algorithm)))
        created = []
        try:
            docker("network", "create", PREFIX)
            docker("run", *platform_args, "-d", "--name", PREFIX + "-page", "--network", PREFIX,
                   "--network-alias", "fixture", "--read-only", "-v", f"{data / 'page'}:/srv:ro",
                   "-w", "/srv", "python:3.13-alpine", "python", "/srv/fixture-server.py")
            created.append(PREFIX + "-page")
            docker("run", *platform_args, "-d", "--name", PREFIX + "-api", "--network", PREFIX,
                   "-p", "127.0.0.1::8080", "--read-only", "--shm-size=512m",
                   "--tmpfs", "/work:uid=10001,gid=10001,mode=700", "--tmpfs", "/tmp",
                   "-v", f"{template}:/template/user-data:ro",
                   "-v", f"{ROOT / 'testdata/extension'}:/src/testdata/extension:ro",
                   "-v", f"{data / 'bundle.json'}:/template/bundle.json:ro",
                   "-v", f"{data / 'token'}:/run/secrets/webcappi/token:ro",
                   "-v", f"{data / 'config.json'}:/etc/webcappi/config.json:ro",
                   "-v", f"{filters}:/etc/webcappi/resize-filters:ro", image)
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
                query = dict(url="http://fixture:8000/")
                query.update(kwargs)
                return "/capture?" + urllib.parse.urlencode(query)

            assert get("/healthz", authorization=None)[0] == 200
            for authorization in (None, "Bearer wrong-token", "Basic " + token):
                status, headers, body = get(capture(), authorization=authorization)
                assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
                assert headers["Cache-Control"] == "no-store"
                assert json.loads(body) == {"error": "unauthorized"}
            assert get(capture(access_token=token), authorization=None)[0] == 401

            def render_count():
                logs = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
                return (logs.stdout + logs.stderr).count("capture started")

            original = hashlib.sha256()
            for path in sorted(template.rglob("*")):
                if path.is_file():
                    original.update(path.read_bytes())
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: get(capture()), range(6)))
            assert all(r[0] == 200 for r in results), [(r[0], r[2][:256] if r[0] != 200 else len(r[2])) for r in results]
            for _, result_headers, result_png in results:
                assert struct.unpack(">II", result_png[16:24]) == (1280, 720)
                assert result_headers["ETag"] == '"' + hashlib.sha256(result_png).hexdigest() + '"'
            assert render_count() == 6, "unconditional requests shared a capture"
            # Establish a current validator after the independent concurrent jobs.
            status, headers, png = get(capture())
            assert status == 200, (status, png[:256])
            assert headers["Content-Type"] == "image/png"
            assert struct.unpack(">II", png[16:24]) == (1280, 720)
            assert get(capture(), headers["ETag"])[0] == 304
            assert get(capture(profile="default"), headers["ETag"])[0] == 304
            assert get(capture(), headers["ETag"], authorization=None)[0] == 401
            assert get(capture(), headers["ETag"], authorization="Bearer wrong-token")[0] == 401
            # Docker sends slog's stderr separately from stdout.
            logproc = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
            assert (logproc.stdout + logproc.stderr).count("capture started") == 7
            # Expired metadata revalidates the source Last-Modified value without
            # generating a PNG. Ordinary GET must still generate a new image.
            before_validation = render_count()
            time.sleep(2.1)
            revalidated = get(capture(), headers["ETag"])
            assert revalidated[0] == 304, revalidated
            assert render_count() == before_validation, "source 304 generated a PNG"
            assert get(capture())[0] == 200
            assert render_count() == before_validation + 1, "unconditional GET reused PNG bytes"
            (artifacts / "api.png").write_bytes(png)
            (data / "page/index.html").write_text((data / "page/index.html").read_text().replace("#eef2f6", "#ffcc88"))
            time.sleep(2.1)
            changed = get(capture(), headers["ETag"])
            assert changed[0] == 200, f"refresh returned HTTP {changed[0]}: {changed[2][:1024]!r}"
            assert changed[1]["ETag"] != headers["ETag"], "page change did not change the PNG ETag"
            started = time.monotonic()
            delayed = get(capture(delay_ms=600))
            assert delayed[0] == 200, (delayed[0], delayed[2][:1024])
            assert time.monotonic() - started >= 0.6, "delay reused a capture with a different delay"
            assert get(capture(delay_ms=600), delayed[1]["ETag"])[0] == 304
            assert get(capture(delay_ms=-1))[0] == 400
            assert get(capture(delay_ms=10000))[0] == 504
            assert get(capture(ready_selector="#never-ready"))[0] == 504
            assert get(capture(url="http://fixture:8000/missing"))[0] == 502
            assert get(capture(width=99999))[0] == 400

            # A source without validators uses only our bounded freshness window.
            plain_path = capture(url="http://fixture:8000/plain/")
            before_plain = render_count()
            plain = get(plain_path)
            assert plain[0] == 200, (plain[0], plain[2][:256])
            assert get(plain_path, plain[1]["ETag"])[0] == 304
            assert render_count() == before_plain + 1, "fresh fallback metadata generated a PNG"
            time.sleep(2.1)
            plain_expired = get(plain_path, plain[1]["ETag"])
            assert plain_expired[0] in (200, 304), plain_expired
            if plain_expired[0] == 200:
                assert plain_expired[1]["ETag"] != plain[1]["ETag"]
                assert plain_expired[2] != plain[2]
            assert render_count() == before_plain + 2, "expired fallback metadata skipped capture"

            resized_path = capture(width=1200, height=800, output_width=600, output_height=600)
            resized_status, resized_headers, resized_png = get(resized_path)
            assert resized_status == 200, (resized_status, resized_png[:1024])
            assert struct.unpack(">II", resized_png[16:24]) == (600, 600)
            assert get(resized_path, resized_headers["ETag"])[0] == 304
            other_size = get(capture(width=1200, height=800, output_width=600, output_height=400),
                             resized_headers["ETag"])
            assert other_size[0] == 200
            assert struct.unpack(">II", other_size[2][16:24]) == (600, 400)
            assert other_size[1]["ETag"] != resized_headers["ETag"]
            assert get(capture(output_width=4096, output_height=4096))[0] == 400
            (artifacts / "api-resized.png").write_bytes(resized_png)

            resized_html = get("/capture?width=320&height=200&output_width=160&output_height=160",
                               data=b"<p>resized HTML</p>", content_type="text/html")
            assert resized_html[0] == 200, (resized_html[0], resized_html[2][:1024])
            assert struct.unpack(">II", resized_html[2][16:24]) == (160, 160)
            assert resized_html[1]["Cache-Control"] == "no-store"

            for algorithm in algorithms:
                docker("exec", PREFIX + "-api", "webcappi", "capture",
                       "-config", f"/etc/webcappi/resize-filters/{algorithm}.json", "-url", "http://fixture:8000/",
                       "-width", "1200", "-height", "800", "-output-width", "600",
                       "-output-height", "600", "-out", "/tmp/cli-resized.png")
                cli_png = subprocess.run(["docker", "exec", PREFIX + "-api", "cat", "/tmp/cli-resized.png"],
                                         check=True, capture_output=True).stdout
                assert struct.unpack(">II", cli_png[16:24]) == (600, 600), algorithm

            docker("exec", PREFIX + "-api", "webcappi", "capture",
                   "-config", "/etc/webcappi/config.json", "-url", "http://fixture:8000/",
                   "-out", "/tmp/cli-default.png")
            cli_default = subprocess.run(["docker", "exec", PREFIX + "-api", "cat", "/tmp/cli-default.png"],
                                         check=True, capture_output=True).stdout
            assert struct.unpack(">II", cli_default[16:24]) == (1280, 720)

            html_path = "/capture?" + urllib.parse.urlencode(dict(width=320, height=200,
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
            minimal_status, minimal_headers, minimal_png = get("/capture", data=html, content_type="text/html")
            assert minimal_status == 200, (minimal_status, minimal_png[:1024])
            assert minimal_headers["Content-Type"] == "image/png"
            assert minimal_headers["Cache-Control"] == "no-store"
            assert struct.unpack(">II", minimal_png[16:24]) == (1280, 720)
            assert post_html(mode="data")[0] == 400
            assert post_html(body=b"x" * (cfg["max_post_bytes"] + 1))[0] == 413
            assert get(html_path, data=b"{}", content_type="application/json")[0] == 415
            after_html_logs = subprocess.run(["docker", "logs", PREFIX + "-api"], capture_output=True, text=True, check=True)
            after_html_renders = (after_html_logs.stdout + after_html_logs.stderr).count("capture started")
            assert after_html_renders == before_html_renders + 7, "POST reused a previous render"

            for content_type, meta in (("text/html; charset=shift_jis", "utf-8"),
                                       ("text/html", "shift_jis")):
                charset_html = (f"<!doctype html><meta charset={meta}><p>日本語</p>"
                                r"<script>if(document.characterSet === 'Shift_JIS' && "
                                r"document.querySelector('p').textContent === '\u65e5\u672c\u8a9e')"
                                r"document.body.id='ready';</script>").encode("shift_jis")
                charset_status, charset_headers, charset_png = get(
                    html_path + "&html_mode=loopback", data=charset_html,
                    content_type=content_type)
                assert charset_status == 200, (content_type, charset_status, charset_png[:1024])
                assert charset_headers["Content-Type"] == "image/png"
                assert charset_headers["Cache-Control"] == "no-store"
                assert struct.unpack(">II", charset_png[16:24]) == (320, 200)
            assert get(html_path + "&html_mode=file", data=charset_html,
                       content_type="text/html; charset=shift_jis")[0] == 415
            assert get(html_path + "&html_mode=file", data=charset_html,
                       content_type="text/html; charset=utf-8")[0] == 400

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
            print("PASS: real container REST, Bearer authentication, private DNS, independent URL/HTML PNG, extension, metadata/source validation, fresh ETag, errors, cleanup, shutdown")
        finally:
            for container in reversed(created):
                docker("rm", "-f", container, check=False)
            docker("network", "rm", PREFIX, check=False)


if __name__ == "__main__":
    main()
