# webcappi Helm chart

Deploy the Chromium capture API using a local application chart, with its
browser sandbox enabled by default.
This phase provides a ClusterIP Service. Ingress, Gateway API and OCI chart
publication are planned separately; no published chart is required here.
The existing [raw manifests](../../deploy/kubernetes.yaml) remain supported.

## Requirements and local installation

- Linux amd64 or arm64 nodes, sufficient ephemeral storage, and a node/runtime
  policy that permits Chromium's user-namespace sandbox when
  `browser.noSandbox=false` (the default). Standard runtimes may require
  existing Localhost seccomp/AppArmor profiles; follow the
  [node preparation guide](../../deploy/sandbox/README.md). The chart grants
  no additional capabilities and does not install node policy.
- Helm 4; validation tooling pins Helm 4.3.0 and Kubernetes 1.36.4. See the
  [development notes](../../notes/DEVELOP.md) for executed validation and limits.
- An application image containing the pinned full Chrome for Testing. No
  browser, profile or extension downloads take place at application startup.
- An existing bearer-token Secret in the release namespace, unless API
  authentication is explicitly disabled for a trusted environment.

Create a token file containing one valid bearer token (1–4096 bytes, with an
optional trailing LF/CRLF), then create the Secret without putting its contents
in values or rendered manifests:

```sh
kubectl create namespace captures
kubectl -n captures create secret generic capture-auth --from-file=token=./token
helm install capture ./charts/webcappi --namespace captures \
  --set auth.existingSecret=capture-auth
kubectl -n captures port-forward service/capture-webcappi 8080:8080
```

Use `Authorization: Bearer <token>` for captures. `/healthz` and `/readyz` are
unauthenticated so Kubernetes can probe the API. The default image is
`ghcr.io/dayflower/webcappi:0.1.1`; override it with your qualified image or
digest. A local kind cluster also needs that image loaded into its nodes.

The default values deliberately require `auth.existingSecret`; bare
`helm install`, `helm template` and `helm lint` fail until it is supplied or
`auth.enabled=false` is set. Helm never reads the Secret contents via `lookup`.

## Values and configuration

See [values.yaml](values.yaml) for defaults and [values.schema.json](values.schema.json)
for accepted types, ranges and conditional requirements. Unknown top-level
settings and arbitrary JSON/config overrides are rejected. Application paths,
port 8080, security contexts and rollout strategies are fixed.

| Values | Behavior |
| --- | --- |
| `replicaCount` | Integer at least 1; default 1. Values above 1 require Redis. |
| `image.repository`, `tag`, `digest`, `pullPolicy` | A digest takes precedence over a fixed tag. `latest` is rejected. |
| `imagePullSecrets` | Existing Secret references, also used by the bundle initContainer. |
| `browser.noSandbox` | Boolean; default `false` enables Chromium's sandbox. Explicit `true` disables it through the application's `no_sandbox` setting. |
| `auth.enabled`, `existingSecret`, `tokenKey` | Enabled by default; select the Secret and token key. The projected filename is always `token`. |
| `cache.backend` | `memory` (default) or `redis`; only the selected backend is serialized. |
| `cache.redis` | External primary address, namespace, explicit generation, optional ACL username, password Secret and TLS/CA reference. |
| `profiles.descriptors`, `profiles.bundle` | Optional application descriptors and immutable bundle image; configured together. |
| `service.port`, `annotations` | ClusterIP Service; its configurable port targets the fixed named HTTP container port. |
| `resources` | CPU, memory and ephemeral storage requests/limits for API and initContainer. |
| `volumes` | Positive `workSizeLimit`, `tmpSizeLimit`, `shmSizeLimit` emptyDir capacities. `/dev/shm` uses memory. |
| `podAnnotations`, `nodeSelector`, `tolerations`, `affinity` | Pod annotations and scheduling. `checksum/config` is reserved. |

The explicit capture mappings are:

