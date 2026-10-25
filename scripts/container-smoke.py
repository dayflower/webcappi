#!/usr/bin/env python3
"""Exercise the actual REST image using only a private Docker network/fixtures."""

import concurrent.futures
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse

from smoke_support import (
    DockerResources,
    HTTPClient,
    ROOT,
    Settings,
    container_base,
    container_logs,
    docker,
    scenario,
)

ALGORITHMS = ("catmull_rom", "approx_bilinear", "nearest_neighbor")
SCENARIOS = ("authentication", "independent", "conditional", "delay-errors", "resize")


def capture_url(default_url, **kwargs):
    query = dict(url=default_url)
    query.update(kwargs)
    return "/capture?" + urllib.parse.urlencode(query)


def render_count(api):
    return container_logs(api).count("capture started")


def template_snapshot(template):
    return {
        path.relative_to(template): hashlib.sha256(path.read_bytes()).digest()
        for path in template.rglob("*")
        if path.is_file()
    }


def smoke_authentication(client, token):
    get = client.get
    capture = partial(capture_url, "http://fixture:8000/scenarios/authentication/")
    assert get("/healthz", authorization=None)[0] == 200
    for authorization in (None, "Bearer wrong-token", "Basic " + token):
        status, headers, body = get(capture(), authorization=authorization)
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
        assert headers["Cache-Control"] == "no-store"
        assert json.loads(body) == {"error": "unauthorized"}
    assert get(capture(access_token=token), authorization=None)[0] == 401


def smoke_independent(client, api):
    get = client.get
    capture = partial(capture_url, "http://fixture:8000/scenarios/independent/")
    before = render_count(api)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: get(capture()), range(6)))
    assert all(r[0] == 200 for r in results), [
        (r[0], r[2][:256] if r[0] != 200 else len(r[2])) for r in results
    ]
    for _, result_headers, result_png in results:
        assert struct.unpack(">II", result_png[16:24]) == (1280, 720)
        assert (
            result_headers["ETag"] == '"' + hashlib.sha256(result_png).hexdigest() + '"'
        )
    assert render_count(api) == before + 6, "unconditional requests shared a capture"


def smoke_conditional(client, api, artifacts, page):
    get = client.get
    capture = partial(capture_url, "http://fixture:8000/scenarios/conditional/")
    before = render_count(api)
    # Keep all metadata transitions on this scenario's own source URL.
    status, headers, png = get(capture())
    assert status == 200, (status, png[:256])
    assert headers["Content-Type"] == "image/png"
    assert struct.unpack(">II", png[16:24]) == (1280, 720)
    assert get(capture(), headers["ETag"])[0] == 304
    assert get(capture(profile="default"), headers["ETag"])[0] == 304
    assert get(capture(), headers["ETag"], authorization=None)[0] == 401
    assert get(capture(), headers["ETag"], authorization="Bearer wrong-token")[0] == 401
    assert render_count(api) == before + 1, (
        "fresh metadata or rejected auth started a capture"
    )
    # Expired metadata revalidates the source Last-Modified value without
    # generating a PNG. Ordinary GET must still generate a new image.
    before_validation = render_count(api)
    time.sleep(2.1)
    revalidated = get(capture(), headers["ETag"])
    assert revalidated[0] == 304, revalidated
    assert render_count(api) == before_validation, "source 304 generated a PNG"
    assert get(capture())[0] == 200
    assert render_count(api) == before_validation + 1, (
        "unconditional GET reused PNG bytes"
    )
    (artifacts / "api.png").write_bytes(png)
    source = page / "scenarios/conditional/index.html"
    source.write_text(source.read_text().replace("#eef2f6", "#ffcc88"))
    time.sleep(2.1)
    changed = get(capture(), headers["ETag"])
    assert changed[0] == 200, (
        f"refresh returned HTTP {changed[0]}: {changed[2][:1024]!r}"
    )
    assert changed[1]["ETag"] != headers["ETag"], (
        "page change did not change the PNG ETag"
    )

    # A source without validators uses only our bounded freshness window.
    plain_path = capture(url="http://fixture:8000/plain/scenarios/conditional/")
    before_plain = render_count(api)
    plain = get(plain_path)
    assert plain[0] == 200, (plain[0], plain[2][:256])
    assert get(plain_path, plain[1]["ETag"])[0] == 304
    assert render_count(api) == before_plain + 1, (
        "fresh fallback metadata generated a PNG"
    )
    time.sleep(2.1)
    plain_expired = get(plain_path, plain[1]["ETag"])
    assert plain_expired[0] in (200, 304), plain_expired
    if plain_expired[0] == 200:
        assert plain_expired[1]["ETag"] != plain[1]["ETag"]
        assert plain_expired[2] != plain[2]
    assert render_count(api) == before_plain + 2, (
        "expired fallback metadata skipped capture"
    )


