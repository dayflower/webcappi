# webcappi Helm chart

Deploy the Chromium capture API using an application chart, with its
browser sandbox enabled by default.
The chart provides a ClusterIP Service and optional Ingress or Gateway API
HTTPRoute resources. The release workflow publishes versioned OCI charts after
distribution acceptance. The first publication is planned for application
version **0.1.2**; until that release completes, use the local chart.
The existing [raw manifests](../../deploy/kubernetes.yaml) remain supported.

## Public OCI installation and upgrades

Once the selected version's GitHub Release includes `chart-distribution.json`,
the chart is available without registry login at
`oci://ghcr.io/dayflower/charts/webcappi`. Chart `version` and `appVersion`
both equal the application version without `v`. There is no independent chart
version series or floating chart alias. The distribution values pin the verified
multi-platform application image digest; the local source values are not the
distribution defaults. Confirm the version exists before using this example:

```sh
helm pull oci://ghcr.io/dayflower/charts/webcappi --version 0.1.2
helm show values ./webcappi-0.1.2.tgz
```

Prepare compatible nodes using the [sandbox guide](../../deploy/sandbox/README.md),
create the namespace and `capture-auth` Secret as below, and keep operator
settings in a values file. For the qualified node policies, for example:

```yaml
auth:
  existingSecret: capture-auth
browser:
  seccompProfile:
    type: Localhost
    localhostProfile: webcappi/chrome.json
  appArmorProfile:
    type: Localhost
    localhostProfile: webcappi-chrome
```

These are references to profiles already installed by the operator; adjust them
to the qualified profile names on your nodes. They do not install node policy.

```sh
helm install capture ./webcappi-0.1.2.tgz --namespace captures -f operator-values.yaml
helm upgrade capture oci://ghcr.io/dayflower/charts/webcappi --version 0.1.2 \
  --namespace captures --reset-values -f operator-values.yaml
helm history capture --namespace captures
helm rollback capture 1 --namespace captures --wait
helm uninstall capture --namespace captures --wait
```

Choose an explicitly released version for each upgrade, review its values and
retain the operator values used by each revision. `--reset-values` keeps a prior
release's image digest from silently overriding the new distribution default.
Review any intentional image overrides too. Update Redis `generation` whenever
the image, fonts, extensions or prepared bundle changes, including changes made
through chart upgrades. Rollback restores the earlier values and generation;
confirm that the corresponding external Secret, node profiles and bundle remain
available. Uninstall preserves operator-managed Secrets, Redis, Gateways and
node profiles. Secret changes still require an explicit Pod restart.

