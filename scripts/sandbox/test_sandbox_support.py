"""Verify policy ownership, preparation failures and cleanup without host mutations."""

from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import Mock, patch

from . import sandbox_support as sandbox


class SandboxPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = Mock(return_value="")
        with patch.object(sandbox, "apparmor_enabled", return_value=True):
            self.policy = sandbox.SandboxPolicy(
                "webcappi-helm-0123456789ab", self.root, self.run, self.root / "parser"
            )

    def test_requires_owned_node(self):
        with self.assertRaisesRegex(ValueError, "owned"):
            sandbox.SandboxPolicy("production", self.root, self.run, "/bin/true")

    def test_missing_parser_fails_before_cluster_setup(self):
        with self.assertRaisesRegex(RuntimeError, "make chart-tools"):
            self.policy.configuration()
        self.run.assert_not_called()

    def test_existing_profile_is_never_replaced_or_removed(self):
        self.run.return_value = self.policy.name + " (enforce)"
        with self.assertRaisesRegex(RuntimeError, "existing"):
            self.policy.install()
        self.policy.cleanup()
        self.assertFalse(self.policy.load_attempted)
        self.assertEqual(self.run.call_count, 1)

    def test_partial_load_failure_is_cleaned(self):
        self.root.joinpath("chrome-apparmor.bin").write_bytes(b"compiled")
        self.run.side_effect = ["", KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.policy.install()
        self.assertTrue(self.policy.load_attempted)
        self.run.side_effect = [
            "",
            "sandbox-1\nsandbox-2",
            "",
            "",
            self.policy.name + " (enforce)",
            "",
            "",
        ]
        self.policy.cleanup()
        calls = [c.args[3:] for c in self.run.call_args_list[2:]]
        self.assertEqual(calls[0], ("systemctl", "stop", "kubelet"))
        self.assertEqual(calls[2], ("crictl", "stopp", "sandbox-1"))
        self.assertEqual(calls[3], ("crictl", "stopp", "sandbox-2"))
        self.assertIn(".remove", calls[5][2])
        self.assertTrue(self.policy.record["apparmor_removed"])

    def test_workload_stop_failure_never_unloads_live_profile(self):
        self.policy.load_attempted = True
        self.run.side_effect = ["", "sandbox-1", RuntimeError("stop failed")]
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            self.policy.cleanup()
        self.assertFalse(any(".remove" in str(c) for c in self.run.call_args_list))

    def test_no_apparmor_host_uses_seccomp_only(self):
        self.policy.apparmor = False
        self.assertEqual(self.policy.configuration()["nodes"][0]["extraMounts"], [])
        self.assertNotIn("appArmorProfile", self.policy.values)
        self.policy.cleanup()
        self.run.assert_not_called()


class PolicyIsolationTest(unittest.TestCase):
    def test_namespace_exception_keeps_other_privileged_syscalls_blocked(self):
        profile = json.loads((sandbox.POLICY / "chrome-seccomp.json").read_text())
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        allowed = [
            rule for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"
        ]
        names = {name for rule in allowed for name in rule["names"]}
        self.assertTrue({"mount", "setns", "bpf", "clone3"}.isdisjoint(names))

        def permits(name, flags):
            for rule in allowed:
                if name not in rule["names"]:
                    continue
                self.assertTrue(rule.get("args"), "namespace call lacks restrictions")
                matches = []
                for arg in rule["args"]:
                    self.assertEqual(arg["index"], 0)
                    if arg["op"] == "SCMP_CMP_EQ":
                        matches.append(flags == arg["value"])
                    else:
                        self.assertEqual(arg["op"], "SCMP_CMP_MASKED_EQ")
                        matches.append(flags & arg["value"] == arg.get("valueTwo", 0))
                if all(matches):
                    return True
            return False

        self.assertTrue(permits("clone", 17))
        self.assertTrue(permits("clone", 0x70000000 | 17))
        self.assertTrue(permits("unshare", 0x10000000))
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 1 << 32, 1 << 63):
            self.assertFalse(permits("clone", flag | 17))
            self.assertFalse(permits("clone", flag | 0x70000000 | 17))
            self.assertFalse(permits("unshare", flag))
            self.assertFalse(permits("unshare", flag | 0x10000000))


if __name__ == "__main__":
    unittest.main()
