"""Real Ingress and Gateway API acceptance in the smoke harness's owned cluster."""

from copy import deepcopy
import hashlib
import http.client
import json
import ssl
import time
import urllib.parse

import yaml

from smoke_support import HTTPClient, ROOT

TRAEFIK_IMAGE = "traefik:v3.7.16@sha256:c8a121e4bb1347e8fd52657fab12f2cb70b68f8a018895e2068f92c9e519b9c2"
GATEWAY_IMAGE = "envoyproxy/gateway:v1.9.3@sha256:192fc9bdadb6d2462e8cf7bd702ce3dd9648f3310c6038787448a1f509b7cd9f"
ENVOY_IMAGE = "envoyproxy/envoy:distroless-v1.39.5@sha256:9d84a586119a27b50437dbb01eccd4722832741539b1ed53c8e9d6da6067a6e0"
HOSTS = ["ingress.webcappi.test", "route.webcappi.test"]


class SNIConnection(http.client.HTTPSConnection):
    def __init__(self, *args, server_name, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_name = server_name

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self.server_name
        )


class RouteClient(HTTPClient):
    """Connect through the controller port-forward with real Host/SNI verification.

    Each request is sent once. Only readiness/status polling may repeat.
    """

    def __init__(self, base, host, authorization, context=None):
        super().__init__(base, authorization, timeout=50)
        self.host = host
        self.context = context

    def get(
        self,
        path,
        etag=None,
        authorization=...,
        data=None,
        content_type=None,
        timeout=None,
    ):
        endpoint = urllib.parse.urlsplit(self.base)
        options = {"timeout": self.timeout if timeout is None else timeout}
        connection = (
            SNIConnection(
                endpoint.hostname,
                endpoint.port,
                context=self.context,
                server_name=self.host,
                **options,
            )
            if self.context
            else http.client.HTTPConnection(endpoint.hostname, endpoint.port, **options)
        )
        headers = {"Host": self.host, "Connection": "close"}
        if authorization is ...:
            authorization = self.authorization
        if authorization is not None:
            headers["Authorization"] = authorization
        if etag:
            headers["If-None-Match"] = etag
        if content_type:
            headers["Content-Type"] = content_type
        try:
            connection.request(
                "POST" if data is not None else "GET", path, data, headers
            )
            response = connection.getresponse()
            body = response.read()
            self.recent.append(f"{path}: HTTP {response.status}, {len(body)} bytes")
            return response.status, response.headers, body
        finally:
            connection.close()


def accepted_parents(route):
    """Require fresh, controller-owned Accepted AND ResolvedRefs per desired parent."""
    generation = route["metadata"]["generation"]
    parents = route.get("status", {}).get("parents", [])
    for reference in route["spec"]["parentRefs"]:
        expected = {
            "name": reference["name"],
            "namespace": reference.get("namespace", route["metadata"]["namespace"]),
            "sectionName": reference.get("sectionName"),
        }
        found = False
        for parent in parents:
            actual = parent["parentRef"]
            key = {
                "name": actual["name"],
                "namespace": actual.get("namespace", route["metadata"]["namespace"]),
                "sectionName": actual.get("sectionName"),
            }
            if (
                key != expected
                or parent.get("controllerName")
                != "gateway.envoyproxy.io/gatewayclass-controller"
            ):
                continue
            conditions = {c["type"]: c for c in parent.get("conditions", [])}
            found = all(
                conditions.get(t, {}).get("status") == "True"
                and conditions[t].get("observedGeneration") == generation
                for t in ("Accepted", "ResolvedRefs")
            )
            if found:
                break
        if not found:
            return False
    return bool(route["spec"]["parentRefs"])


