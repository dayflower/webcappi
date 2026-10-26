#!/usr/bin/env python3
"""Lint, render, validate Kubernetes/Go configuration, and package the chart."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
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
        ):
            values = temp / f"{name}.json"
            requested = merge(base, changes or {})
            values.write_text(json.dumps(requested))
            output = run(
                args.helm,
                "template",
                release,
                args.chart,
                "--namespace",
                "check",
                "--kube-version",
                "1.36.4",
                "-f",
                values,
                *(["--skip-schema-validation"] if skip_schema else []),
                success=success,
            )
            if not success:
                assert error in output, (name, output)
                return
            documents = list(yaml.safe_load_all(output))
            by_kind = {d["kind"]: d for d in documents}
            assert len(documents) == 3 and set(by_kind) == {
                "Deployment",
                "Service",
                "ConfigMap",
            }
            deployment, service, configmap = (
                by_kind[k] for k in ("Deployment", "Service", "ConfigMap")
            )
            pod = deployment["spec"]["template"]
            spec = pod["spec"]
            api = spec["containers"][0]
            config = json.loads(configmap["data"]["config.json"])
            assert config["no_sandbox"] is requested.get("browser", {}).get(
                "noSandbox", False
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
        memory, _, _ = render("memory")
        assert memory["spec"]["strategy"] == {"type": "Recreate"}
        explicit, _, _ = render("sandbox-enabled", {"browser": {"noSandbox": False}})
        disabled, _, _ = render("sandbox-disabled", {"browser": {"noSandbox": True}})
        assert explicit == memory, "explicit sandbox default changed the Deployment"
        old_pod = memory["spec"]["template"]
        new_pod = disabled["spec"]["template"]
        assert (
            new_pod["metadata"]["annotations"]["checksum/config"]
            != old_pod["metadata"]["annotations"]["checksum/config"]
        )
        assert new_pod["spec"] == old_pod["spec"], (
            "sandbox opt-out changed container isolation settings"
        )
        render("redis-sandbox-disabled", merge(REDIS, {"browser": {"noSandbox": True}}))
        render(
            "disabled-with-profiles",
            merge(
                BUNDLE,
                {
                    "browser": {
                        "noSandbox": True,
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
        invalid = [
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
            ("sandbox", {"capture": {"no_sandbox": True}}, "no_sandbox"),
            ("sandbox-string", {"browser": {"noSandbox": "true"}}, "noSandbox"),
            ("sandbox-number", {"browser": {"noSandbox": 1}}, "noSandbox"),
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
        schema = str(TOOLS / "schemas") + "/{{.ResourceKind}}{{.KindSuffix}}.json"
        print(
            run(
                args.kubeconform,
                "-strict",
                "-summary",
                "-schema-location",
                schema,
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
            f"PASS: {len(configs)} valid configurations, {len(invalid) + helper_rejections} rejected configurations, Kubernetes schemas, Go config loader and package contents"
        )


if __name__ == "__main__":
    main()