def smoke_delay_errors(client):
    get = client.get
    capture = partial(capture_url, "http://fixture:8000/scenarios/delay-errors/")
    started = time.monotonic()
    delayed = get(capture(delay_ms=600))
    assert delayed[0] == 200, (delayed[0], delayed[2][:1024])
    assert time.monotonic() - started >= 0.6, (
        "delay reused a capture with a different delay"
    )
    assert get(capture(delay_ms=600), delayed[1]["ETag"])[0] == 304
    assert get(capture(delay_ms=-1))[0] == 400
    assert get(capture(delay_ms=10000))[0] == 504
    assert get(capture(ready_selector="#never-ready"))[0] == 504
    assert get(capture(url="http://fixture:8000/missing"))[0] == 502
    assert get(capture(width=99999))[0] == 400


def smoke_resize_cli(client, api, artifacts):
    get = client.get
    capture = partial(capture_url, "http://fixture:8000/scenarios/resize/")
    resized_path = capture(width=1200, height=800, output_width=600, output_height=600)
    resized_status, resized_headers, resized_png = get(resized_path)
    assert resized_status == 200, (resized_status, resized_png[:1024])
    assert struct.unpack(">II", resized_png[16:24]) == (600, 600)
    assert get(resized_path, resized_headers["ETag"])[0] == 304
    other_size = get(
        capture(width=1200, height=800, output_width=600, output_height=400),
        resized_headers["ETag"],
    )
    assert other_size[0] == 200
    assert struct.unpack(">II", other_size[2][16:24]) == (600, 400)
    assert other_size[1]["ETag"] != resized_headers["ETag"]
    assert get(capture(output_width=4096, output_height=4096))[0] == 400
    (artifacts / "api-resized.png").write_bytes(resized_png)

    resized_html = get(
        "/capture?width=320&height=200&output_width=160&output_height=160",
        data=b"<p>resized HTML</p>",
        content_type="text/html",
    )
    assert resized_html[0] == 200, (resized_html[0], resized_html[2][:1024])
    assert struct.unpack(">II", resized_html[2][16:24]) == (160, 160)
    assert resized_html[1]["Cache-Control"] == "no-store"

    for algorithm in ALGORITHMS:
        docker(
            "exec",
            api,
            "webcappi",
            "capture",
            "-config",
            f"/etc/webcappi/resize-filters/{algorithm}.json",
            "-url",
            "http://fixture:8000/scenarios/resize/",
            "-width",
            "1200",
            "-height",
            "800",
            "-output-width",
            "600",
            "-output-height",
            "600",
            "-out",
            "/tmp/cli-resized.png",
        )
        cli_png = docker("exec", api, "cat", "/tmp/cli-resized.png", binary=True)
        assert struct.unpack(">II", cli_png[16:24]) == (600, 600), algorithm

    docker(
        "exec",
        api,
        "webcappi",
        "capture",
        "-config",
        "/etc/webcappi/config.json",
        "-url",
        "http://fixture:8000/scenarios/resize/",
        "-out",
        "/tmp/cli-default.png",
    )
    cli_default = docker("exec", api, "cat", "/tmp/cli-default.png", binary=True)
    assert struct.unpack(">II", cli_default[16:24]) == (1280, 720)


