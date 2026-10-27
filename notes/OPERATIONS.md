# Operations guide

The [README](../README.md) covers starting the published image and using the API.
This guide covers configuration, container requirements and shared metadata.

## Access and resource limits

Keep the API accessible only to trusted callers. Captured pages execute
JavaScript and can reach the browser container's network, including private
addresses and Kubernetes Service DNS names. Prepared profiles can supply saved
credentials. A shared Bearer token permits access to all configured profiles;
it does not restrict capture destinations or provide per-caller authorization.

Use ingress/egress policies and container resource limits appropriate to the
deployment. Browser activity can consume substantial CPU, memory and network
resources even with bounded capture concurrency, queues and deadlines. Use an
authentication gateway when per-caller access control is needed.

## Configuration

JSON configuration rejects unknown top-level fields. All omitted limits take
these defaults. Durations are whole seconds. Zero `max_age_seconds` requires
validation on every conditional request; zero metadata capacity disables metadata
retention. Configuration changes require a process restart.

| Field | Default | Meaning |
| --- | --- | --- |
| `listen` | `:8080` | HTTP bind address |
| `auth.bearer_token_file` | unset | Absolute shared Bearer token file path; omit `auth` to disable API authentication |
| `browser_path` | `/opt/chrome/chrome` | Absolute, preinstalled browser path |
| `sandbox` | `true` | Enable the browser sandbox; omitted, null or empty string also enables it |
| `work_dir` | `/work` | Writable scratch and automatic template; separate from supplied templates/extensions |
| `concurrency` | 1 | Active browser captures (1–32) |
| `queue_size` | 8 | Additional pending captures or source validations (0–1024) |
| `max_requests` | 64 | Concurrent HTTP capture handlers, including metadata lookups |
| `capture_timeout_seconds` | 30 | Queue + profile copy + browser start + readiness + delay + PNG (1–300) |
| `cache` | memory | Metadata backend; see [Redis metadata cache](#redis-metadata-cache) |
| `max_age_seconds` | 60 | Maximum downstream/metadata freshness lifetime (0–86400) |
| `metadata_max_bytes` | 1048576 | Validation metadata byte budget (0–67108864) |
| `metadata_max_entries` | 128 | Validation metadata LRU entry limit (0–10000) |
| `max_width`, `max_height` | 4096 each | Maximum viewport and output dimensions, checked independently |
| `max_pixels` | 8388608 | Maximum area of each viewport and output canvas |
| `resize_algorithm` | `bilinear` | Resize filter: `bilinear`, `catmull_rom`, `approx_bilinear` or `nearest_neighbor`; center cropping is preserved |
| `max_profile_bytes` | 536870912 | Maximum template bytes copied during initialization or per capture |
| `max_post_bytes` | 1048576 | Maximum HTML POST body bytes (1–134217728) |
| `max_png_bytes` | 41943040 | Maximum generated PNG size |
| `profiles` | omitted | Named immutable descriptors; absent `default` is generated (128 total including `default`) |

See [config.example.json](../config.example.json) for a complete example and the
[profile guide](PROFILES.md) for named profile descriptors. For older
configurations, replace `cache_ttl_seconds` with `max_age_seconds` and remove
`cache_max_bytes` / `cache_max_entries`; unknown fields are rejected.

## Authentication

Set `auth.bearer_token_file` to an absolute path and mount the token read-only;
see the [README example](../README.md#authentication). The runtime UID/GID is
10001. Grant that identity read access to the token and traverse access to its
parent directories, using ownership or a narrowly scoped ACL.

The token is loaded at startup by `serve`, `check` and `capture`; missing,
unreadable or invalid files fail startup. Restart after rotating it, including
when a mounted Kubernetes Secret changes. The local CLI validates the configured
file but does not require an Authorization header. Use TLS at an ingress or
reverse proxy when sending Bearer tokens over a network; the application serves
HTTP.

## Container requirements

Use the [quick-start Docker command](../README.md#quick-start) with a configuration
mount. UID/GID 10001 must read configuration, tokens and supplied bundles, and
write `/work`, `/tmp` and `/dev/shm`. Provide shared memory with `--shm-size=512m`
and writable scratch with the shown tmpfs mounts. Prepared bundles should be
mounted read-only; keep credential-bearing data private.

The image supplies the same pinned full Chrome for Testing version on Linux
amd64 and arm64, with Noto fonts for multilingual text, CJK and color emoji.
There are no runtime browser downloads. Check representative pages when changing
images or profiles because font selection and CSS fallback can affect layout.

### Browser sandbox

Chromium's sandbox is enabled by default. The host/container policy must permit
its user-namespace sandbox. If startup reports `No usable sandbox`, follow the
[node-policy guide](../deploy/sandbox/README.md) for the pinned browser's seccomp
and AppArmor setup.

The `sandbox` setting accepts JSON booleans. Omission, `null` and `""` mean
`true`; only an explicit `false` disables the sandbox. The former `no_sandbox`
field is no longer accepted; migrate `no_sandbox: true` to `sandbox: false` and
`no_sandbox: false` to `sandbox: true`. Helm uses `browser.sandbox` with the same
default and empty-value behavior.

Incompatible policies fail startup; the service never retries without a sandbox.
An operator can explicitly set `sandbox: false` (Helm:
`browser.sandbox=false`), which removes Chromium's sandbox isolation. Do not
substitute privileged mode or `SYS_ADMIN` for compatible node policies.

### Shutdown and cleanup

Each capture or source validation has its own deadline and scratch directories.
Request cancellation stops its job. SIGTERM stops admission and readiness,
cancels active/queued work and waits for browser/file cleanup before HTTP
shutdown. SIGKILL or node loss cannot run cleanup; Kubernetes `emptyDir` scratch
is reclaimed when the Pod is removed.

## Redis metadata cache

Omit `cache`, or set `"cache": {"backend": "memory"}`, to keep the existing
process-local LRU with no external dependency. Select Redis for shared validation
metadata across application replicas:

```json
{
  "cache": {
    "backend": "redis",
    "redis": {
      "address": "redis:6379",
      "namespace": "webcappi-production",
      "generation": "capture-v1",
      "username": "default",
      "password_file": "/run/secrets/redis/password",
      "tls": true,
      "timeout_ms": 500,
      "retention_seconds": 86400
    }
  }
}
```

`address`, `namespace` and `generation` are required. Namespace and generation
accept 1–64 ASCII letters, digits, underscores or hyphens. The address is a
single Redis primary endpoint (`host:port`); Redis Cluster and Sentinel discovery
are not implemented.

Authentication and TLS are optional. Passwords are read once from an absolute
file path (1–4096 bytes; trailing CR/LF removed). Use Kubernetes Secrets or
read-only Docker mounts. `username` selects a Redis ACL user. TLS verifies the
server hostname and certificate using system roots; optional `ca_file` adds a
PEM CA bundle from an absolute path. Neither insecure TLS nor passwords in the
address are supported. Provision the Redis endpoint and credentials separately.

`timeout_ms` defaults to 500 and accepts 1–5000; zero uses the default.
Connection pools are capped at the smaller of `max_requests` and 16 per Pod.
Automatic command retries are disabled; the script-cache fallback is bounded by
the same operation deadline. Cache I/O and capture share the capture deadline.
The Redis ACL must permit connection initialization, PING, EVAL/EVALSHA and the
TIME, EXISTS, DEL, HGET/HSET/HDEL, ZADD/ZREM/ZCARD/ZRANGE/ZRANGEBYSCORE and PEXPIRE
commands used by the scripts, restricted to the application's key prefix.

`metadata_max_entries` and `metadata_max_bytes` apply to the **whole Redis
scope**, including update fences and approximate record/index overhead, rather
than to each Pod. Zero capacity disables retention. Redis's actual memory use
also includes allocator and server overhead; configure a separate `maxmemory`
limit with headroom. Use a dedicated cache instance with `noeviction`; capacity
exhaustion produces an error. Application scripts enforce bounded LRU eviction.
Partial loss of the namespace's Redis keys invalidates the remaining records
and update tokens together.

Freshness (`max_age_seconds` and source cache headers) is independent of physical
retention. `retention_seconds` defaults to 86400, must cover `max_age_seconds`,
and is limited to 604800. Stale entries remain available for source revalidation
until that retention deadline or capacity eviction. Access does not renew either
validation time or retention. Expired records are removed on cache operations;
inactive scopes also expire automatically. Freshness and Age use a Redis clock
reference with conservative network-delay accounting, so differing Pod clocks
do not extend shared freshness.

Replicas share a scope when namespace, generation, architecture, pinned browser,
profile descriptors (excluding template paths), resize algorithm, freshness and
capacity/retention settings, and capture timeout match. The Redis key prefix is
`webcappi:{<scope SHA-256>}:`. Profile contents, extension code, fonts and image
contents are not hashed automatically: **change generation when any of these
change**, and use the same immutable image/bundles across Pods sharing it. A
rolling update can run old and new generations together without reusing their
metadata. Different architectures are isolated. Old generations expire without
flushing a shared Redis database.

Redis is a required dependency when selected. An initial connection failure stops
startup. A lookup or write failure returns 503 with `Cache-Control: no-store`;
there is no automatic memory fallback and no stale-on-error response. Readiness
checks Redis connectivity, while liveness remains process-only. HTML POST and
direct CLI captures do not access Redis. Redis replicas/failover can lose recent
writes according to the Redis deployment's durability settings; this cache does
not provide stronger consistency than its Redis primary.

For HTTP freshness behavior and the limits of source validation, see
[conditional GET](API.md#conditional-get-and-freshness). PNG data is never
stored in Redis.

## Kubernetes

The [Helm chart guide](../charts/webcappi/README.md) covers installation, Secrets,
prepared bundles, memory or external Redis, routing, upgrades and rollback.
Prepare compatible [sandbox node policies](../deploy/sandbox/README.md) before
deploying.

For raw manifests, [deploy/kubernetes.yaml](../deploy/kubernetes.yaml) provides
a single-replica Deployment, ClusterIP Service, configuration, probes, resource
limits and writable scratch. Create the API Secret in the deployment namespace:

```sh
kubectl create secret generic webcappi-auth \
  --from-file=token=/secure/path/webcappi-token
```

Set the application image and review configuration/node policy references, then:

```sh
kubectl apply -f deploy/kubernetes.yaml
```

For multiple replicas, use
[deploy/kubernetes.redis.yaml](../deploy/kubernetes.redis.yaml) instead. It expects
an authenticated Redis primary Service named `redis` in the same namespace and
a separate password Secret:

```sh
kubectl create secret generic webcappi-redis \
  --from-file=password=/secure/path/redis-password
kubectl apply -f deploy/kubernetes.redis.yaml
```

Adjust the Redis address, ACL user, TLS/CA settings and generation before applying.
ConfigMap updates require a Pod restart to take effect. Each Pod has its own
scratch, concurrency and queue; Redis shares metadata, not capture capacity.
