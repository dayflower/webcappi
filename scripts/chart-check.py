#!/usr/bin/env python3
"""Lint, render, validate Kubernetes/Go configuration, and package the chart."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "bin/chart-tools"
CHART = ROOT / "charts/webcappi"
REDIS = {
    "cache": {
        "backend": "redis",
        "redis": {
            "address": "redis:6379",
            "namespace": "test",
            "generation": "capture-v1",
        },
    }
}
BUNDLE = {
    "profiles": {
        "bundle": {
            "repository": "example.invalid/profiles",
            "digest": "sha256:" + "a" * 64,
        },
        "descriptors": {
            "default": {
                "user_data_dir": "/bundles/default/user-data",
                "profile_directory": "Default",
                "extensions": ["/bundles/default/extensions/fixture"],
                "ready_selectors": ["body"],
            }
        },
    }
}


def run(*args, success=True, **kwargs):
    result = subprocess.run(
        list(map(str, args)), capture_output=True, text=True, timeout=180, **kwargs
    )
    if (result.returncode == 0) != success:
        raise RuntimeError(
            f"unexpected result ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout + (result.stderr if not success else "")


def merge(base, changes):
    result = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, default=CHART)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/charts")
    parser.add_argument("--helm", default=os.environ.get("HELM", str(TOOLS / "helm")))
    parser.add_argument(
        "--kubeconform",
        default=os.environ.get("KUBECONFORM", str(TOOLS / "kubeconform")),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="webcappi-chart-") as temporary:
        temp = Path(temporary)
        base = {"auth": {"existingSecret": "api-auth"}}
        configs = []
        manifests = []

        def render(
            name,
            changes=None,
            release="check",
            success=True,
            error=None,
            skip_schema=False,
            gateway=True,
            parent=False,
        ):
            values = temp / f"{name}.json"
            requested = merge(base, changes or {})
            scoped = deepcopy(requested)
            if parent:
                scoped = {"webcappi": scoped}
                if "global" in requested:
                    scoped["global"] = scoped["webcappi"].pop("global")
            values.write_text(json.dumps(scoped))
            output = run(
                args.helm,
                "template",
                release,
                parent_chart if parent else args.chart,
                "--namespace",
                "check",
                "--kube-version",
                "1.36.4",
                "-f",
                values,
                *(
                    ["--api-versions", "gateway.networking.k8s.io/v1/HTTPRoute"]
                    if gateway
                    else []
                ),
                *(["--skip-schema-validation"] if skip_schema else []),
                success=success,
            )
            if not success:
                assert error in output, (name, output)
                return
            documents = list(yaml.safe_load_all(output))
            by_kind = {d["kind"]: d for d in documents}
            expected = {"Deployment", "Service", "ConfigMap"}
            if requested.get("ingress", {}).get("enabled"):
                expected.add("Ingress")
            if requested.get("httpRoute", {}).get("enabled"):
                expected.add("HTTPRoute")
            assert len(documents) == len(expected) and set(by_kind) == expected
            for kind in ("Ingress", "HTTPRoute"):
                if kind not in by_kind:
                    continue
                network = by_kind[kind]
                assert (
                    network["metadata"]["name"]
                    == by_kind["Service"]["metadata"]["name"]
                )
                port = requested.get("service", {}).get("port", 8080)
                if kind == "Ingress":
                    assert network["spec"]["rules"] == [
                        {
                            "host": host["host"],
                            "http": {
                                "paths": [
                                    {
                                        **path,
                                        "backend": {
                                            "service": {
                                                "name": network["metadata"]["name"],
                                                "port": {"number": port},
                                            }
                                        },
                                    }
                                    for path in host["paths"]
                                ]
                            },
                        }
                        for host in requested["ingress"]["hosts"]
                    ]
                    for field in ("tls", "ingressClassName"):
                        source = "className" if field == "ingressClassName" else field
                        assert network["spec"].get(field) == (
                            requested["ingress"].get(source) or None
                        )
                else:
                    route = requested["httpRoute"]
                    assert network["spec"]["parentRefs"] == route["parentRefs"]
                    assert network["spec"].get("hostnames") == (
                        route.get("hostnames") or None
                    )
                    rule = network["spec"]["rules"][0]
                    assert rule["backendRefs"] == [
                        {"name": network["metadata"]["name"], "port": port}
                    ]
                    assert rule["matches"] == [
                        {"path": path}
                        for path in route.get(
                            "paths", [{"type": "PathPrefix", "value": "/"}]
                        )
                    ]
                    assert rule.get("timeouts") == (route.get("timeouts") or None)
                key = "ingress" if kind == "Ingress" else "httpRoute"
                assert network["metadata"].get("annotations") == (
                    requested[key].get("annotations") or None
                )
            deployment, service, configmap = (
                by_kind[k] for k in ("Deployment", "Service", "ConfigMap")
            )
            pod = deployment["spec"]["template"]
            spec = pod["spec"]
            api = spec["containers"][0]
            config = json.loads(configmap["data"]["config.json"])
            assert config["sandbox"] is (
                requested.get("browser", {}).get("sandbox") is not False
            )
            assert config["listen"] == ":8080"
            assert config["work_dir"] == "/work/captures"
            assert ("redis" in config["cache"]) == (
                config["cache"]["backend"] == "redis"
            )
            assert spec["automountServiceAccountToken"] is False
            assert spec["securityContext"] == dict(
                runAsNonRoot=True,
                runAsUser=10001,
                runAsGroup=10001,
                fsGroup=10001,
                seccompProfile={"type": "RuntimeDefault"},
            )
            browser = requested.get("browser", {})
            expected_security = dict(
                allowPrivilegeEscalation=False,
                readOnlyRootFilesystem=True,
                capabilities={"drop": ["ALL"]},
                seccompProfile={"type": "RuntimeDefault"},
            )
            for key in ("seccompProfile", "appArmorProfile"):
                selected = browser.get(key, {})
                if selected.get("type"):
                    expected_security[key] = {"type": selected["type"]}
                    if selected["type"] == "Localhost":
                        expected_security[key]["localhostProfile"] = selected[
                            "localhostProfile"
                        ]
            assert api["securityContext"] == expected_security
            for init in spec.get("initContainers", []):
                assert init["securityContext"] == dict(
                    allowPrivilegeEscalation=False,
                    readOnlyRootFilesystem=True,
                    capabilities={"drop": ["ALL"]},
                )
            assert (
                service["spec"]["selector"]
                == deployment["spec"]["selector"]["matchLabels"]
                == pod["metadata"]["labels"]
            )
            assert (
                api["ports"][0]["containerPort"] == 8080
                and service["spec"]["ports"][0]["targetPort"] == "http"
            )
            assert all(
                api[k]["httpGet"]["port"] == "http"
                for k in ["startupProbe", "readinessProbe", "livenessProbe"]
            )
            assert (
                pod["metadata"]["annotations"]["checksum/config"]
                == hashlib.sha256(
                    configmap["data"]["config.json"].rstrip("\n").encode()
                ).hexdigest()
            )
            for volume in spec["volumes"]:
                if "emptyDir" in volume:
                    assert volume["emptyDir"]["sizeLimit"]
                if "secret" in volume:
                    assert volume["secret"]["defaultMode"] == 0o440
            path = temp / f"{name}.yaml"
            path.write_text(output)
            manifests.append(path)
            path = temp / f"{name}-config.json"
            path.write_text(json.dumps(config))
            configs.append(path)
            return deployment, service, config

        defaults = temp / "lint.json"
        defaults.write_text(json.dumps(base))
        print(run(args.helm, "lint", "--strict", args.chart, "-f", defaults).strip())

        parent_chart = temp / "parent"
        shutil.copytree(args.chart, parent_chart / "charts/webcappi")
        (parent_chart / "templates").mkdir()
        (parent_chart / "values.yaml").write_text("{}\n")
        metadata = yaml.safe_load((args.chart / "Chart.yaml").read_text())
        (parent_chart / "Chart.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v2",
                    "name": "chart-check-parent",
                    "version": "0.1.0",
                    "dependencies": [
                        {
                            "name": "webcappi",
                            "version": metadata["version"],
                            "repository": "file://charts/webcappi",
                        }
                    ],
                }
            )
        )
        overrides = {"service": {"port": 9090}, "capture": {"timeoutSeconds": 45}}
        _, service, config = render("subchart-reference", overrides)
        assert service["spec"]["ports"][0]["port"] == 9090
        assert config["capture_timeout_seconds"] == 45
        reference = list(
            yaml.safe_load_all((temp / "subchart-reference.yaml").read_text())
        )
        for name, globals_ in (
            ("absent", {}),
            ("empty", {"global": {}}),
            (
                "nested",
                {
                    "global": {
                        "imageRegistry": "registry.example.com",
                        "service": {"port": 1234},
                        "shared": {"enabled": True, "labels": ["parent", "sibling"]},
                    }
                },
            ),
        ):
            name = "subchart-" + name
            render(name, merge(overrides, globals_), parent=True)
            assert list(yaml.safe_load_all((temp / f"{name}.yaml").read_text())) == (
                reference
            ), "parent values changed the standalone resources"
        print(
            run(
                args.helm,
                "lint",
                "--strict",
                parent_chart,
                "-f",
                temp / "subchart-nested.json",
            ).strip()
        )
        subchart_rejections = 0
        for name, changes, error in (
            ("unknown", {"unknownSetting": True}, "unknownSetting"),
            ("port", {"service": {"port": "invalid"}}, "port"),
            ("auth", {"auth": {"existingSecret": ""}}, "existingSecret"),
        ):
            render(
                "subchart-invalid-" + name,
                changes,
                parent=True,
                success=False,
                error=error,
            )
            subchart_rejections += 1

        memory, _, _ = render("memory")
        assert memory["spec"]["strategy"] == {"type": "Recreate"}
        explicit, _, _ = render("sandbox-enabled", {"browser": {"sandbox": True}})
        disabled, _, _ = render("sandbox-disabled", {"browser": {"sandbox": False}})
        assert explicit == memory, "explicit sandbox default changed the Deployment"
        for value in (None, ""):
            empty, _, _ = render(
                "sandbox-empty-" + str(value), {"browser": {"sandbox": value}}
            )
            assert empty == memory, "empty sandbox changed the default"
        old_pod = memory["spec"]["template"]
        new_pod = disabled["spec"]["template"]
        assert (
            new_pod["metadata"]["annotations"]["checksum/config"]
            != old_pod["metadata"]["annotations"]["checksum/config"]
        )
        assert new_pod["spec"] == old_pod["spec"], (
            "sandbox opt-out changed container isolation settings"
        )
        render("redis-sandbox-disabled", merge(REDIS, {"browser": {"sandbox": False}}))
        render(
            "disabled-with-profiles",
            merge(
                BUNDLE,
                {
                    "browser": {
                        "sandbox": False,
                        "seccompProfile": {
                            "type": "Localhost",
                            "localhostProfile": "webcappi/chrome.json",
                        },
                        "appArmorProfile": {
                            "type": "Localhost",
                            "localhostProfile": "webcappi-chrome",
                        },
                    }
                },
            ),
        )
        helper_rejections = 0
        for key in ("seccompProfile", "appArmorProfile"):
            for i, reference in enumerate(
                (
                    {"type": "Unconfined"},
                    {"type": "Localhost", "localhostProfile": "../escape"},
                    {"type": "RuntimeDefault", "localhostProfile": "extra"},
                )
            ):
                render(
                    f"helper-{key}-{i}",
                    {"browser": {key: reference}},
                    success=False,
                    error=key,
                    skip_schema=True,
                )
                helper_rejections += 1

        for key in ("seccompProfile", "appArmorProfile"):
            for kind in ("RuntimeDefault", "Localhost"):
                reference = {
                    "type": kind,
                    "localhostProfile": "webcappi/chrome.json"
                    if key == "seccompProfile"
                    else "webcappi-chrome",
                }
                if kind == "RuntimeDefault":
                    reference["localhostProfile"] = ""
                render(f"{key}-{kind}", merge(BUNDLE, {"browser": {key: reference}}))

        for count in [1, 3]:
            dep, _, _ = render(f"redis-{count}", merge(REDIS, {"replicaCount": count}))
            assert dep["spec"]["strategy"] == {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            }
        _, _, config = render(
            "redis-tls",
            merge(
                REDIS,
                {
                    "cache": {
                        "redis": {
                            "tls": True,
                            "username": "app",
                            "passwordSecret": {"name": "redis-auth", "key": "pass"},
                            "ca": {"kind": "Secret", "name": "redis-ca", "key": "cert"},
                            "timeoutMS": 0,
                            "retentionSeconds": 0,
                        }
                    }
                },
            ),
        )
        assert (
            config["cache"]["redis"]["password_file"] == "/run/secrets/redis/password"
        )
        assert config["cache"]["redis"]["ca_file"] == "/run/redis-ca/ca.crt"
        render(
            "redis-configmap-ca",
            merge(
                REDIS,
                {
                    "cache": {
                        "redis": {
                            "tls": True,
                            "ca": {"name": "redis-ca", "key": "ca.crt"},
                        }
                    }
                },
            ),
        )
        dep, _, _ = render("bundle", BUNDLE)
        assert dep["spec"]["template"]["spec"]["initContainers"][0]["args"] == [
            "cp -R /bundle/. /out/"
        ]
        _, _, zero = render(
            "zero",
            {
                "capture": {
                    "queueSize": 0,
                    "maxAgeSeconds": 0,
                    "metadataMaxBytes": 0,
                    "metadataMaxEntries": 0,
                },
                "auth": {"enabled": False},
            },
        )
        assert all(
            zero[k] == 0
            for k in [
                "queue_size",
                "max_age_seconds",
                "metadata_max_bytes",
                "metadata_max_entries",
            ]
        )
        assert "auth" not in zero
        capture_schema = json.loads((args.chart / "values.schema.json").read_text())[
            "properties"
        ]["capture"]["properties"]
        for boundary in ["minimum", "maximum"]:
            render(
                "capture-" + boundary,
                {
                    "capture": {
                        key: rule[boundary]
                        for key, rule in capture_schema.items()
                        if boundary in rule
                    }
                },
            )
        escaped = merge(REDIS, {"cache": {"redis": {"username": 'user"with\\quotes'}}})
        _, _, config = render("escaped-json", escaped)
        assert config["cache"]["redis"]["username"] == 'user"with\\quotes'
        render(
            "redis-ipv6", merge(REDIS, {"cache": {"redis": {"address": "[::1]:6379"}}})
        )
        for algorithm in [
            "bilinear",
            "catmull_rom",
            "approx_bilinear",
            "nearest_neighbor",
        ]:
            render(algorithm, {"capture": {"resizeAlgorithm": algorithm}})
        dep, svc, _ = render(
            "digest-port",
            {"image": {"digest": "sha256:" + "b" * 64}, "service": {"port": 9090}},
        )
        assert dep["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
            "@sha256:" + "b" * 64
        )
        assert svc["spec"]["ports"][0]["port"] == 9090
        first = render("first", release="first")
        second = render("second", release="first-webcappi")
        assert first[0]["metadata"]["name"] != second[0]["metadata"]["name"]
        assert first[1]["spec"]["selector"] != second[1]["spec"]["selector"]
        ingress = {
            "ingress": {
                "enabled": True,
                "className": "traefik",
                "annotations": {"example.com/setting": "enabled"},
                "hosts": [
                    {
                        "host": "capture.example.com",
                        "paths": [{"path": "/", "pathType": "Prefix"}],
                    }
                ],
                "tls": [
                    {"secretName": "capture-tls", "hosts": ["capture.example.com"]}
                ],
            }
        }
        route = {
            "httpRoute": {
                "enabled": True,
                "parentRefs": [
                    {
                        "name": "public",
                        "namespace": "gateway-system",
                        "sectionName": "https",
                    }
                ],
                "hostnames": ["capture.example.com"],
                "annotations": {"example.com/setting": "enabled"},
                "timeouts": {"request": "45s", "backendRequest": "40000ms"},
            }
        }
        render("ingress", ingress)
        render("httproute", route)
        render(
            "both-networks-port",
            merge(merge(ingress, route), {"service": {"port": 9090}}),
        )
        render(
            "ingress-port",
            merge(
                ingress,
                {"service": {"port": 80}, "ingress": {"className": "", "tls": []}},
            ),
        )
        render(
            "route-defaults",
            {"httpRoute": {"enabled": True, "parentRefs": [{"name": "public"}]}},
        )
        render(
            "route-zero-timeout",
            merge(
                route,
                {"httpRoute": {"timeouts": {"request": "0s", "backendRequest": "0s"}}},
            ),
        )
        render(
            "route-multiple-paths",
            merge(
                route,
                {
                    "httpRoute": {
                        "paths": [
                            {"type": "Exact", "value": "/capture"},
                            {"type": "PathPrefix", "value": "/healthz"},
                        ]
                    }
                },
            ),
        )
        render(
            "no-gateway-api",
            route,
            success=False,
            error="requires gateway.networking.k8s.io/v1/HTTPRoute",
            gateway=False,
        )
        helper_rejections += 1
        network_invalid = [
            ("ingress-no-hosts", {"ingress": {"enabled": True}}, "hosts"),
            (
                "ingress-empty-host",
                merge(
                    ingress,
                    {
                        "ingress": {
                            "hosts": [
                                {
                                    "host": "",
                                    "paths": [{"path": "/", "pathType": "Prefix"}],
                                }
                            ]
                        }
                    },
                ),
                "host",
            ),
            (
                "ingress-no-paths",
                merge(
                    ingress,
                    {
                        "ingress": {
                            "hosts": [{"host": "capture.example.com", "paths": []}]
                        }
                    },
                ),
                "paths",
            ),
            (
                "ingress-path",
                merge(
                    ingress,
                    {
                        "ingress": {
                            "hosts": [
                                {
                                    "host": "capture.example.com",
                                    "paths": [
                                        {"path": "relative", "pathType": "Prefix"}
                                    ],
                                }
                            ]
                        }
                    },
                ),
                "path",
            ),
            (
                "ingress-path-type",
                merge(
                    ingress,
                    {
                        "ingress": {
                            "hosts": [
                                {
                                    "host": "capture.example.com",
                                    "paths": [{"path": "/", "pathType": "Unknown"}],
                                }
                            ]
                        }
                    },
                ),
                "pathType",
            ),
            (
                "ingress-tls-secret",
                merge(
                    ingress, {"ingress": {"tls": [{"hosts": ["capture.example.com"]}]}}
                ),
                "secretName",
            ),
            ("route-no-parents", {"httpRoute": {"enabled": True}}, "parentRefs"),
            (
                "route-parent-name",
                {
                    "httpRoute": {
                        "enabled": True,
                        "parentRefs": [{"namespace": "gateway-system"}],
                    }
                },
                "name",
            ),
            (
                "route-bad-parent",
                merge(
                    route,
                    {
                        "httpRoute": {
                            "parentRefs": [{"name": "public", "namespace": "bad/name"}]
                        }
                    },
                ),
                "namespace",
            ),
            ("route-no-paths", merge(route, {"httpRoute": {"paths": []}}), "paths"),
            (
                "route-path",
                merge(
                    route,
                    {
                        "httpRoute": {
                            "paths": [{"type": "PathPrefix", "value": "relative"}]
                        }
                    },
                ),
                "value",
            ),
            (
                "route-regex",
                merge(
                    route,
                    {
                        "httpRoute": {
                            "paths": [{"type": "RegularExpression", "value": "/.*"}]
                        }
                    },
                ),
                "type",
            ),
            (
                "route-hostname",
                merge(
                    route, {"httpRoute": {"hostnames": ["https://capture.example.com"]}}
                ),
                "hostnames",
            ),
            (
                "route-timeout",
                merge(route, {"httpRoute": {"timeouts": {"request": "1.5s"}}}),
                "request",
            ),
            (
                "route-backend-timeout",
                merge(
                    route,
                    {
                        "httpRoute": {
                            "timeouts": {"request": "30s", "backendRequest": "1m"}
                        }
                    },
                ),
                "must not exceed",
            ),
            (
                "route-unbounded-backend",
                merge(route, {"httpRoute": {"timeouts": {"backendRequest": "0s"}}}),
                "must not exceed",
            ),
            (
                "route-raw-backends",
                merge(route, {"httpRoute": {"backendRefs": []}}),
                "backendRefs",
            ),
            (
                "ingress-boolean-annotation",
                {"ingress": {"annotations": {"example.com/setting": True}}},
                "annotations",
            ),
        ]
        for name, changes, error in (
            network_invalid[:1]
            + network_invalid[2:3]
            + network_invalid[6:8]
            + network_invalid[9:10]
            + network_invalid[14:16]
        ):
            render(
                "helper-" + name, changes, success=False, error=error, skip_schema=True
            )
            helper_rejections += 1
        invalid = [
            ("global-string", {"global": "invalid"}, "global"),
            ("global-array", {"global": []}, "global"),
            ("memory-replicas", {"replicaCount": 2}, "requires cache.backend=redis"),
            ("replica-zero", {"replicaCount": 0}, "replicaCount"),
            ("replica-string", {"replicaCount": "2"}, "replicaCount"),
            ("replica-fraction", {"replicaCount": 1.5}, "replicaCount"),
            ("auth-missing", {"auth": {"existingSecret": ""}}, "existingSecret"),
            ("auth-key", {"auth": {"tokenKey": ""}}, "tokenKey"),
            ("redis-missing", {"cache": {"backend": "redis"}}, "address"),
            (
                "redis-namespace",
                merge(REDIS, {"cache": {"redis": {"namespace": "bad:name"}}}),
                "namespace",
            ),
            (
                "redis-generation",
                merge(REDIS, {"cache": {"redis": {"generation": ""}}}),
                "generation",
            ),
            (
                "redis-port",
                merge(REDIS, {"cache": {"redis": {"address": "redis:65536"}}}),
                "port must",
            ),
            (
                "redis-url",
                merge(REDIS, {"cache": {"redis": {"address": "redis://redis:6379"}}}),
                "address",
            ),
            (
                "redis-password",
                merge(
                    REDIS,
                    {"cache": {"redis": {"passwordSecret": {"name": "redis-auth"}}}},
                ),
                "both name and key",
            ),
            (
                "redis-ca-key",
                merge(REDIS, {"cache": {"redis": {"ca": {"key": "ca.crt"}}}}),
                "both name and key",
            ),
            (
                "redis-ca-tls",
                merge(REDIS, {"cache": {"redis": {"ca": {"name": "ca", "key": "ca"}}}}),
                "requires TLS",
            ),
            (
                "retention",
                merge(REDIS, {"cache": {"redis": {"retentionSeconds": 59}}}),
                "must cover",
            ),
            (
                "timeout",
                merge(REDIS, {"cache": {"redis": {"timeoutMS": 5001}}}),
                "timeoutMS",
            ),
            ("capacity", {"capture": {"metadataMaxBytes": -1}}, "metadataMaxBytes"),
            ("raw-config", {"config": {"no_sandbox": True}}, "config"),
            ("sandbox", {"capture": {"sandbox": True}}, "sandbox"),
            ("legacy-sandbox", {"browser": {"noSandbox": False}}, "noSandbox"),
            ("sandbox-string", {"browser": {"sandbox": "true"}}, "sandbox"),
            ("sandbox-number", {"browser": {"sandbox": 1}}, "sandbox"),
            (
                "bundle-missing",
                {"profiles": {"descriptors": BUNDLE["profiles"]["descriptors"]}},
                "require profiles.bundle",
            ),
            (
                "bundle-tag",
                {"profiles": {"bundle": {"repository": "example.invalid/bundle"}}},
                "requires repository",
            ),
            (
                "profile-path",
                merge(
                    BUNDLE,
                    {
                        "profiles": {
                            "descriptors": {
                                "default": {"user_data_dir": "/work/template"}
                            }
                        }
                    },
                ),
                "below /bundles/",
            ),
            (
                "extension-path",
                merge(
                    BUNDLE,
                    {
                        "profiles": {
                            "descriptors": {
                                "default": {
                                    "extensions": ["/bundles/default/user-data/ext"]
                                }
                            }
                        }
                    },
                ),
                "separate from user_data_dir",
            ),
        ]
        invalid.extend(
            [
                (
                    "bad-resize",
                    {"capture": {"resizeAlgorithm": "lanczos"}},
                    "resizeAlgorithm",
                ),
                (
                    "no-image-reference",
                    {"image": {"tag": "", "digest": ""}},
                    "fixed tag or digest",
                ),
                ("floating-image", {"image": {"tag": "latest"}}, "tag"),
                (
                    "negative-timeout",
                    merge(REDIS, {"cache": {"redis": {"timeoutMS": -1}}}),
                    "timeoutMS",
                ),
                (
                    "negative-retention",
                    merge(REDIS, {"cache": {"redis": {"retentionSeconds": -1}}}),
                    "retentionSeconds",
                ),
                ("port-zero", {"service": {"port": 0}}, "port"),
                ("zero-volume", {"volumes": {"workSizeLimit": "0Gi"}}, "workSizeLimit"),
                (
                    "reserved-checksum",
                    {"podAnnotations": {"checksum/config": "override"}},
                    "checksum/config",
                ),
                (
                    "empty-selector",
                    merge(
                        BUNDLE,
                        {
                            "profiles": {
                                "descriptors": {"default": {"ready_selectors": ["   "]}}
                            }
                        },
                    ),
                    "nonblank bytes",
                ),
                (
                    "unclean-profile-path",
                    merge(
                        BUNDLE,
                        {
                            "profiles": {
                                "descriptors": {
                                    "default": {
                                        "user_data_dir": "/bundles/default/../user-data"
                                    }
                                }
                            }
                        },
                    ),
                    "clean path",
                ),
                (
                    "invalid-profile-directory",
                    merge(
                        BUNDLE,
                        {
                            "profiles": {
                                "descriptors": {"default": {"profile_directory": ".."}}
                            }
                        },
                    ),
                    "profile_directory",
                ),
            ]
        )
        for key in ("seccompProfile", "appArmorProfile"):
            references = [
                {"type": "Unconfined"},
                {"type": "Localhost"},
                {"type": "Localhost", "localhostProfile": ""},
                {"type": "Localhost", "localhostProfile": "../escape"},
                {"type": "Localhost", "localhostProfile": "/absolute"},
                {"type": "Localhost", "localhostProfile": "x/../escape"},
                {"type": "RuntimeDefault", "localhostProfile": "unexpected"},
                {"type": "Localhost", "localhostProfile": "white space"},
                {"type": "Localhost", "localhostProfile": "chrome", "raw": True},
            ]
            for i, reference in enumerate(references):
                invalid.append((f"bad-{key}-{i}", {"browser": {key: reference}}, key))
        invalid.append(
            (
                "legacy-apparmor",
                {
                    "podAnnotations": {
                        "container.apparmor.security.beta.kubernetes.io/api": "unconfined"
                    }
                },
                "invalid propertyName",
            )
        )
        many_profiles = {
            f"named-{i}": deepcopy(BUNDLE["profiles"]["descriptors"]["default"])
            for i in range(128)
        }
        invalid.append(
            (
                "automatic-profile-limit",
                {
                    "profiles": {
                        "bundle": BUNDLE["profiles"]["bundle"],
                        "descriptors": many_profiles,
                    }
                },
                "including the automatic default",
            )
        )
        huge_profiles = {
            f"named-{i}": dict(
                deepcopy(BUNDLE["profiles"]["descriptors"]["default"]),
                ready_selectors=["x" * 1024] * 64,
            )
            for i in range(20)
        }
        invalid.append(
            (
                "config-size-limit",
                {
                    "profiles": {
                        "bundle": BUNDLE["profiles"]["bundle"],
                        "descriptors": huge_profiles,
                    }
                },
                "1 MiB limit",
            )
        )
        invalid.extend(network_invalid)
        for host in (
            "https://capture.example.com",
            "127.0.0.1",
            "a" * 64 + ".example.com",
        ):
            invalid.append(
                (
                    "bad-ingress-host-" + str(len(invalid)),
                    merge(
                        ingress,
                        {
                            "ingress": {
                                "hosts": [
                                    {
                                        "host": host,
                                        "paths": [{"path": "/", "pathType": "Prefix"}],
                                    }
                                ]
                            }
                        },
                    ),
                    "host",
                )
            )
            invalid.append(
                (
                    "bad-route-host-" + str(len(invalid)),
                    merge(route, {"httpRoute": {"hostnames": [host]}}),
                    "hostnames",
                )
            )
        for name, changes, error in invalid:
            render(name, changes, success=False, error=error)
        validator = temp / "config-check"
        run(
            *shlex.split(os.environ.get("GO", "go")),
            "build",
            "-o",
            validator,
            ROOT / "scripts/chart-config-check.go",
            cwd=ROOT,
        )
        run(validator, *configs)
        # Derive the v1 schema from the checksummed controller's Gateway API CRD.
        settings = __import__("runpy").run_path(str(ROOT / "scripts/chart-tools.py"))
        fixture = TOOLS / "fixtures/envoy-gateway.yaml"
        assert (
            hashlib.sha256(fixture.read_bytes()).hexdigest()
            == settings["NETWORK_FIXTURES"][fixture.name][1]
        )
        crd = next(
            d
            for d in yaml.safe_load_all(fixture.read_text())
            if d
            and d["kind"] == "CustomResourceDefinition"
            and d["metadata"]["name"] == "httproutes.gateway.networking.k8s.io"
        )
        version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1")
        route_schema = temp / "httproute_v1.json"
        route_schema.write_text(json.dumps(version["schema"]["openAPIV3Schema"]))
        schema = str(TOOLS / "schemas") + "/{{.ResourceKind}}{{.KindSuffix}}.json"
        print(
            run(
                args.kubeconform,
                "-strict",
                "-summary",
                "-schema-location",
                schema,
                "-schema-location",
                str(temp) + "/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                *manifests,
            ).strip()
        )
        print(
            run(args.helm, "package", args.chart, "--destination", args.output).strip()
        )
        metadata = yaml.safe_load((args.chart / "Chart.yaml").read_text())
        package = args.output / f"webcappi-{metadata['version']}.tgz"
        with tarfile.open(package) as archive:
            actual = {
                m.name: archive.extractfile(m).read()
                for m in archive.getmembers()
                if m.isfile()
            }
        expected = {
            f"webcappi/{p.relative_to(args.chart)}": p.read_bytes()
            for p in args.chart.rglob("*")
            if p.is_file()
        }
        # Helm serializes Chart.yaml; compare its parsed metadata instead.
        assert yaml.safe_load(actual.pop("webcappi/Chart.yaml")) == yaml.safe_load(
            expected.pop("webcappi/Chart.yaml")
        )
        assert actual == expected, "packaged files differ from chart source"
        print(
            f"PASS: {len(configs)} valid configurations, {len(invalid) + helper_rejections + subchart_rejections} rejected configurations, Kubernetes schemas, Go config loader and package contents"
        )


if __name__ == "__main__":
    main()