def smoke_html(client, api, artifacts, max_post_bytes):
    get = client.get
    html_path = "/capture?" + urllib.parse.urlencode(
        dict(width=320, height=200, ready_selector="#ready")
    )
    html = (
        "<!doctype html><style>body{background:#123456}</style>"
        "<p id=ready>日本語 HTML</p>"
    ).encode("utf-8")

    def post_html(body=html, etag=None, authorization=..., mode=None):
        path = html_path
        if mode is not None:
            path += "&" + urllib.parse.urlencode(dict(html_mode=mode))
        return get(
            path,
            etag=etag,
            authorization=authorization,
            data=body,
            content_type="text/html; charset=utf-8",
        )

    assert post_html(authorization=None)[0] == 401
    before_html_renders = render_count(api)
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
    minimal_status, minimal_headers, minimal_png = get(
        "/capture", data=html, content_type="text/html"
    )
    assert minimal_status == 200, (minimal_status, minimal_png[:1024])
    assert minimal_headers["Content-Type"] == "image/png"
    assert minimal_headers["Cache-Control"] == "no-store"
    assert struct.unpack(">II", minimal_png[16:24]) == (1280, 720)
    assert post_html(mode="data")[0] == 400
    assert post_html(body=b"x" * (max_post_bytes + 1))[0] == 413
    assert get(html_path, data=b"{}", content_type="application/json")[0] == 415
    after_html_renders = render_count(api)
    assert after_html_renders == before_html_renders + 7, (
        "POST reused a previous render"
    )

    for content_type, meta in (
        ("text/html; charset=shift_jis", "utf-8"),
        ("text/html", "shift_jis"),
    ):
        charset_html = (
            f"<!doctype html><meta charset={meta}><p>日本語</p>"
            r"<script>if(document.characterSet === 'Shift_JIS' && "
            r"document.querySelector('p').textContent === '\u65e5\u672c\u8a9e')"
            r"document.body.id='ready';</script>"
        ).encode("shift_jis")
        charset_status, charset_headers, charset_png = get(
            html_path + "&html_mode=loopback",
            data=charset_html,
            content_type=content_type,
        )
        assert charset_status == 200, (content_type, charset_status, charset_png[:1024])
        assert charset_headers["Content-Type"] == "image/png"
        assert charset_headers["Cache-Control"] == "no-store"
        assert struct.unpack(">II", charset_png[16:24]) == (320, 200)
    assert (
        get(
            html_path + "&html_mode=file",
            data=charset_html,
            content_type="text/html; charset=shift_jis",
        )[0]
        == 415
    )
    assert (
        get(
            html_path + "&html_mode=file",
            data=charset_html,
            content_type="text/html; charset=utf-8",
        )[0]
        == 400
    )

    (artifacts / "api-html.png").write_bytes(html_png)


def smoke_shutdown(api, template, original):
    assert docker("exec", api, "find", "/work/captures", "-mindepth", "1") == ""
    assert template_snapshot(template) == original, "template changed"
    docker("stop", "-t", "10", api)
    state = json.loads(docker("inspect", api))[0]["State"]
    assert state["ExitCode"] == 0, state


