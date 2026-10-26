"""Prepare scoped Chromium sandbox policy in an owned, disposable kind node."""

import hashlib
import json
from pathlib import Path
import platform
import re

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deploy/sandbox"


def apparmor_enabled():
    path = Path("/sys/module/apparmor/parameters/enabled")
    return path.exists() and path.read_text().strip() == "Y"


class SandboxPolicy:
    def __init__(self, name, root, run, parser):
        if not re.fullmatch(r"webcappi-helm-[a-f0-9]{12}", name):
            raise ValueError("policy preparation requires an owned kind node")
        self.node = name + "-control-plane"
        self.name = name + "-chrome"
        self.root = root
        self.run = run
        self.parser = Path(parser).resolve()
        self.apparmor = apparmor_enabled()
        self.load_attempted = False
        self.additional_profiles = []
        self.record = {
            "host_kernel": platform.release(),
            "host_os": platform.freedesktop_os_release(),
            "apparmor": self.apparmor,
            "host_userns": {},
        }
        for key in (
            "kernel/unprivileged_userns_clone",
            "kernel/apparmor_restrict_unprivileged_userns",
            "user/max_user_namespaces",
        ):
            path = Path("/proc/sys") / key
            if path.exists():
                self.record["host_userns"][key] = path.read_text().strip()
        if (
            self.record["host_userns"].get("kernel/unprivileged_userns_clone") == "0"
            or self.record["host_userns"].get("user/max_user_namespaces") == "0"
        ):
            raise RuntimeError(
                "host disables unprivileged user namespaces; operator preparation is required"
            )

    @property
    def values(self):
        browser = {
            "seccompProfile": {
                "type": "Localhost",
                "localhostProfile": "webcappi/chrome.json",
            }
        }
        if self.apparmor:
            browser["appArmorProfile"] = {
                "type": "Localhost",
                "localhostProfile": self.name,
            }
        return browser

    def configuration(self):
        mounts = []
        if self.apparmor:
            if not self.parser.is_file():
                raise RuntimeError(
                    "missing pinned AppArmor parser; run make chart-tools"
                )
            source = (
                (POLICY / "chrome-apparmor")
                .read_text()
                .replace("WEBCAPPI_PROFILE", self.name)
            )
            (self.root / "chrome-apparmor").write_text(source)
            self.run(
                self.parser,
                "-I",
                self.parser.parent / "apparmor",
                "-Q",
                "-K",
                "-o",
                self.root / "chrome-apparmor.bin",
                self.root / "chrome-apparmor",
            )
            mounts = [
                {
                    "hostPath": "/sys/kernel/security",
                    "containerPath": "/sys/kernel/security",
                },
                {
                    "hostPath": str(self.parser),
                    "containerPath": "/sbin/apparmor_parser",
                    "readOnly": True,
                },
            ]
            self.record["apparmor_parser"] = self.run(self.parser, "--version")
            self.record["apparmor_parser_sha256"] = hashlib.sha256(
                self.parser.read_bytes()
            ).hexdigest()
            self.record["apparmor_profile"] = self.name
            self.record["apparmor_abi_sha256"] = hashlib.sha256(
                (self.parser.parent / "apparmor/abi/4.0").read_bytes()
            ).hexdigest()
        for filename in ("chrome-seccomp.json", "chrome-apparmor"):
            self.record[filename + "_sha256"] = hashlib.sha256(
                (POLICY / filename).read_bytes()
            ).hexdigest()
        return {
            "kind": "Cluster",
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "nodes": [{"role": "control-plane", "extraMounts": mounts}],
        }

    def exec(self, *args, **kwargs):
        return self.run("docker", "exec", self.node, *args, **kwargs)

    def install(self):
        if self.apparmor:
            profiles = self.exec("cat", "/sys/kernel/security/apparmor/profiles")
            if any(line.split(" ")[0] == self.name for line in profiles.splitlines()):
                raise RuntimeError("refusing to replace an existing AppArmor profile")
            # The privileged kind node is infrastructure; application Pods stay non-root.
            # Loading is explicit and limited to this unique profile, never --replace.
            self.load_attempted = True
            self.run(
                "docker",
                "exec",
                "-i",
                self.node,
                "sh",
                "-ec",
                "cat > /sys/kernel/security/apparmor/.load",
                input=(self.root / "chrome-apparmor.bin").read_bytes(),
                binary=True,
            )
            assert self.name + " (enforce)" in self.exec(
                "cat", "/sys/kernel/security/apparmor/profiles"
            )
        self.run(
            "docker",
            "exec",
            "-i",
            self.node,
            "sh",
            "-ec",
            "mkdir -p /var/lib/kubelet/seccomp/webcappi; cat > /var/lib/kubelet/seccomp/webcappi/chrome.json",
            input=(POLICY / "chrome-seccomp.json").read_text(),
        )
        self.record["node_containerd"] = self.exec("containerd", "--version")
        self.record["node_runc"] = self.exec("runc", "--version")
        self.record["docker"] = json.loads(
            self.run("docker", "version", "--format", "{{json .Server}}")
        )

    def incompatible_apparmor(self):
        name = self.name + "-deny-userns"
        profiles = self.exec("cat", "/sys/kernel/security/apparmor/profiles")
        if any(line.split(" ")[0] == name for line in profiles.splitlines()):
            raise RuntimeError("refusing to replace an existing AppArmor profile")
        source = (
            (POLICY / "chrome-apparmor")
            .read_text()
            .replace("WEBCAPPI_PROFILE", name)
            .replace("  userns create,", "  deny userns create,")
        )
        path = self.root / "deny-userns"
        path.write_text(source)
        self.run(
            self.parser,
            "-I",
            self.parser.parent / "apparmor",
            "-Q",
            "-K",
            "-o",
            path.with_suffix(".bin"),
            path,
        )
        self.additional_profiles.append(name)
        self.run(
            "docker",
            "exec",
            "-i",
            self.node,
            "sh",
            "-ec",
            "cat > /sys/kernel/security/apparmor/.load",
            input=path.with_suffix(".bin").read_bytes(),
            binary=True,
        )
        self.record["incompatible_apparmor_profile"] = name
        self.record["incompatible_apparmor_sha256"] = hashlib.sha256(
            source.encode()
        ).hexdigest()
        return {"type": "Localhost", "localhostProfile": name}

    def cleanup(self):
        if not self.load_attempted:
            return
        # Never unconfine live workloads by removing their profile. Stop kubelet and
        # every sandbox before unloading; all resources belong to this test node.
        self.exec("systemctl", "stop", "kubelet")
        ids = self.exec("crictl", "pods", "-q").splitlines()
        for sandbox in ids:
            self.exec("crictl", "stopp", sandbox)
        for name in [self.name, *self.additional_profiles]:
            profiles = self.exec("cat", "/sys/kernel/security/apparmor/profiles")
            if name + " (enforce)" in profiles:
                self.exec(
                    "sh",
                    "-ec",
                    'printf "%s\\0" "$1" > /sys/kernel/security/apparmor/.remove',
                    "sh",
                    name,
                )
            assert name + " (enforce)" not in self.exec(
                "cat", "/sys/kernel/security/apparmor/profiles"
            )
        self.record["apparmor_removed"] = True
