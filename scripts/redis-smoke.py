#!/usr/bin/env python3
"""Verify actual Chromium captures sharing Redis across disposable API containers."""

import json
import os
from pathlib import Path
import tempfile
import urllib.parse

from smoke_support import (
    DockerResources,
    HTTPClient,
    PYTHON_IMAGE,
    REDIS_IMAGE,
    ROOT,
    Settings,
    container_base,
    container_logs,
    docker,
    scenario,
)


def captures(name):
    return container_logs(name).count("capture started")


def main():
    settings = Settings.from_env()
    prefix = f"webcappi-redis-smoke-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        data = Path(temporary)
        data.chmod(0o755)
        config = {
            # This exception only applies to these disposable test containers.
            "no_sandbox": True,
            "work_dir": "/work/captures",
            "capture_timeout_seconds": 15,
            "cache": {
                "backend": "redis",
                "redis": {
                    "address": "redis:6379",
                    "namespace": "smoke",
                    "generation": "v1",
                },
            },
        }
        config_path = data / "config.json"
        config_path.write_text(json.dumps(config))
        with DockerResources(settings) as resources:
            with scenario("Redis replica startup", resources):
                resources.network(prefix)
                redis_name = prefix + "-redis"
                resources.run(
                    redis_name,
                    "-d",
                    "--network",
                    prefix,
                    "--network-alias",
                    "redis",
                    REDIS_IMAGE,
                    "redis-server",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--maxmemory",
                    "64mb",
                    "--maxmemory-policy",
                    "noeviction",
                )
                resources.run(
                    prefix + "-fixture",
                    "-d",
                    "--network",
                    prefix,
                    "--network-alias",
                    "fixture",
                    "--read-only",
                    "-v",
                    f"{ROOT / 'testdata/page'}:/srv:ro",
                    "-w",
                    "/srv",
                    PYTHON_IMAGE,
                    "python",
                    "-m",
                    "http.server",
                    "8000",
                )

                def start_api(name, path):
                    resources.run(
                        name,
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
                        f"{path}:/etc/webcappi/config.json:ro",
                        settings.image,
                    )
                    client = HTTPClient(container_base(name), timeout=20)
                    client.wait_ready()
                    return client

                first_name, second_name = prefix + "-a", prefix + "-b"
                first = start_api(first_name, config_path)
                second = start_api(second_name, config_path)
                path = "/capture?" + urllib.parse.urlencode(
                    {"url": "http://fixture:8000/", "width": 320, "height": 200}
                )

            with scenario("Redis cross-replica captures", resources, second):
                response = first.get(path)
                assert response[0] == 200, response
                tag = response[1]["ETag"]
                assert second.get(path, tag)[0] == 304
                assert captures(first_name) == 1 and captures(second_name) == 0
                ordinary = second.get(path)
                assert ordinary[0] == 200
                tag = ordinary[1]["ETag"]
                assert captures(second_name) == 1, "ordinary GET reused an image"
                before = captures(first_name)
                docker("restart", first_name)
                first.base = container_base(first_name)
                first.wait_ready()
                assert first.get(path, tag)[0] == 304
                assert captures(first_name) == before, "restart lost shared metadata"

            with scenario("Redis generation isolation", resources):
                config["cache"]["redis"]["generation"] = "v2"
                next_path = data / "next.json"
                next_path.write_text(json.dumps(config))
                next_name = prefix + "-next"
                next_client = start_api(next_name, next_path)
                assert next_client.get(path, tag)[0] in (200, 304)
                assert captures(next_name) == 1, "generation reused old metadata"

            with scenario("Redis outage and recovery", resources, second):
                docker("stop", "-t", "5", redis_name)
                assert second.get("/readyz")[0] == 503
                assert second.get("/healthz")[0] == 200
                assert second.get(path, tag)[0] == 503
                assert (
                    second.get(
                        "/capture",
                        data=b"<p>Redis-independent HTML</p>",
                        content_type="text/html",
                    )[0]
                    == 200
                )
                docker(
                    "exec",
                    second_name,
                    "webcappi",
                    "capture",
                    "-config",
                    "/etc/webcappi/config.json",
                    "-url",
                    "http://fixture:8000/",
                    "-out",
                    "/tmp/redis-independent.png",
                )
                docker("start", redis_name)
                second.wait_ready()
                before = captures(second_name)
                assert second.get(path, tag)[0] in (200, 304)
                assert captures(second_name) == before + 1, "Redis loss reused metadata"

    print("Redis smoke passed: shared captures, restarts, generations, outage/recovery")


if __name__ == "__main__":
    main()
