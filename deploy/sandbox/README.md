# Chromium sandbox node policy

These files support Chrome for Testing **156.0.8078.47**, Linux amd64/arm64,
non-root UID/GID 10001, no privilege escalation, all capabilities dropped and a
read-only root filesystem. They grant no application container capabilities.
They are node policies; Helm only references profiles provisioned by an operator.
A policy change requires fresh runtime qualification on each native platform.
See [validation results](../../notes/DEVELOP.md#phase-1a-sandbox-runtime).

## Policy design

The policies are independently authored from public Linux/OCI/AppArmor
interfaces, the project's application requirements and runtime observations.
They are covered by the project's [MIT license](../../LICENSE).

The OCI policy rejects unlisted syscalls with EPERM. Only x86_64/aarch64 ABIs are
enabled. Namespace creation uses a full 64-bit clone mask and the observed
USER, USER|PID|NET and PID child combinations; unshare permits USER only.
clone3 deliberately returns ENOSYS so libc uses filtered clone. Mount, setns,
extended BPF, ptrace tracing and unrelated host administration stay rejected.
prctl, seccomp-install flags, ioctl selectors and socket family/type choices are
finite. The allowed operations cover the complete API container, including tini,
Go scheduling and cleanup, profile copying, Chrome rendering and IPC, HTTP/TLS,
DNS and Redis. The policy includes source-derived alternatives for error paths
and runtime compatibility; it is not a proven minimal syscall set.

AppArmor uses ABI 4.0, enforced inherited execution for named immutable image
executables, read-only input/resource roots, and scratch writes without scratch
execve permission. It grants no runtime container capabilities. Its capability
rules mediate namespace-local mapping, chroot and identity dropping after
unprivileged user-namespace creation. In particular, `capability sys_admin`
cannot express a namespace qualifier itself: safety depends on the required empty
initial permitted/effective/bounding capability sets, NNP, kernel namespace
authority and the rejecting seccomp layer. Do not add CAP_SYS_ADMIN to the Pod.
Original-namespace CAP_SYS_ADMIN acquisition and UID0 changes are negatively tested.
No host abstraction or complain/unconfined transition is used. Anonymous
executable memory/memfd remains supported for Chrome/Javascript.

| Permission | Purpose and restriction |
| --- | --- |
| Namespace creation and chroot | Chrome's user/PID/network sandbox; no mount or namespace-joining permission. |
| Filter installation | Chrome adds its own seccomp filters beneath the container filter; listener creation and unknown flags are rejected. |
| Image resources and input reads | Libraries, fonts, certificates, configuration and complete profile/extension bundles at the supported paths. |
| Scratch writes | Per-job profile clones, caches, HTML, PNG/archive output and Unix sockets; execution from scratch is rejected. |
| Selected proc access | Process discovery and cancellation cleanup, memory accounting, namespace identity maps and sandbox diagnostics; no broad proc writes or process-memory access. |
| Network families and types | HTTP/TLS/Redis, DNS, loopback HTML, Unix IPC and routing discovery; raw IP/packet sockets are rejected. |
| Same-profile process access | Signals preserve shutdown and child cleanup; ptrace read mediation permits required proc inspection without tracing authority. |

Interface references: [OCI seccomp format](https://github.com/opencontainers/runtime-spec/blob/v1.3.0/config-linux.md#seccomp),
[Linux seccomp filtering](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
and [AppArmor policy syntax](https://manpages.ubuntu.com/manpages/noble/man5/apparmor.d.5.html).

### Supported filesystem layout

- Writable job clones, archives and CLI outputs: `/work`, `/tmp`, `/dev/shm`.
- Read-only configuration: `/etc/webcappi`; credentials: `/run/secrets` or
  `/var/run/secrets`; Redis CA: `/run/redis-ca`.
- Read-only prepared profiles: `/bundles`; extension installations: `/extensions`
  or a separate immutable bundle path outside the User Data Directory.
- Chrome and libraries/fonts: the pinned image paths. Supported operator
  executables are explicitly named in the profile; custom tools/paths need an
  explicit policy change and validation.

The Go application accepts other paths, but this supplied fixed-layout profile
does not promise arbitrary locations. Keep template and extension mounts
read-only. Replace every `WEBCAPPI_PROFILE` token consistently, including peer
labels, with a dedicated name before compiling.

## Operator-controlled production setup

Prepare each eligible node before installing the chart. Verify unprivileged
user namespaces are available, `user.max_user_namespaces` is nonzero and the
node runtime supports Localhost seccomp. On AppArmor hosts, verify AppArmor and
securityfs are available to the runtime and load an enforced profile with
`userns` support. The host administrator owns this preparation; the chart does
not install policy, change runtime defaults or relax sysctls.

For example, on an AppArmor 4 host, with a dedicated name that does not already
exist:

```sh
sed 's/WEBCAPPI_PROFILE/webcappi-chrome/g' deploy/sandbox/chrome-apparmor > /tmp/webcappi-chrome
# Inspect the source and ensure webcappi-chrome is not already loaded.
sudo apparmor_parser --add --skip-cache /tmp/webcappi-chrome
sudo install -D -m 0644 deploy/sandbox/chrome-seccomp.json /var/lib/kubelet/seccomp/webcappi/chrome.json
```

Reference these names with `browser.seccompProfile` and
`browser.appArmorProfile` as documented in the [chart guide](../../charts/webcappi/README.md#browser-sandbox-selection).
Provision all schedulable nodes, or use a node selector for prepared nodes.
Persist/reload the policy through your node-management system before reboots.
For reversal, first remove or stop **all** workloads using this dedicated
profile, then `sudo apparmor_parser --remove /tmp/webcappi-chrome` and remove
only the seccomp file you installed. Never replace or remove a pre-existing
operator profile. Do not remove a live workload's profile: this would change
its confinement.

Hosts without AppArmor need only seccomp and available user namespaces; leave
`browser.appArmorProfile.type` empty there. AppArmor hosts that restrict
unprivileged user namespaces need the explicit `userns` profile. RuntimeDefault
AppArmor and a path-based host Chrome exception do not establish permission
inside a container's confinement. SELinux and other LSM policies require their
own qualification; this recipe has no SELinux policy.

## Disposable kind preparation

`make chart-tools` installs checksummed Helm/kind/kubectl tools and extracts
only the AppArmor parser from Ubuntu package
**4.0.1really4.0.1-0ubuntu0.24.04.9** into `bin/chart-tools/`. It does not install
a host package or service. Both package SHA-256 values are pinned in
`scripts/chart-tools.py`. Native Ubuntu 24.04 supplies the loader/glibc needed
by this parser; the pinned Debian kind node supplies its runtime dependencies.

```sh
make chart-tools
GOTOOLCHAIN=go1.26.10 make helm-smoke IMAGE=webcappi:helm-check HELM_SMOKE_SANDBOX=enabled
GOTOOLCHAIN=go1.26.10 make helm-smoke IMAGE=webcappi:helm-check HELM_SMOKE_SANDBOX=disabled
```

A native C compiler with static libc and the pinned Go toolchain build the
immutable diagnostic executables in a disposable derivative image. The source
application image remains separately identified in `result.json`. OpenSSL creates
one-day certificates for the disposable Redis TLS fixture.

Docker access, securityfs and a kernel permitting user namespaces are explicit
host prerequisites. On AppArmor hosts the enabled helper mounts securityfs and
the pinned parser into its uniquely named kind node **before** containerd
starts. Without the parser containerd reports AppArmor as unsupported, even
when the profile is already loaded into the shared host kernel. The helper
compiles its unique profile without root on the host, loads it with `.load`
through that node's securityfs, and copies seccomp into the node kubelet's
profile directory. It never replaces an existing profile. Stock kind nodes
are privileged test infrastructure; application Pods retain their normal
non-root security context. No application security exception is used.

On success, failure or SIGINT/SIGTERM, diagnostics are collected first. Kubelet
and node Pod sandboxes are stopped before the owned AppArmor profile is
removed; then kind deletes its node and the helper removes temporary files and
image aliases. The shared default Docker `kind` network and operator
configuration are preserved. A cleanup error fails the run and is recorded;
if Docker becomes unavailable, recover it and stop the owned node's workloads
before removing the recorded unique profile manually. SIGKILL or host power
loss cannot execute cleanup and requires the same recovery procedure.

Artifacts are separate under `helm-enabled/` and `helm-disabled/`.
`chromium-sandbox.json` records `chrome://sandbox`, actual browser arguments and
the pinned browser version using the application's browser launcher. It also
records per-role proc status, namespace links/read errors and renderer filter
counts greater than the outer/browser count; Seccomp:2 alone is insufficient.
Diagnostics are installed at immutable `/usr/local/bin` paths, never executed
from writable scratch.
`result.json` records kernel, Docker/containerd/runc, policy hashes, the unique
AppArmor name and removal status. Enabled tests also reject missing seccomp,
incompatible RuntimeDefault seccomp, missing AppArmor profiles and an enforced
AppArmor profile denying user namespaces, with
status/log evidence and no unsandboxed retry. Prepared bundles use the real
sandboxed browser directly.

`confined-browser-tests.json` records real CDP font selection, an already
installed extension with stable paths/installation count and welcome-tab
handling, file/loopback HTML, selectors and timeout/failure cleanup under the
application policies. External headed fixture installation has a separate
explicit sandbox-disabled record, `extension-fixture-preparation.json`; it is
not counted as enabled acceptance. `docker-policy-denials.json` records an
otherwise matching engine-default AppArmor baseline proving a canary is writable
and the probe binary executes on explicitly exec-enabled diagnostic scratch
mounts, followed by custom-policy rejection. Production mounts are not relaxed.
Other errno observations are not automatically attributed to a particular
security layer.


Run the real failure/cleanup qualification independently with:

```sh
python3 scripts/sandbox/sandbox-cleanup-check.py --artifacts artifacts/helm-cleanup
```

It creates two additional owned kind clusters, injects failure immediately
after policy loading and sends SIGTERM after successful setup. Each case checks
that the unique profiles and node were removed. Native CI runs this in addition
to both complete lifecycle modes.