def main():
    settings = Settings.from_env()
    subprocess.run([sys.executable, str(ROOT / "scripts/profile-smoke.py")], check=True)
    if not (settings.artifacts / "user-data").is_dir():
        raise SystemExit(
            "Run make browser-test first to create the synthetic template."
        )
    prefix = f"webcappi-smoke-{os.getpid()}"
    api = prefix + "-api"
    with tempfile.TemporaryDirectory(prefix="webcappi-smoke-") as tmp:
        data = Path(tmp)
        # Container UID needs traversal/read access to synthetic test inputs only.
        data.chmod(0o755)
        # The fixture has no credentials. Export ownership from docker cp is the
        # host user's, so grant read/traverse access on this temporary test copy.
        shutil.copytree(settings.artifacts / "user-data", data / "user-data")
        template = data / "user-data"
        for path in [template, *template.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        (data / "page/scenarios").mkdir(parents=True)
        for name in SCENARIOS:
            shutil.copytree(ROOT / "testdata/page", data / "page/scenarios" / name)
        (data / "page/fixture-server.py").write_text(
            "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
            "class Handler(SimpleHTTPRequestHandler):\n"
            "    def translate_path(self, path):\n"
            "        return super().translate_path('/' + path[len('/plain/'):] if path.startswith('/plain/') else path)\n"
            "    def send_header(self, name, value):\n"
            "        if self.path.startswith('/plain/') and name.lower() == 'last-modified':\n"
            "            return\n"
            "        super().send_header(name, value)\n"
            "ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()\n"
        )
        profile = dict(
            user_data_dir="/template/user-data",
            profile_directory="Default",
            extensions=["/src/testdata/extension"],
            ready_selectors=["#webcappi-extension-ready", "html"],
        )
        (data / "bundle.json").write_text(json.dumps(profile))
        # This exception is confined to disposable tests on this host. Production
        # examples preserve the sandbox unless explicitly configured otherwise.
        cfg = dict(
            no_sandbox=True,
            work_dir="/work/captures",
            concurrency=1,
            queue_size=8,
            max_requests=8,
            capture_timeout_seconds=10,
            max_age_seconds=2,
            max_post_bytes=1024,
            profiles={"default": profile},
        )
        token = "synthetic-smoke-token"
        (data / "token").write_text(token + "\n")
        (data / "token").chmod(0o644)
        cfg["auth"] = {"bearer_token_file": "/run/secrets/webcappi/token"}
        (data / "config.json").write_text(json.dumps(cfg))
        filters = data / "resize-filters"
        filters.mkdir()
        for algorithm in ALGORITHMS:
            (filters / f"{algorithm}.json").write_text(
                json.dumps(dict(cfg, resize_algorithm=algorithm))
            )

        artifacts = settings.artifacts
        original = template_snapshot(template)
        client = HTTPClient("", authorization=f"Bearer {token}")
        with DockerResources(settings) as resources:
            with scenario("REST startup/readiness", resources, client):
                resources.network(prefix)
                resources.run(
                    prefix + "-page",
                    "-d",
                    "--network",
                    prefix,
                    "--network-alias",
                    "fixture",
                    "--read-only",
                    "-v",
                    f"{data / 'page'}:/srv:ro",
                    "-w",
                    "/srv",
                    "python:3.13-alpine",
                    "python",
                    "/srv/fixture-server.py",
                )
                resources.run(
                    api,
                    "-d",
                    "--network",
                    prefix,
                    "-p",
                    "127.0.0.1::8080",
                    "--read-only",
                    "--shm-size=512m",
                    "--tmpfs",
                    "/work:uid=10001,gid=10001,mode=700",
                    "--tmpfs",
                    "/tmp",
                    "-v",
                    f"{template}:/template/user-data:ro",
                    "-v",
                    f"{ROOT / 'testdata/extension'}:/src/testdata/extension:ro",
                    "-v",
                    f"{data / 'bundle.json'}:/template/bundle.json:ro",
                    "-v",
                    f"{data / 'token'}:/run/secrets/webcappi/token:ro",
                    "-v",
                    f"{data / 'config.json'}:/etc/webcappi/config.json:ro",
                    "-v",
                    f"{filters}:/etc/webcappi/resize-filters:ro",
                    settings.image,
                )

                client.base = container_base(api)
                client.wait_ready()
            checks = (
                ("authentication", lambda: smoke_authentication(client, token)),
                ("independent captures", lambda: smoke_independent(client, api)),
                (
                    "conditional GET/source updates",
                    lambda: smoke_conditional(client, api, artifacts, data / "page"),
                ),
                ("delay/errors", lambda: smoke_delay_errors(client)),
                ("resize/CLI", lambda: smoke_resize_cli(client, api, artifacts)),
                (
                    "HTML modes/encoding",
                    lambda: smoke_html(client, api, artifacts, cfg["max_post_bytes"]),
                ),
                (
                    "shutdown/immutability",
                    lambda: smoke_shutdown(api, template, original),
                ),
            )
            for name, check in checks:
                with scenario(name, resources, client):
                    check()
            print(
                "PASS: real container REST, Bearer authentication, private DNS, independent URL/HTML PNG, extension, metadata/source validation, fresh ETag, errors, cleanup, shutdown"
            )


if __name__ == "__main__":
    main()
