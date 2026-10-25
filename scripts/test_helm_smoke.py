"""Check owned-cluster cleanup even when creation or execution is interrupted."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "helm_smoke", Path(__file__).with_name("helm-smoke.py")
)
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class ClusterLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = argparse.Namespace(
            helm="helm",
            kind="kind",
            kubectl="kubectl",
            artifacts=self.root / "artifacts",
        )

    def test_ambient_cluster_and_helm_overrides_are_removed(self):
        with patch.dict(
            os.environ,
            {
                "KUBECONFIG": "/production",
                "HELM_KUBEAPISERVER": "https://production",
                "HELM_NAMESPACE": "production",
                "KIND_EXPERIMENTAL_DOCKER_NETWORK": "custom-network",
            },
        ):
            cluster = SMOKE.Cluster(self.args, self.root)
        self.assertEqual(cluster.env["KUBECONFIG"], str(self.root / "kubeconfig"))
        self.assertNotIn("HELM_KUBEAPISERVER", cluster.env)
        self.assertNotIn("HELM_NAMESPACE", cluster.env)
        self.assertNotIn("KIND_EXPERIMENTAL_DOCKER_NETWORK", cluster.env)
        self.assertEqual(cluster.env["HELM_DRIVER"], "secret")

    def test_failed_creation_deletes_partially_created_cluster(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(
                SMOKE, "run", side_effect=[RuntimeError("create failed"), ""]
            ) as run,
            patch.object(cluster, "diagnostics"),
        ):
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                cluster.__enter__()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[:3], ("kind", "create", "cluster"))
        self.assertEqual(run.call_args_list[1].args[:3], ("kind", "delete", "cluster"))
        self.assertEqual(run.call_args_list[1].args[4], cluster.name)

    def test_interrupt_during_creation_still_cleans_up(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(SMOKE, "run", side_effect=[KeyboardInterrupt(), ""]) as run,
            patch.object(cluster, "diagnostics"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                cluster.__enter__()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[:3], ("kind", "delete", "cluster"))

    def test_failure_and_interrupt_in_suite_delete_owned_cluster(self):
        for failure in [RuntimeError("capture failed"), KeyboardInterrupt()]:
            with self.subTest(failure=type(failure).__name__):
                cluster = SMOKE.Cluster(self.args, self.root)
                version = json.dumps(
                    {
                        "serverVersion": {
                            "gitVersion": "v"
                            + SMOKE.TOOL_SETTINGS["KUBERNETES_VERSION"]
                        }
                    }
                )
                with (
                    patch.object(
                        SMOKE, "run", side_effect=["", "", version, ""]
                    ) as run,
                    patch.object(cluster, "diagnostics"),
                ):
                    with self.assertRaises(type(failure)):
                        with cluster:
                            raise failure
                self.assertEqual(run.call_args.args[:3], ("kind", "delete", "cluster"))

    def test_success_uses_default_network_without_managing_docker_networks(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        version = json.dumps(
            {
                "serverVersion": {
                    "gitVersion": "v" + SMOKE.TOOL_SETTINGS["KUBERNETES_VERSION"]
                }
            }
        )
        with (
            patch.object(SMOKE, "run", side_effect=["", "", version, ""]) as run,
            patch.object(cluster, "diagnostics"),
        ):
            with cluster:
                pass
        self.assertEqual(run.call_count, 4)
        self.assertEqual(run.call_args_list[0].args[:3], ("kind", "create", "cluster"))
        self.assertEqual(run.call_args.args[:3], ("kind", "delete", "cluster"))
        self.assertTrue(
            all(call.args[0] in ("kind", "kubectl") for call in run.call_args_list)
        )
        self.assertNotIn(
            "KIND_EXPERIMENTAL_DOCKER_NETWORK", run.call_args_list[0].kwargs["env"]
        )

    def test_failed_creation_diagnostics_still_delete_resources(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(
                SMOKE, "run", side_effect=[RuntimeError("create failed"), ""]
            ) as run,
            patch.object(
                cluster, "diagnostics", side_effect=RuntimeError("diagnostics")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "diagnostics"):
                cluster.__enter__()
        self.assertEqual(run.call_args.args[:3], ("kind", "delete", "cluster"))

    def test_diagnostic_failure_does_not_skip_cleanup(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(
                cluster, "diagnostics", side_effect=RuntimeError("diagnostics")
            ),
            patch.object(cluster, "cleanup") as cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "diagnostics"):
                cluster.__exit__(None, None, None)
        cleanup.assert_called_once()

    def test_cleanup_failure_is_not_reported_as_success(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(cluster, "diagnostics"),
            patch.object(cluster, "cleanup", side_effect=RuntimeError("delete failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "delete failed"):
                cluster.__exit__(None, None, None)

    def test_sigterm_unwinds_contexts(self):
        with self.assertRaises(KeyboardInterrupt):
            SMOKE.interrupted(signal.SIGTERM, None)

    def test_uninstall_waits_for_cascading_pod_deletion(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(cluster, "helm") as helm,
            patch.object(
                cluster,
                "kube",
                side_effect=[
                    json.dumps({"items": [{"metadata": {"name": "terminating"}}]}),
                    json.dumps({"items": []}),
                ],
            ),
            patch.object(SMOKE.time, "sleep") as sleep,
        ):
            cluster.uninstall("capture")
        helm.assert_called_once_with(
            "uninstall", "capture", "--wait", "--timeout", "60s"
        )
        sleep.assert_called_once()

    def test_uninstall_does_not_succeed_with_remaining_pods(self):
        cluster = SMOKE.Cluster(self.args, self.root)
        with (
            patch.object(cluster, "helm"),
            patch.object(
                cluster,
                "kube",
                return_value=json.dumps({"items": [{"metadata": {"name": "stuck"}}]}),
            ),
            patch.object(SMOKE.time, "monotonic", side_effect=[0, 61]),
        ):
            with self.assertRaisesRegex(AssertionError, "uninstall left Pods"):
                cluster.uninstall("capture")

    def test_failed_inspection_with_json_stdout_is_still_missing(self):
        result = subprocess.CompletedProcess(["docker"], 1, "[]\n", "not found")
        with patch.object(SMOKE.subprocess, "run", return_value=result):
            self.assertIsNone(
                SMOKE.run("docker", "image", "inspect", "missing", check=False)
            )

    def test_image_cleanup_skips_aliases_that_were_never_created(self):
        with patch.object(SMOKE, "run", side_effect=["present", "", None]) as run:
            with SMOKE.image_aliases() as images:
                images.extend(["missing", "present"])
        self.assertEqual(
            run.call_args_list[1].args, ("docker", "image", "rm", "present")
        )
        self.assertEqual(run.call_count, 3)

    def test_image_cleanup_failure_still_checks_remaining_aliases(self):
        with patch.object(
            SMOKE, "run", side_effect=["present", RuntimeError("remove failed"), None]
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "image cleanup failed"):
                with SMOKE.image_aliases() as images:
                    images.extend(["missing", "present"])
        self.assertEqual(run.call_args.args, ("docker", "image", "inspect", "missing"))


class SandboxModeTest(unittest.TestCase):
    def test_default_preserves_enabled_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            args = SMOKE.parse_args([])
        self.assertEqual(args.sandbox, "enabled")
        self.assertEqual(args.artifacts, SMOKE.ROOT / "artifacts/helm-enabled")

    def test_explicit_disabled_mode_has_separate_artifacts(self):
        with patch.dict(os.environ, {"WEBCAPPI_ARTIFACTS_DIR": "/tmp/browser-check"}):
            args = SMOKE.parse_args(["--sandbox", "disabled"])
        self.assertEqual(args.sandbox, "disabled")
        self.assertEqual(args.artifacts, Path("/tmp/browser-check/helm-disabled"))

    def test_invalid_mode_does_not_start_any_process(self):
        with patch.object(SMOKE, "run") as run, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                SMOKE.parse_args(["--sandbox", "auto"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_result_reports_requested_mode_even_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(
                artifacts=Path(temp),
                sandbox="disabled",
                image="webcappi:test",
                runtime_platform="linux/amd64",
            )
            SMOKE.write_result(args, "failed", RuntimeError("capture failed"))
            report = json.loads((args.artifacts / "result.json").read_text())
            self.assertFalse(report["sandbox"])
            self.assertEqual(report["sandbox_mode"], "disabled")
            self.assertEqual(report["result"], "failed")
            self.assertEqual(report["error"], "capture failed")
            args.sandbox = "enabled"
            SMOKE.write_result(args, "passed")
            report = json.loads((args.artifacts / "result.json").read_text())
            self.assertTrue(report["sandbox"])
            self.assertEqual(report["sandbox_mode"], "enabled")
            self.assertNotIn("error", report)

    def test_enabled_profile_generation_uses_the_real_browser(self):
        cluster = Mock()
        cluster.kube.side_effect = ["", b"archive", ""]
        self.assertEqual(
            SMOKE.export_profile(cluster, "api-pod", "enabled"), b"archive"
        )
        self.assertNotIn("-browser-path", cluster.kube.call_args_list[0].args)
        self.assertNotIn("helm-chrome-test", repr(cluster.kube.call_args_list))

    def test_disabled_profile_generation_has_an_explicit_temporary_wrapper(self):
        cluster = Mock()
        cluster.kube.side_effect = ["", "", b"archive", ""]
        self.assertEqual(
            SMOKE.export_profile(cluster, "api-pod", "disabled"), b"archive"
        )
        self.assertIn("--no-sandbox", cluster.kube.call_args_list[0].kwargs["input"])
        self.assertIn("-browser-path", cluster.kube.call_args_list[1].args)
        self.assertIn("/tmp/helm-chrome-test", cluster.kube.call_args_list[-1].args)

    def test_profile_failure_is_not_retried_with_another_mode(self):
        for mode, returns in [
            ("enabled", [RuntimeError("browser failed"), ""]),
            ("disabled", ["", RuntimeError("browser failed"), ""]),
        ]:
            with self.subTest(mode=mode):
                cluster = Mock()
                cluster.kube.side_effect = returns
                with self.assertRaisesRegex(RuntimeError, "browser failed"):
                    SMOKE.export_profile(cluster, "api-pod", mode)
                self.assertEqual(cluster.kube.call_count, len(returns))
                self.assertEqual(
                    cluster.kube.call_args.args[:5],
                    ("exec", "api-pod", "--", "rm", "-rf"),
                )


if __name__ == "__main__":
    unittest.main()
