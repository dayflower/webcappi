"""Reject stale/foreign route status and preserve single-attempt proxy requests."""

from copy import deepcopy
import http.server
import importlib.util
from pathlib import Path
import threading
import unittest

from helm_network import RouteClient, accepted_parents


class RouteStatusTest(unittest.TestCase):
    def route(self):
        reference = {
            "name": "public",
            "namespace": "network-system",
            "sectionName": "https",
        }
        return {
            "metadata": {"namespace": "smoke", "generation": 3},
            "spec": {"parentRefs": [reference]},
            "status": {
                "parents": [
                    {
                        "parentRef": reference,
                        "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                        "conditions": [
                            {"type": name, "status": "True", "observedGeneration": 3}
                            for name in ("Accepted", "ResolvedRefs")
                        ],
                    }
                ]
            },
        }

    def test_only_fresh_successful_status_qualifies(self):
        route = self.route()
        self.assertTrue(accepted_parents(route))
        for field, value in [("status", "False"), ("observedGeneration", 2)]:
            for index in (0, 1):
                invalid = deepcopy(route)
                invalid["status"]["parents"][0]["conditions"][index][field] = value
                self.assertFalse(accepted_parents(invalid))
        route["status"]["parents"][0]["controllerName"] = "unrelated/controller"
        self.assertFalse(accepted_parents(route))

    def test_every_listener_and_namespace_must_match(self):
        route = self.route()
        route["spec"]["parentRefs"].append(
            {"name": "public", "namespace": "network-system", "sectionName": "http"}
        )
        self.assertFalse(accepted_parents(route))
        route = self.route()
        route["status"]["parents"][0]["parentRef"] = {
            "name": "public",
            "sectionName": "https",
        }
        self.assertFalse(accepted_parents(route))
        route["spec"]["parentRefs"][0].pop("namespace")
        self.assertTrue(accepted_parents(route))

    def test_empty_or_missing_status_is_not_acceptance(self):
        route = self.route()
        route.pop("status")
        self.assertFalse(accepted_parents(route))
        route["spec"]["parentRefs"] = []
        self.assertFalse(accepted_parents(route))


class SingleAttemptTest(unittest.TestCase):
    def test_post_errors_and_redirects_are_never_retried(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(
                    (
                        self.path,
                        self.headers["Host"],
                        self.rfile.read(int(self.headers["Content-Length"])),
                    )
                )
                self.send_response(int(self.path[1:]))
                self.send_header("Location", "/unexpected-retry")
                self.end_headers()

            def log_message(self, *args):
                pass

        with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                client = RouteClient(
                    f"http://127.0.0.1:{server.server_port}",
                    "capture.test",
                    "Bearer test",
                )
                for code in (302, 503, 504):
                    self.assertEqual(
                        client.get(
                            f"/{code}", data=b"capture", content_type="text/html"
                        )[0],
                        code,
                    )
            finally:
                server.shutdown()
                worker.join(timeout=5)
        self.assertEqual(
            requests,
            [(f"/{code}", "capture.test", b"capture") for code in (302, 503, 504)],
        )

    def test_disabled_sandbox_is_rejected_before_cluster_creation(self):
        spec = importlib.util.spec_from_file_location(
            "network_args_smoke", Path(__file__).with_name("helm-smoke.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaises(SystemExit):
            module.parse_args(["--network-only", "--sandbox", "disabled"])


if __name__ == "__main__":
    unittest.main()
