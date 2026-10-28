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
    def test_shared_fixtures_keep_the_original_redis_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            cluster = Mock(root=Path(directory), fixtures_ready=False)

            def openssl(*args, **kwargs):
                (cluster.root / "redis.crt").write_text("original CA")
                (cluster.root / "redis.key").write_text("private fixture key")

            with patch.object(SMOKE, "run", side_effect=openssl) as run:
                SMOKE.fixture_resources(cluster)
                SMOKE.fixture_resources(cluster)
            self.assertEqual(run.call_count, 1)
            self.assertEqual((cluster.root / "redis.crt").read_text(), "original CA")

    def test_distribution_image_comes_from_the_explicit_archive_defaults(self):
        image = {
            "repository": "ghcr.io/example/api",
            "tag": "1.2.3",
            "digest": "sha256:" + "a" * 64,
        }
        with patch.object(
            SMOKE, "run", return_value=json.dumps({"image": image})
        ) as run:
            args = SMOKE.parse_args(
                [
                    "--chart",
                    "/tmp/distribution.tgz",
                    "--chart-default-image",
                    "--image",
                    "ignored:test",
                ]
            )
        self.assertEqual(args.image, image["repository"] + "@" + image["digest"])
        self.assertEqual(run.call_args.args[-1], Path("/tmp/distribution.tgz"))

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


class PolicyFailureTest(unittest.TestCase):
    def test_permission_denial_is_recorded_without_unsandboxed_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            cluster = Mock()
            cluster.policy.apparmor = False
            cluster.kube.side_effect = [
                json.dumps(
                    {
                        "spec": {
                            "template": {
                                "spec": {"containers": [{"securityContext": {}}]}
                            }
                        }
                    }
                ),
                json.dumps({"status": {"phase": "Failed"}}),
                "Permission denied (13)",
                "",
                json.dumps({"status": {"phase": "Failed"}}),
                "Permission denied (13)",
                "",
            ]
            SMOKE.policy_failures(
                cluster, argparse.Namespace(artifacts=Path(directory))
            )
            for name in ("missing-seccomp", "incompatible-seccomp"):
                report = json.loads((Path(directory) / (name + ".json")).read_text())
                self.assertFalse(report["no_sandbox"])
                self.assertNotIn("--no-sandbox", report["browser_arguments"])
            self.assertEqual(cluster.apply.call_count, 2)

    def test_successful_browser_with_incompatible_policy_fails_and_deletes_pod(self):
        cluster = Mock()
        cluster.policy.apparmor = False
        cluster.kube.side_effect = [
            json.dumps(
                {
                    "spec": {
                        "template": {"spec": {"containers": [{"securityContext": {}}]}}
                    }
                }
            ),
            json.dumps({"status": {"phase": "Succeeded"}}),
            "",
        ]
        with self.assertRaisesRegex(AssertionError, "unexpectedly"):
            SMOKE.policy_failures(cluster, argparse.Namespace())
        self.assertEqual(
            cluster.kube.call_args.args[:3], ("delete", "pod", "missing-seccomp")
        )


class DistributionSmokeTest(unittest.TestCase):
    def test_distribution_mode_requires_enabled_defaults_and_excludes_network(self):
        for flags in (
            ["--distribution-only"],
            ["--distribution-only", "--chart-default-image", "--sandbox", "disabled"],
            ["--distribution-only", "--chart-default-image", "--network-only"],
        ):
            with (
                self.subTest(flags=flags),
                patch.object(SMOKE, "run") as run,
                patch("sys.stderr"),
            ):
                with self.assertRaises(SystemExit):
                    SMOKE.parse_args(flags)
                run.assert_not_called()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                SMOKE,
                "run",
                return_value='{"image":{"repository":"webcappi","tag":"test"}}',
            ),
        ):
            args = SMOKE.parse_args(["--distribution-only", "--chart-default-image"])
        self.assertEqual(args.artifacts, SMOKE.ROOT / "artifacts/helm-distribution")

    def test_distribution_checks_diagnostic_sandbox_without_lifecycle(self):
        cluster = Mock()
        cluster.policy.values = {"seccompProfile": {"type": "Localhost"}}
        cluster.pods.return_value = ["sandbox-pod"]
        args = argparse.Namespace(image="diagnostic:test")
        with patch.object(SMOKE, "sandbox_evidence") as evidence:
            SMOKE.distribution_smoke(cluster, args)
        cluster.load.assert_called_once_with(args.image)
        values = cluster.upgrade.call_args.args[1]
        self.assertTrue(values["browser"]["sandbox"])
        self.assertEqual(values["image"], SMOKE.image_values(args.image))
        evidence.assert_called_once_with(cluster, "sandbox-pod", args)
        cluster.uninstall.assert_called_once_with("sandbox")

    def test_distribution_main_routes_checks_and_reports_success_only_after_cleanup(
        self,
    ):
        for failure_at in (None, "capture", "sandbox", "cleanup", "interruption"):
            with (
                self.subTest(failure_at=failure_at),
                tempfile.TemporaryDirectory() as temp,
            ):
                args = argparse.Namespace(
                    image="webcappi:test",
                    sandbox="enabled",
                    artifacts=Path(temp),
                    chart_default_image=True,
                    distribution_only=True,
                    network_only=False,
                )
                failure = (
                    KeyboardInterrupt("interrupted")
                    if failure_at == "interruption"
                    else RuntimeError("failed")
                )
                with (
                    patch.object(SMOKE, "parse_args", return_value=args),
                    patch.object(SMOKE.platform, "system", return_value="Linux"),
                    patch.object(SMOKE.platform, "machine", return_value="x86_64"),
                    patch.object(SMOKE, "run", return_value="linux/amd64"),
                    patch.object(SMOKE, "diagnostic_image"),
                    patch.object(SMOKE, "Cluster") as cluster_type,
                    patch.object(SMOKE, "default_image_capture") as capture,
                    patch.object(SMOKE, "distribution_smoke") as distribution,
                    patch.object(SMOKE, "suite") as lifecycle,
                ):
                    cluster = cluster_type.return_value
                    if failure_at == "capture":
                        capture.side_effect = failure
                    if failure_at in ("sandbox", "interruption"):
                        distribution.side_effect = failure
                    if failure_at == "cleanup":
                        cluster.__exit__.side_effect = failure
                    if failure_at:
                        with self.assertRaises(type(failure)):
                            SMOKE.main()
                    else:
                        SMOKE.main()
                    cluster.__exit__.assert_called_once()
                    capture.assert_called_once()
                    if failure_at == "capture":
                        distribution.assert_not_called()
                    else:
                        distribution.assert_called_once()
                    lifecycle.assert_not_called()
                result = json.loads((args.artifacts / "result.json").read_text())
                self.assertEqual(result["result"], "failed" if failure_at else "passed")
                self.assertEqual(result["suite"], "distribution")


if __name__ == "__main__":
    unittest.main()