| `capture` value | Application JSON field | Default |
| --- | --- | --- |
| `concurrency` | `concurrency` | 1 |
| `queueSize` | `queue_size` | 8 |
| `maxRequests` | `max_requests` | 64 |
| `timeoutSeconds` | `capture_timeout_seconds` | 30 |
| `metadataMaxBytes` | `metadata_max_bytes` | 1048576 |
| `metadataMaxEntries` | `metadata_max_entries` | 128 |
| `maxAgeSeconds` | `max_age_seconds` | 60 |
| `maxWidth`, `maxHeight` | `max_width`, `max_height` | 4096, 4096 |
| `maxPixels` | `max_pixels` | 8388608 |
| `resizeAlgorithm` | `resize_algorithm` | `bilinear` |
| `maxProfileBytes` | `max_profile_bytes` | 536870912 |
| `maxPOSTBytes` | `max_post_bytes` | 1048576 |
| `maxPNGBytes` | `max_png_bytes` | 41943040 |

Zero queue size, metadata capacities and freshness retain their application
meaning; Helm does not substitute defaults for explicit zero values. Metadata
limits do not allocate a PNG cache. The rendered JSON is serialized rather than
interpolated and checked against the application's strict Go loader in CI.

## Browser sandbox selection

The default `browser.noSandbox=false` keeps Chromium's own sandbox enabled.
An operator can explicitly opt out using a values file:

```yaml
browser:
  noSandbox: true
```

```sh
helm upgrade --install capture ./charts/webcappi -n captures \
  --set auth.existingSecret=capture-auth --set browser.noSandbox=true
```

This boolean maps directly to `config.json`'s `no_sandbox` field. Set it back
to `false` to enable the sandbox again. A config checksum triggers a Deployment
update when it changes. There is no automatic fallback to an unsandboxed browser
when startup fails. Disabling Chromium's sandbox removes its internal process
isolation; the chart's non-root identity, read-only mounts, dropped capabilities,
the selected seccomp/AppArmor profiles and resource limits remain in effect.

Sandbox-enabled operation still requires a compatible node/runtime policy.
The API container accepts two dedicated existing-profile references:

| Value | Default | Accepted values |
| --- | --- | --- |
| `browser.seccompProfile.type` | `RuntimeDefault` | `RuntimeDefault`, `Localhost` |
| `browser.seccompProfile.localhostProfile` | `""` | Clean relative path under the kubelet seccomp directory, required only for `Localhost` |
| `browser.appArmorProfile.type` | `""` | Empty (runtime default, including hosts without AppArmor), `RuntimeDefault`, `Localhost` |
| `browser.appArmorProfile.localhostProfile` | `""` | Existing enforced profile name, required only for `Localhost` |

Provision the [node policies](../../deploy/sandbox/README.md) before installing:

```yaml
browser:
  noSandbox: false
  seccompProfile:
    type: Localhost
    localhostProfile: webcappi/chrome.json
  appArmorProfile:
    type: Localhost
    localhostProfile: webcappi-chrome
```

References apply only to `api`; bundle-copy init containers inherit the Pod's
RuntimeDefault seccomp and runtime AppArmor default. On hosts without AppArmor,
leave its type empty. Unconfined, incomplete Localhost references, traversal
paths and legacy security-profile annotations are rejected. All UID/GID,
no-privilege-escalation, capability and filesystem constraints remain intact.
Helm does not inspect or load host profiles. Missing policy fails container
creation; incompatible policy fails browser startup/readiness, with no retry
using a disabled sandbox. Inspect Pod events and API logs, then correct node
preparation before retrying.