def network_suite(
    cluster,
    args,
    run,
    capture,
    sandbox_evidence,
    fixture_resources,
    image_values,
    token,
):
    if args.sandbox != "enabled":
        raise ValueError("network acceptance requires the browser sandbox enabled")
    record = {
        "result": "running",
        "controllers": {
            "ingress": TRAEFIK_IMAGE,
            "httpRoute": GATEWAY_IMAGE,
            "dataPlane": ENVOY_IMAGE,
        },
        "gateway_api": "1.6.1",
        "cases": [],
    }
    args.network_record = record
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

    def kube(*command, **kwargs):
        return run(args.kubectl, *command, env=cluster.env, **kwargs)

    def apply(*documents):
        kube(
            "apply",
            "--server-side",
            "-f",
            "-",
            input=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}),
        )

    def wait_route():
        deadline = time.monotonic() + 60
        while True:
            route = json.loads(
                cluster.kube("get", "httproute", "network-webcappi", "-o", "json")
            )
            if accepted_parents(route):
                (args.artifacts / "httproute-status.json").write_text(
                    json.dumps(route, indent=2) + "\n"
                )
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"HTTPRoute not Accepted/ResolvedRefs: {route.get('status')}"
                )
            time.sleep(0.5)

    print("Installing pinned disposable Ingress and Gateway controllers", flush=True)
    fixture = ROOT / "bin/chart-tools/fixtures/envoy-gateway.yaml"
    checksum = "ac89b0b564e52c97c97a3f8b4d82bb594a0fbc810431fb2e6ffd91702aabebfb"
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != checksum:
        raise RuntimeError(
            "Envoy Gateway fixture checksum mismatch; run make chart-tools"
        )
    documents = [d for d in yaml.safe_load_all(fixture.read_text()) if d]
    for document in documents:
        if document["kind"] in ("Deployment", "Job"):
            for container in document["spec"]["template"]["spec"]["containers"]:
                if container["image"] == "envoyproxy/gateway:v1.9.3":
                    container["image"] = GATEWAY_IMAGE
        if (
            document["kind"] == "ConfigMap"
            and document["metadata"]["name"] == "envoy-gateway-config"
        ):
            config = yaml.safe_load(document["data"]["envoy-gateway.yaml"])
            config["provider"]["kubernetes"]["shutdownManager"]["image"] = GATEWAY_IMAGE
            document["data"]["envoy-gateway.yaml"] = yaml.safe_dump(config)
    apply(*documents)
    kube(
        "wait",
        "--for=condition=Established",
        "crd/httproutes.gateway.networking.k8s.io",
        "--timeout=90s",
    )
    kube(
        "rollout",
        "status",
        "deployment/envoy-gateway",
        "-n",
        "envoy-gateway-system",
        "--timeout=180s",
        timeout=210,
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "network-system"},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "traefik", "namespace": "network-system"},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "smoke-traefik"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["services", "secrets", "nodes"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": ["discovery.k8s.io"],
                    "resources": ["endpointslices"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["ingresses", "ingressclasses"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["ingresses/status"],
                    "verbs": ["update"],
                },
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "smoke-traefik"},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "smoke-traefik",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "traefik",
                    "namespace": "network-system",
                }
            ],
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "IngressClass",
            "metadata": {"name": "traefik"},
            "spec": {"controller": "traefik.io/ingress-controller"},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "traefik", "namespace": "network-system"},
            "data": {
                "dynamic.yaml": yaml.safe_dump(
                    {
                        "http": {
                            "middlewares": {
                                "capture-body": {
                                    "buffering": {
                                        "maxRequestBodyBytes": 2097152,
                                        "memRequestBodyBytes": 2097152,
                                    }
                                }
                            },
                            "serversTransports": {
                                "capture": {
                                    "forwardingTimeouts": {
                                        "responseHeaderTimeout": "40s"
                                    }
                                }
                            },
                        }
                    }
                )
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "traefik", "namespace": "network-system"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "traefik"}},
                "template": {
                    "metadata": {"labels": {"app": "traefik"}},
                    "spec": {
                        "serviceAccountName": "traefik",
                        "containers": [
                            {
                                "name": "traefik",
                                "image": TRAEFIK_IMAGE,
                                "args": [
                                    "--providers.kubernetesingress",
                                    "--providers.kubernetesingress.ingressclass=traefik",
                                    "--providers.file.filename=/config/dynamic.yaml",
                                    "--entrypoints.web.address=:8000",
                                    "--entrypoints.websecure.address=:8443",
                                    "--entrypoints.web.transport.respondingtimeouts.readtimeout=45s",
                                    "--entrypoints.web.transport.respondingtimeouts.writetimeout=45s",
                                    "--entrypoints.websecure.transport.respondingtimeouts.readtimeout=45s",
                                    "--entrypoints.websecure.transport.respondingtimeouts.writetimeout=45s",
                                    "--accesslog=true",
                                ],
                                "ports": [
                                    {"containerPort": 8000},
                                    {"containerPort": 8443},
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/config",
                                        "readOnly": True,
                                    }
                                ],
                                "securityContext": {
                                    "runAsNonRoot": True,
                                    "runAsUser": 65532,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"memory": "512Mi"},
                                },
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": "traefik"}}
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": "smoke-envoy"},
            "spec": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"},
        },
        {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "EnvoyProxy",
            "metadata": {"name": "smoke", "namespace": "network-system"},
            "spec": {
                "provider": {
                    "type": "Kubernetes",
                    "kubernetes": {
                        "envoyDeployment": {"container": {"image": ENVOY_IMAGE}},
                        "envoyService": {"type": "ClusterIP"},
                    },
                }
            },
        },
    )
    certificate, key = cluster.root / "network.crt", cluster.root / "network.key"
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
        "/CN=webcappi.test",
        "-addext",
        "subjectAltName=" + ",".join("DNS:" + host for host in HOSTS),
    )
    for namespace in ["smoke", "network-system"]:
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "network-tls", "namespace": namespace},
                "type": "kubernetes.io/tls",
                "stringData": {
                    "tls.crt": certificate.read_text(),
                    "tls.key": key.read_text(),
                },
            }
        )
    listeners = []
    for name, port, protocol in [("http", 8080, "HTTP"), ("https", 8443, "HTTPS")]:
        listener = {
            "name": name,
            "port": port,
            "protocol": protocol,
            "hostname": HOSTS[1],
            "allowedRoutes": {
                "namespaces": {
                    "from": "Selector",
                    "selector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "smoke"}
                    },
                }
            },
        }
        if protocol == "HTTPS":
            listener["tls"] = {
                "mode": "Terminate",
                "certificateRefs": [{"name": "network-tls"}],
            }
        listeners.append(listener)
    apply(
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": "public", "namespace": "network-system"},
            "spec": {
                "gatewayClassName": "smoke-envoy",
                "infrastructure": {
                    "parametersRef": {
                        "group": "gateway.envoyproxy.io",
                        "kind": "EnvoyProxy",
                        "name": "smoke",
                    }
                },
                "listeners": listeners,
            },
        }
    )
    kube(
        "rollout",
        "status",
        "deployment/traefik",
        "-n",
        "network-system",
        "--timeout=120s",
    )
    cluster.load(args.image)
    fixture_resources(cluster)
    values = {
        "image": image_values(args.image),
        "browser": {"noSandbox": False, **cluster.policy.values},
        "auth": {"existingSecret": "api-auth", "tokenKey": "access-token"},
        "capture": {"maxPOSTBytes": 2097152},
        "service": {
            "port": 9090,
            "annotations": {
                "traefik.ingress.kubernetes.io/service.serverstransport": "capture@file"
            },
        },
    }
    path = "/capture?" + urllib.parse.urlencode(
        {
            "url": "http://fixture.smoke.svc.cluster.local:8000/",
            "width": 320,
            "height": 200,
            "ready_selector": "#page-ready",
        }
    )
    cluster.upgrade("network", values)
    sandbox_evidence(cluster, cluster.pods("network")[0], args)
    context = ssl.create_default_context(cafile=str(certificate))

    def proxy_resource(kind):
        if kind == "ingress":
            return "deployment/traefik", "network-system", 8000
        deadline = time.monotonic() + 90
        while True:
            services = json.loads(
                kube(
                    "get",
                    "services",
                    "-n",
                    "envoy-gateway-system",
                    "-l",
                    "gateway.envoyproxy.io/owning-gateway-name=public",
                    "-o",
                    "json",
                )
            )["items"]
            if services:
                return (
                    "service/" + services[0]["metadata"]["name"],
                    "envoy-gateway-system",
                    8080,
                )
            if time.monotonic() >= deadline:
                raise AssertionError("Envoy Gateway did not create a proxy Service")
            time.sleep(0.5)

    for kind, host in zip(("ingress", "httpRoute"), HOSTS):
        configured = deepcopy(values)
        if kind == "ingress":
            configured[kind] = {
                "enabled": True,
                "className": "traefik",
                "annotations": {
                    "traefik.ingress.kubernetes.io/router.entrypoints": "web",
                    "traefik.ingress.kubernetes.io/router.middlewares": "capture-body@file",
                },
                "hosts": [
                    {"host": host, "paths": [{"path": "/", "pathType": "Prefix"}]}
                ],
            }
        else:
            configured[kind] = {
                "enabled": True,
                "parentRefs": [
                    {
                        "name": "public",
                        "namespace": "network-system",
                        "sectionName": "http",
                    }
                ],
                "hostnames": [host],
                "timeouts": {"request": "45s", "backendRequest": "40s"},
            }
        cluster.upgrade("network", configured)
        if kind == "httpRoute":
            wait_route()
        resource, namespace, port = proxy_resource(kind)
        # The helper forwards only the owned cluster; namespace selects its controllers.
        with cluster.forward(resource, port=port, namespace=namespace) as forwarded:
            client = RouteClient(forwarded.base, host, "Bearer " + token)
            client.wait_ready(timeout=90)
            capture(client, path, args.artifacts, kind + "-http")
        revision = json.loads(cluster.helm("history", "network", "-o", "json"))[-1][
            "revision"
        ]
        tls_values = deepcopy(configured)
        if kind == "ingress":
            tls_values[kind]["annotations"].update(
                {
                    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                }
            )
            tls_values[kind]["tls"] = [{"secretName": "network-tls", "hosts": [host]}]
        else:
            tls_values[kind]["parentRefs"][0]["sectionName"] = "https"
        cluster.upgrade("network", tls_values)
        if kind == "httpRoute":
            wait_route()
        with cluster.forward(resource, port=8443, namespace=namespace) as forwarded:
            client = RouteClient(forwarded.base, host, "Bearer " + token, context)
            client.wait_ready(timeout=60)
            capture(client, path, args.artifacts, kind + "-tls")
            post_path = "/capture?width=320&height=200&ready_selector=%23slow-ready"
            html = (
                b'<body><script>setTimeout(()=>{document.body.id="slow-ready"},3500)</script>'
                + b" " * 1100000
            )
            begin = time.monotonic()
            status, _, body = client.get(post_path, data=html, content_type="text/html")
            assert status == 200 and body.startswith(b"\x89PNG"), (status, body[:256])
            assert time.monotonic() - begin >= 3.5, "selector delay was bypassed"
            (args.artifacts / (kind + "-delayed-post.png")).write_bytes(body)
            before = sum(
                int(cluster.kube("logs", pod, "-c", "api").count("capture started"))
                for pod in cluster.pods("network")
            )
            assert (
                client.get("/capture", data=b" " * 2097153, content_type="text/html")[0]
                == 413
            )
            after = sum(
                int(cluster.kube("logs", pod, "-c", "api").count("capture started"))
                for pod in cluster.pods("network")
            )
            assert after == before, "oversized body started Chromium"
        # A rejected SNI can close kubectl's stream; isolate this negative handshake.
        with cluster.forward(resource, port=8443, namespace=namespace) as forwarded:
            try:
                RouteClient(forwarded.base, "wrong.webcappi.test", None, context).get(
                    "/healthz"
                )
            except (ssl.SSLCertVerificationError, ssl.SSLEOFError):
                pass
            else:
                raise AssertionError("TLS hostname verification was bypassed")
        # A host change makes the old rule stop matching; poll health only, never captures.
        changed = deepcopy(tls_values)
        if kind == "ingress":
            changed[kind]["hosts"][0]["host"] = "changed.webcappi.test"
        else:
            changed[kind]["paths"] = [{"type": "Exact", "value": "/healthz"}]
        cluster.upgrade("network", changed)
        if kind == "httpRoute":
            wait_route()
        with cluster.forward(resource, port=8443, namespace=namespace) as forwarded:
            client = RouteClient(forwarded.base, host, "Bearer " + token, context)
            deadline = time.monotonic() + 60
            probe = "/readyz"
            while client.get(probe, authorization=None)[0] != 404:
                if time.monotonic() >= deadline:
                    raise AssertionError("route update did not remove the old rule")
                time.sleep(0.5)
        cluster.helm(
            "rollback",
            "network",
            str(revision),
            "--wait",
            "--timeout",
            "180s",
            timeout=210,
        )
        if kind == "httpRoute":
            wait_route()
        with cluster.forward(resource, port=port, namespace=namespace) as forwarded:
            client = RouteClient(forwarded.base, host, "Bearer " + token)
            client.wait_ready(timeout=60)
            capture(client, path, args.artifacts, kind + "-rollback")
        if kind == "httpRoute":
            # Prove each deadline is enforced, rather than merely accepted by the API.
            for timeout_name, deadlines in [
                ("backendRequest", {"request": "12s", "backendRequest": "6s"}),
                ("request", {"request": "6s"}),
            ]:
                short = deepcopy(configured)
                short[kind]["timeouts"] = deadlines
                cluster.upgrade("network", short)
                wait_route()
                with cluster.forward(
                    resource, port=port, namespace=namespace
                ) as forwarded:
                    client = RouteClient(forwarded.base, host, "Bearer " + token)
                    client.wait_ready(timeout=60)
                    time.sleep(2)
                    before = sum(
                        cluster.kube("logs", pod, "-c", "api").count("capture started")
                        for pod in cluster.pods("network")
                    )
                    begin = time.monotonic()
                    status, _, _ = client.get(
                        "/capture?width=320&height=200&ready_selector=%23never",
                        data=b"<body>timeout</body>",
                        content_type="text/html",
                    )
                    elapsed = time.monotonic() - begin
                    assert status == 504 and 4 <= elapsed < 10, (
                        timeout_name,
                        status,
                        elapsed,
                    )
                    time.sleep(1)
                    after = sum(
                        cluster.kube("logs", pod, "-c", "api").count("capture started")
                        for pod in cluster.pods("network")
                    )
                    assert after - before == 1, (
                        "expected one capture without retries",
                        before,
                        after,
                    )
                    deadline = time.monotonic() + 15
                    while any(
                        cluster.kube(
                            "exec",
                            pod,
                            "--",
                            "find",
                            "/work/captures",
                            "-maxdepth",
                            "1",
                            "-type",
                            "d",
                            "-name",
                            "capture-*",
                        )
                        for pod in cluster.pods("network")
                    ):
                        if time.monotonic() >= deadline:
                            raise AssertionError(
                                "proxy cancellation left capture scratch directories"
                            )
                        time.sleep(0.5)
                    record[timeout_name + "_timeout_seconds"] = elapsed
        record["cases"].append(
            {
                "kind": kind,
                "http": True,
                "tls": True,
                "auth": True,
                "delayed_post_bytes": len(html),
                "oversize_rejected": True,
                "update": True,
                "rollback": True,
            }
        )
        cluster.upgrade("network", values)
        print(
            f"PASS: {kind} HTTP/TLS, auth, delayed large POST, body limit, update and rollback",
            flush=True,
        )
    cluster.uninstall("network")
    assert not json.loads(
        cluster.kube(
            "get",
            "ingress,httproute",
            "-l",
            "app.kubernetes.io/instance=network",
            "-o",
            "json",
        )
    )["items"]
    kube("get", "gateway", "public", "-n", "network-system", "-o", "name")
    cluster.kube("get", "secret", "network-tls", "-o", "name")
    record["external_resources_preserved"] = True
    record["result"] = "passed"
    (args.artifacts / "network.json").write_text(json.dumps(record, indent=2) + "\n")