See [distribution and recovery](../../notes/DEVELOP.md#helm-distribution-and-recovery)
for archive/public acceptance, initial package visibility and interrupted releases.

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
| `ingress` | Optional networking.k8s.io/v1 Ingress; existing class, annotations, hosts/paths and TLS Secrets. Disabled by default. |
| `httpRoute` | Optional gateway.networking.k8s.io/v1 HTTPRoute; existing Gateway parents, hostnames, path matches and explicit timeouts. Disabled by default. |
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

## Ingress and Gateway API

Both resources are disabled by default and can be enabled independently or
simultaneously. They forward to this release's Service and `service.port`,
including values other than 8080; the API container still listens on 8080.
The chart installs no controller, IngressClass, Gateway API CRD, GatewayClass,
Gateway, certificate or proxy policy. Provision these separately. Ingress uses
`networking.k8s.io/v1`; HTTPRoute requires `gateway.networking.k8s.io/v1/HTTPRoute`
in discovery. Install the CRDs before installing/upgrading the application.
Offline HTTPRoute rendering must explicitly advertise the capability:

```sh
helm template capture ./charts/webcappi -n captures -f production-values.yaml \
  --api-versions gateway.networking.k8s.io/v1/HTTPRoute
```

Helm checks schema, required hosts/paths and parent references during every
render, including when the other network resource is disabled. It does not
read TLS Secret contents, Gateway objects or status with `lookup`. Check the
actual controller status and perform a capture after install and every update.
`helm --wait` alone does not establish that a public route works.

### Ingress example (Traefik)

Create `capture-tls` in `captures`, containing a certificate valid for your
public hostname. Supply the existing class and controller-specific annotations:

```yaml
auth:
  existingSecret: capture-auth
capture:
  timeoutSeconds: 30
  maxPOSTBytes: 2097152
service:
  port: 9090
  annotations:
    traefik.ingress.kubernetes.io/service.serverstransport: capture@file
ingress:
  enabled: true
  className: traefik
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls: "true"
    traefik.ingress.kubernetes.io/router.middlewares: capture-body@file
  hosts:
    - host: capture.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: capture-tls
      hosts:
        - capture.example.com
```

For the Traefik **3.7.16** configuration exercised by CI, provision this dynamic
file through the controller's file provider. Names before `@file` refer to
these operator-managed entries:

```yaml
http:
  serversTransports:
    capture:
      forwardingTimeouts:
        responseHeaderTimeout: 40s
  middlewares:
    capture-body:
      buffering:
        maxRequestBodyBytes: 2097152
        memRequestBodyBytes: 2097152
```

Configure the selected entrypoint's
`transport.respondingTimeouts.readTimeout=45s` and `writeTimeout=45s` in
Traefik's static configuration. These are controller settings, not portable
Ingress annotations. The annotations above select the transport/body policy
and TLS entrypoint; they do not install those policies. Set the same deadlines
on a plain HTTP entrypoint when using it for tests. Do not attach Retry
middleware or a buffering `retryExpression`. See the pinned test fixture in
[scripts/helm_network.py](../../scripts/helm_network.py) and
[Traefik's Ingress reference](https://doc.traefik.io/traefik/v3.7/reference/routing-configuration/kubernetes/ingress/).

### HTTPRoute example (Envoy Gateway)

Provision the Gateway and its TLS Secret in `gateway-system`. This example
allows only routes from the `captures` namespace and terminates TLS on the
listener. No ReferenceGrant is needed for this attachment: the listener's
`allowedRoutes` controls it. The Service backend remains in the HTTPRoute's
release namespace. A TLS Secret in another namespace requires separate
controller-supported certificate references and a ReferenceGrant; prefer a
Secret in the Gateway namespace as shown.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: gateway-system
spec:
  gatewayClassName: envoy
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      hostname: capture.example.com
      tls:
        mode: Terminate
        certificateRefs:
          - name: capture-tls
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              kubernetes.io/metadata.name: captures
```

Supply these application values along with the bearer Secret and qualified
browser profile references:

```yaml
httpRoute:
  enabled: true
  parentRefs:
    - name: public
      namespace: gateway-system
      sectionName: https
  hostnames:
    - capture.example.com
  paths:
    - type: PathPrefix
      value: /
  timeouts:
    request: 45s
    backendRequest: 40s
```

Parent `namespace` and `sectionName` are optional; omitting namespace means the
release namespace, while omitting sectionName permits all matching listeners.
Paths support `PathPrefix` or `Exact`. Empty hostnames use the parent listener's
hostname policy. `timeouts` is omitted unless specified. Durations use a single
integer (up to five digits) followed by `ms`, `s`, `m` or `h`; `0s` disables a
timeout. A nonzero request deadline must cover the backend deadline; a disabled
backend timeout cannot fit inside a nonzero request deadline. Verify your
controller implements both settings. CI uses Envoy Gateway **1.9.3**, its
Gateway API **1.6.1** CRDs and Envoy **1.39.5**, and proves backend timeout
behavior by executing a deliberately delayed request. Traefik **3.7.16** is
used only for Ingress because that release does not implement HTTPRoute
`timeouts`. See [Envoy Gateway's timeout guide](https://gateway.envoyproxy.io/docs/tasks/traffic/http-timeouts/).

Check `status.parents` for **Accepted=True** and **ResolvedRefs=True** for the
selected controller, parent/listener and current `metadata.generation`:

```sh
kubectl -n captures get httproute capture-webcappi -o yaml
```

### Deadlines, bodies, authentication and paths

Set proxy deadlines above `capture.timeoutSeconds`, with room for upload,
queueing and response transfer. A larger application deadline requires
corresponding controller limits. Size request-body policies for
`capture.maxPOSTBytes` and any surrounding proxy limits. CI verifies a delayed
HTML POST larger than 1 MiB with a 2 MiB limit and rejection above that limit.
HTTPRoute has no portable request-body-size field: the chart sets none; the
application enforces `maxPOSTBytes`, and optional Gateway policies belong to
the operator. Controllers must not automatically retry expensive captures,
including on timeout, disconnect or backend errors. Keep API authentication
enabled and preserve the Authorization header through every proxy.

Publish `/` by default. Matching `/capture` or `/healthz` selects existing API
paths; matching `/webcappi` does not add URL-prefix support or remove that
prefix. A prefixed deployment requires an operator-managed rewrite and separate
end-to-end verification; this chart does not generate rewrites. The qualified
examples use root paths. TLS is terminated by the controller; the backend
Service speaks HTTP. Use certificates trusted by clients in production.

Private URLs and Kubernetes Service DNS remain valid **capture targets**:
pass them as the API's `url` parameter. Public routing applies to the API
connection and does not change browser target access. `/healthz` and `/readyz`
remain unauthenticated. Public TLS/authentication is verified by the smoke
suite, including rejection of wrong SNI certificate names.

Routing-only changes update network objects without changing the application's
configuration checksum. Use `helm history` and `helm rollback` as below to
restore previous routes, then verify status and perform a capture again.
Uninstall removes the release's Ingress/HTTPRoute and leaves operator-managed
controllers, Gateways and TLS Secrets intact.

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