Validate and record the selected mode explicitly; disabled-mode success does
not qualify sandbox-enabled operation. The disposable kind helper prepares
its own unique profiles and removes them after stopping its workloads. See
[setup, cleanup and policy provenance](../../deploy/sandbox/README.md) and
[executed validation](../../notes/DEVELOP.md#phase-1a-sandbox-runtime).

The local lifecycle helper defaults to the enabled mode. To test an explicit
opt-out, run `make helm-smoke IMAGE=<local-image> HELM_SMOKE_SANDBOX=disabled`.
Results record the selected mode in separate `helm-enabled/` or
`helm-disabled/` artifact directories. Native amd64/arm64 CI jobs run both
modes and real setup-failure/interruption cleanup. See
[development validation](../../notes/DEVELOP.md#phase-1a-sandbox-runtime)
for procedures, qualified native runner combinations and remaining limits.

## Memory and external Redis

Memory uses `Recreate` so a normal Deployment update does not overlap old and
new API Pods. There is no HPA or rollout-strategy override. Helm's replica
constraint cannot prevent direct `kubectl scale` or another controller from
changing a Deployment; keep a memory Deployment at exactly one replica.

Redis is provisioned separately. The chart does not install Redis, manage its
credentials or configure its availability. Example `redis-values.yaml`:

```yaml
replicaCount: 3
auth:
  existingSecret: capture-auth
cache:
  backend: redis
  redis:
    address: redis-primary.cache.svc.cluster.local:6379
    namespace: captures-production
    generation: capture-v1
    username: webcappi
    passwordSecret:
      name: redis-auth
      key: password
    tls: true
    ca:
      kind: ConfigMap
      name: redis-ca
      key: ca.crt
    timeoutMS: 500
    retentionSeconds: 86400
```

```sh
kubectl -n captures create secret generic redis-auth --from-file=password=./redis-password
kubectl -n captures create configmap redis-ca --from-file=ca.crt=./redis-ca.pem
helm upgrade --install capture ./charts/webcappi -n captures -f redis-values.yaml
```

Use `[IPv6]:port` for IPv6 endpoints. Namespace and generation each contain
1–64 letters, digits, underscores or hyphens. `passwordSecret` and `ca` require
both a name and key when used; CA kind may be `ConfigMap` or `Secret` and a
custom CA requires `tls=true`. With TLS and no custom CA, the image's system
trust store is used. `timeoutMS=0` means 500 ms; `retentionSeconds=0` means
86400 seconds. Effective retention must cover `capture.maxAgeSeconds` and
cannot exceed 604800 seconds. Redis uses RollingUpdate with maxSurge 1 and
maxUnavailable 0. Do not point replicas at independently writable Redis stores.

Keep generation identical across all Pods sharing metadata. Update it whenever
the image, fonts, extension code or profiles change, including values-only
image/bundle changes without a chart version change. Chart version alone is
not a cache generation. Preserve content ETags, TTL/Age and source validation
after metadata expiry as provided by the application.

## Profiles

With empty descriptors and no bundle, the application generates its default
profile in Pod scratch space. No artifact image or PVC is required. Scratch
and automatic profiles disappear when the Pod is removed.

For prepared profiles, follow the application's [profile staging procedure](../../README.md)
and [bundle image example](../../deploy/bundle.Dockerfile). Package the complete
User Data Directory, bundle descriptors and separate unpacked extensions under
`/bundle/`, with UID/GID 10001 and permissions readable by that identity. The
bundle image must provide `sh` and `cp`; use an immutable tag or preferably a
digest. A non-root initContainer runs `cp -R /bundle/. /out/`, including hidden
files and modes, into a capacity-limited per-Pod emptyDir. The API mounts that
tree read-only at `/bundles`, separately from `/work/captures`.

```yaml
profiles:
  bundle:
    repository: registry.example.com/capture-profiles
    tag: capture-v2
    # digest: sha256:<64 lowercase hex characters>
    sizeLimit: 1Gi
  descriptors:
    default:
      user_data_dir: /bundles/default/user-data
      profile_directory: Default
      extensions:
        - /bundles/default/extensions/fixture
      ready_selectors:
        - "#page-ready"
```

Descriptors use the application's snake_case field names and are not discovered
from bundle files at render time. Paths must be clean paths below `/bundles/`;
extensions must be separate from the profile directory. Preserve the installed
extension IDs and runtime paths used during profile preparation. The chart
does not rewrite Preferences, reinstall extensions or launch the browser against
the template. The application clones the full directory for each capture and
opens an explicitly created page. Readiness checks verify the mounted content;
Helm cannot inspect files inside an arbitrary bundle image.

## Updates, rollback and removal

Store the complete desired values in a file. A rendered config checksum rolls
Pods when application settings change. External token/password/CA content is
read at startup: after changing it, explicitly restart the Deployment.

```sh
helm upgrade capture ./charts/webcappi -n captures -f production-values.yaml
kubectl -n captures rollout restart deployment/capture-webcappi
kubectl -n captures rollout status deployment/capture-webcappi
```

For Redis scaling, change `replicaCount` in that file and run `helm upgrade`.
For image or bundle updates, change their immutable references and Redis
generation together. Qualify the new image/bundle on both native architectures.

```sh
helm history capture -n captures
helm rollback capture 1 -n captures --wait
helm uninstall capture -n captures --wait
```

Rollback restores the earlier config, generation and image/bundle references;
retain those immutable images for recovery. Secrets, CA ConfigMaps and the
external Redis remain after uninstall. Per-Pod scratch and copied bundles are
removed. Validate sandbox/node compatibility in the target environment before
production rollout.
