# Development guide

The [README](../README.md) covers setup and capture requests. Detailed references
cover the [API](API.md), [operations](OPERATIONS.md) and [profiles](PROFILES.md).
This guide describes implementation boundaries, regression checks and the scope
of recorded validation. Browser choices, extension experiments and font
measurements are in [BROWSER.md](BROWSER.md).

## Toolchain and local build

Use the Go toolchain pinned in [Dockerfile](../Dockerfile): **Go 1.26.10**.
The module minimum is Go 1.26.0 (required by go-redis v9.23.0). The browser is
full Chrome for Testing
**156.0.8078.47** on both Linux amd64 and arm64; chromedp is **v0.21.0**.
Docker with Buildx is needed for image builds/browser checks, and Python 3 for smoke
tests. Host Go compilation is needed by the browser-test script even when the
application runs entirely in containers.

Build and load the application image for the host architecture:

```sh
make image
```

The local tag is `webcappi:dev`. Use it in place of the published image in the
[quick start](../README.md#quick-start) to run your changes. To select a platform:

```sh
make image PLATFORM=linux/amd64 IMAGE=webcappi:dev-amd64
make image PLATFORM=linux/arm64 IMAGE=webcappi:dev-arm64
```

The platform selects both the application and browser architecture. Running a
foreign-architecture image requires runtime emulation; native runners are
preferred. Multi-platform builds and CLI exports are covered under
[build artifacts](#build-artifacts-and-platforms).

To build only the native executable:

```sh
GOTOOLCHAIN=go1.26.10 make build
```

This writes `bin/webcappi`. Native captures still require the pinned browser,
shared libraries, fonts and a suitable sandbox policy; see the
[operations guide](OPERATIONS.md#container-requirements) for runtime requirements.

For native Linux execution, use the same Chrome for Testing binary, dependencies
and fonts as the image, edit absolute paths in the configuration and descriptor,
and prepare any configured profile at those exact extension paths. The container
is the preferred way to keep these dependencies consistent.

```sh
cp config.example.json config.json
# Match profiles.default to the generated bundle.json, or omit profiles.
# For an initialized bundle: set extensions to [] and omit ready_selectors.
# Create the configured token file, or omit auth to disable HTTP authentication.
./bin/webcappi check -config config.json
./bin/webcappi serve -config config.json
```

`check` validates configuration, bundle descriptors, browser version, scratch
access and a real browser launch on each profile clone. `serve` runs the HTTP
API until shutdown. For a single capture, run `capture` instead:

```sh
./bin/webcappi capture -config config.json -url http://page-service:8000/ -out capture.png
```

`capture` calls the capture service directly and saves the PNG without starting
an HTTP server. `version` prints the application version, source commit,
modified state and supported browser version. The Makefile requires Python 3
and uses Git metadata when available. A direct `go build` defaults to `dev` and
uses Go's VCS metadata when available; the supported release build paths pass
identity explicitly and disable automatic VCS stamping so exported and image
binaries agree.

### Profile artifact images

After [preparing a bundle](PROFILES.md), package it independently of the API image:

```sh
docker build -f deploy/bundle.Dockerfile -t webcappi-profiles:default-20261023-1 bundles
```

The Dockerfile copies the bundle with ownership 10001:10001 so an initContainer
can read private files. Use the [chart's profile settings](../charts/webcappi/README.md#profiles)
to copy `/bundle/default` into a Pod-local `emptyDir`, then mount it read-only at
`/bundles` in the API container. Keep the configured descriptor in sync with
`bundle.json` and qualify each bundle with the target architecture and API image.

### Local Redis fixture

The repository's Compose example builds the local application image and starts
an internal Redis with no published Redis port or persistence:

```sh
docker compose -f deploy/compose.redis.yaml up --build
```

It binds the API to loopback, uses unauthenticated/unencrypted Redis and preserves
the browser sandbox. Stop and remove both containers with:

```sh
docker compose -f deploy/compose.redis.yaml down
```

### Formatting and static analysis

Install the validated tool versions before running the checks:

```sh
GOTOOLCHAIN=go1.26.10 go install honnef.co/go/tools/cmd/staticcheck@v0.8.1
uv tool install ruff==0.16.8
```

Add `$(go env GOPATH)/bin` and the uv tool executable directory to `PATH`.
Staticcheck is built with the pinned Go toolchain. Ruff is a development tool;
the container smoke scripts only need Python 3 and Docker. The chart checker
also needs the pinned PyYAML dependency described below.

```sh
GOTOOLCHAIN=go1.26.10 make format-check
GOTOOLCHAIN=go1.26.10 make lint
GOTOOLCHAIN=go1.26.10 make format
```

| Target | Go | Python |
| --- | --- | --- |
| `format-check` | Report unformatted files with the selected Go toolchain's `gofmt`; fail if any are found. | `ruff format --check scripts/` |
| `format` | `go fmt ./cmd/... ./internal/...` | `ruff format scripts/` |
| `lint` | `go vet` followed by `staticcheck` on `./cmd/... ./internal/...`, including tests. | `ruff check scripts/` |

Each target also has `-go` and `-python` variants, such as `make lint-go` and
`make format-check-python`. Go targets cover `cmd/` and `internal/`, including
test files, and exclude generated probes under `artifacts/`. Python checks
cover `.py` files in `scripts/`; Python embedded in shell scripts or generated
strings is outside their scope.
`ruff.toml` sets an 88-character formatting line length and explicitly selects
the `E4`, `E7`, `E9` and `F` lint rules.

`format-check` and `lint` do not rewrite source files. Only `format` rewrites
formatting; it does not apply lint fixes. The targets use installed tools and
do not install them automatically. Override `GO`, `STATICCHECK` or `RUFF` to
select another executable or command, for example
`make lint-python RUFF='uvx --from ruff==0.16.8 ruff'`.

## Continuous integration

[GitHub Actions](../.github/workflows/ci.yml) runs on pull requests, pushes to
`main`, and manual dispatch. A newer run cancels the previous run for the same
ref. Jobs have explicit timeouts and read-only repository permissions.
[Reusable verification](../.github/workflows/verify.yml) supplies the checks,
Go/Redis matrix and native browser/API matrix to both CI and Release. The shared
[artifact build action](../.github/actions/build-artifacts/action.yml) supplies
multi-platform builds and OCI verification.

| Job | Checks | Platforms |
| --- | --- | --- |
| Formatting and static analysis | `make format-check lint`, `go vet ./...`, Actionlint v1.7.12, and Python build/release tests. | Linux amd64 |
| Go and Redis | `GOFLAGS=-count=1 make test` and `make redis-test`, both with race detection. | Native Linux amd64 and arm64 |
| Browser and API | `make image`, `make browser-test`, `python3 scripts/container-smoke.py`, and `python3 scripts/redis-smoke.py`, in sequence. | Native Linux amd64 and arm64 |
| Multi-platform build | `make binaries`, `make image-multi`, and `python3 scripts/verify-build.py`. Runs after runtime checks, on `main` pushes and manual dispatch only. | Both target architectures; arm64 build steps use QEMU |

The native runners are `ubuntu-24.04` and `ubuntu-24.04-arm`. The shared local
setup action reads the exact Go version from the build stage in
Dockerfile. Staticcheck and Ruff use the versions documented above. The workflow
does not publish images or deploy the application.

All external `uses:` references are pinned to full commit SHAs with release
comments. CI infrastructure and fixture images are pinned to multi-platform
SHA-256 digests: BuildKit, QEMU, and Redis/Python test fixtures. Dockerfile
frontend/base images retain their existing tags without digest pins.
Update tags/comments and hashes together after checking the upstream
release and both architectures. Locally built application/test images use local
tags because their contents come from the checked-out source.

Go dependency/build caches and architecture-specific Docker build caches reduce
setup time. Tests still execute on every run. Each browser matrix job has a fresh
artifact directory; it runs smoke scenarios without concurrent heavy builds.
Screenshots and test logs are retained for seven days, including after failures.
Synthetic browser profiles and the large OCI archive are not uploaded. Successful
multi-platform jobs retain the CLI binaries and `build-verification.json` for
seven days. Downloaded ZIP artifacts do not preserve executable permission bits.

The build verifier checks both ELF machine types, follows nested OCI indexes,
excludes attestation descriptors, checks runtime descriptor/config agreement,
compares each image executable's SHA-256 with its exported CLI binary, and
checks image labels against the requested build identity. This is build
verification; runtime qualification comes from the native browser jobs.
It can be rerun locally after the existing artifact-export commands below:

```sh
python3 scripts/verify-build.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

The existing test-only sandbox exception remains confined to disposable tests.
Passing CI does not qualify a sandbox-enabled production host or Kubernetes
deployment. Branch protection and required-check settings are managed separately
in GitHub. The first hosted run must pass before claiming hosted-runner runtime
verification.

Local verification for the CI introduction (2026-10-25) passed on Linux amd64:
Actionlint, toolchain extraction, formatting/static analysis, Go race tests,
`go vet ./...`, Redis integration tests, real-browser/CDP font checks, both
container smoke suites, and the five build-verifier unit tests. Both architecture
exports passed `make binaries`, `make image-multi`, and `verify-build.py`, with
matching CLI/image executable hashes. The pinned BuildKit accepted `cacheonly`
output, and the arm64 build-time Chrome version check passed under emulation.
Hosted workflow execution and native arm64 runtime checks remain unverified.
After removing the Dockerfile image digests, workflow lint and Go-version
extraction were rechecked; image/runtime suites were not rerun for that adjustment.

## Code organization

| Location | Responsibility |
| --- | --- |
| [cmd/webcappi](../cmd/webcappi) | Command dispatch, flags, startup resources and command-owned service lifetimes. CLI capture calls the service directly and writes the output file. |
| [internal/captureinput](../internal/captureinput) | Transport-independent validation and normalization into `capture.Request`. Limits and profile descriptors are passed as options; `config` and `capture` do not depend on this package. |
| [internal/httpapi](../internal/httpapi) | Authentication, routing, query parsing, admission, HTML input, conditional GET and responses. `request.go`, `html_request.go`, `conditional.go` and `response.go` separate those responsibilities. |
| [internal/config](../internal/config) | Configuration validation, configured-template placement, static extension registration and scratch access. |
| [internal/capture](../internal/capture) | Browser/profile lifecycle, shared profile-file validation, readiness, navigation, PNG generation, resizing and cleanup. |
| [internal/service](../internal/service) | Bounded active/queued jobs, independent cancellation and deadlines, content-derived ETags and request keys. |
| [scripts/smoke_support.py](../scripts/smoke_support.py) | Docker/exec/container/network lifetimes, settings, HTTP operations, bounded readiness waits and scenario diagnostics. |

Keep adapters thin without making them interchangeable: HTTP query/body rules
belong to HTTP, and CLI flags/files belong to the CLI. Do not reintroduce a
synthetic HTTP request, response writer or metadata cache for CLI captures.

## Input and response compatibility

`captureinput.Input` preserves omission through pointer fields. Omitted output
dimensions independently inherit the corresponding viewport dimension; explicit
zero is invalid. Omitted and explicitly empty profiles differ. A nil URL selects
HTML input, whose mode, content type and body are validated by HTTP.

The HTTP capture path keeps this order:

1. Authenticate before routing and query validation, except public guide/probes.
2. Check route/method/draining state and validate the query.
3. Acquire admission before reading or buffering an HTML body.
4. For POST, validate content type/charset/encoding and read the bounded body.
5. Handle GET validation metadata, call the service when required, then respond.

Query checks cover raw length, decoding and unknown/repeated keys, followed by
POST mode or GET URL validation. Shared validation then checks all numeric
dimensions, viewport bounds, output defaults/bounds, delay, profile and selector.
Adapters preserve this precedence even when an HTTP numeric string cannot be
parsed. Ordering within multiple unknown/repeated keys or invalid dimensions is
unspecified because those groups iterate Go maps; query order does not determine
the first error. Private addresses and Kubernetes Service DNS remain accepted.

CLI flags are parsed before configuration and browser startup. Repeated flags
use the last value, unlike repeated HTTP parameters. `serve` and `check` retain
their historical acceptance of capture flags. Unknown commands fail at dispatch
without starting a browser. Capture-parameter validation follows runtime
preparation, so a startup error can precede an invalid URL/dimension error.

The CLI retains its legacy 16 KiB encoded-input size check without constructing
an HTTP request. Capture errors use `capture HTTP <status>: <JSON>` with JSON
HTML escaping and no trailing newline. `main` logs errors to stderr and exits 1;
successful captures write PNG bytes to `-out`, creating files with mode 0600.
These behaviors are compatibility constraints, not a requirement to route the
CLI through HTTP. HTTP status/header/JSON behavior is covered separately.

## Profiles and resource lifetimes

`capture.CheckProfileFiles` checks the sibling `bundle.json` against the complete
descriptor and opens the required `Local State` and selected Profile Directory's
`Preferences`. It supports directories other than `Default`. Errors preserve
underlying file/context errors with caller context for the configured profile
name or temporary template.

Configured templates must remain separate from WorkDir, including after symlink
resolution and in both directions of directory overlap. Static extension
registration checks and scratch write checks remain in `config`. Generated
templates intentionally live under WorkDir and must not receive the configured
placement restriction. Startup also checks real browser activation of each
profile's extensions. Readiness checks files and scratch without launching a
browser or repairing a template; losing a generated template fails readiness.

Startup loads configuration and authentication before browser work. Browser
version validation, configured-file checks, default generation and all profile
launch checks share one 30-second context. Do not reset the deadline per step.
Capture the configured-file readiness checks before adding the generated default
to a cloned profile map. Jobs use the process context with their own deadline,
not the completed/canceled startup context.

Each capture clones the complete User Data Directory and reuses saved extension
installation without changing the template or reinstalling extensions. Keep
extension code separate and preserve IDs and absolute paths. Capture an
explicitly created page. Readiness remains selectors, fonts/visible images and
frames, then optional extra delay; network-idle is not a substitute.

Commands own services until browser and file cleanup finishes. Shutdown drains
capture admission/readiness, cancels and waits for active/queued jobs, shuts down
HTTP and waits for its serving goroutine, then removes the generated template.
Listen failure must also close the service. CLI capture closes its service before
template deletion; `check` deletes its template on return. Partial initialization
releases acquired resources, and profile initialization cleans its own partial
browser/files. Preserve a startup/command error over a cleanup error; after a
successful command, return a template cleanup error.

Job deadlines include queueing, profile copy, browser startup, rendering and
resizing. Cleanup has a separate grace period, and a slot is not released until
browser/file cleanup has completed. Keep concurrency, queueing, HTTP admission
and metadata capacity bounded.

## Metadata update invariants

The selected memory/Redis store retains only validation metadata. Do not add
PNG retention or coalesce independent captures. Keys include URL,
viewport/output dimensions, profile name, request selector and delay, plus fixed
renderer attributes. Omitted and explicit viewport-sized outputs share a key.

Allocate a metadata update fence after admission and POST body work, before
conditional lookup or a service call. Memory uses a process-local order and
bounded in-flight fences; Redis allocates a sequence under a namespace epoch in
capacity-bounded records. Only a newer completed update supersedes an older
update; a read or failed capture cannot suppress a successful update. An evicted
or expired token cannot publish a result. A newer `no-store` result removes the
previous entry. Source 304 processing merges policy/validators, clears old
Date/Age accounting and retains the PNG ETag without changing the service's
`Image` or its source headers.

Keep size/entry enforcement in the selected store. Redis records and LRU/expiry
indexes share one hash slot; scripts atomically enforce fences, deletion and
approximate byte/entry budgets. An incomplete script leaves a marker so the next
operation discards partial state and changes the namespace epoch. Do not add a
local fallback or store PNG bytes. Redis retention is separate from HTTP
freshness, and reads never renew either. The Redis clock reference must account
conservatively for network delay.

Fresh matching metadata may return 304 without a browser and without renewing
its validation time. Expired metadata needs source validation or recapture
before deciding 304. Unconditional GET and all HTML POST requests remain
independent captures. See the [API reference](API.md#conditional-get-and-freshness)
for client-visible freshness semantics and
the limits of treating main-document validators as evidence about a screenshot.

## Redis integration checks

```sh
GOTOOLCHAIN=go1.26.10 make redis-test
```

This starts a disposable Redis 8.2.2 container bound to an ephemeral loopback
port, runs the real-Redis tests with the race detector both with and without
file-based password authentication, and removes the container on exit. No production Redis is used. To supply a dedicated disposable server,
set `WEBCAPPI_REDIS_TEST_ADDR` and run
`go test -race -count=1 ./internal/httpapi -run TestRedis`. Tests use unique
namespaces, delete their own data, and flush the server's script cache; do not
point them at a shared production Redis. Ordinary `make test` skips tests that
need an actual Redis server; scope/timeout/memory-fence tests still run.

The scenarios cover cross-instance 304 responses, source revalidation and header
filtering, Pod clock differences, unconditional/HTML captures, concurrent update
fences, no-store deletion, LRU and byte budgets, retention and Age, namespace
loss, script-cache loss, dependency failures, readiness/liveness, reconnects and
incompatible serialized metadata. A local TLS proxy checks trusted and untrusted
certificates against real Redis traffic. The single-endpoint implementation
does not qualify Redis Cluster/Sentinel or an actual Kubernetes failover/rollout.

The real-container Redis smoke also exercises Chromium and automatic profiles:

```sh
python3 scripts/redis-smoke.py
```

It creates a private Docker network with Redis, a static fixture and three API
containers. It checks cross-replica validation, application restart reuse,
generation isolation, Redis outage/recovery, and Redis-independent HTML/CLI
captures. The browser sandbox exception is confined to its disposable test
configuration, as in the existing smoke scripts. All containers/networks are
removed on success or failure. It uses `WEBCAPPI_IMAGE`, `PLATFORM` and
`WEBCAPPI_ARTIFACTS_DIR` through the shared smoke helpers.

## Regression checks

Run checks relevant to the change with the pinned toolchain:

```sh
GOTOOLCHAIN=go1.26.10 GOFLAGS=-count=1 make test
GOTOOLCHAIN=go1.26.10 make format-check lint
```

`make test` runs race detection. Without browser environment variables it skips
real-browser tests; a passing Go run alone does not qualify browser behavior.
`-count=1` ensures a fresh test execution when recording regression evidence.

| Boundary | Maintained coverage |
| --- | --- |
| Shared input and CLI | Omitted/zero and one-sided dimensions, bounds, URLs, private targets, profiles/selectors/delay, validation precedence, CLI/HTTP parity, exact CLI errors and file failures. |
| HTTP and metadata | Authentication/admission before body reads, input/error headers, freshness and source validation, no-store removal, concurrent result ordering and immutable service results. |
| Profiles and startup | Descriptor/required-file errors, non-Default directories, cancellation, resolved placement, default selection, shared startup deadline, partial acquisition cleanup and error priority. |
| Service and shutdown | Bounded queue/admission, independent jobs, active/queued cancellation, deadlines, cleanup before release and occupied-port failure. |
| Real browser | Persisted extensions, explicit capture tabs, immutable complete templates, CDP fonts, HTML modes/charsets, waits, output sizing, source navigation and process/file cleanup. |

### Browser and REST checks

Choose a fresh artifact directory for every run. Substitute a new name for
`verification-amd64-run1` if it already exists:

```sh
export GOTOOLCHAIN=go1.26.10
export PLATFORM=linux/amd64
export WEBCAPPI_IMAGE=webcappi:verification-amd64
export WEBCAPPI_ARTIFACTS_DIR=artifacts/verification-amd64-run1
make image IMAGE="$WEBCAPPI_IMAGE"
make browser-test
python3 scripts/container-smoke.py
```

`browser-test.sh` builds the test-browser image and a target-architecture test
binary with `CGO_ENABLED=0`, without race instrumentation, then runs it under
Xvfb. The separate Go suite supplies race detection. Browser and smoke tests use
synthetic state and private local HTTP/Docker DNS fixtures, not personal profiles
or an external website. Sandbox opt-out is confined to disposable test containers;
keep the production default enabled.

The browser test does not overwrite an existing exported `user-data` directory.
Use its fresh template for subsequent smoke checks, not one from an earlier
run. `WEBCAPPI_IMAGE` selects the REST/profile application image; the browser
script builds its own `test-browser` target. `PLATFORM` must agree with the image.
Repeat runtime checks on a native arm64 runner or with working runtime emulation.

Exports under `WEBCAPPI_ARTIFACTS_DIR` include `extension.png`, `html.png`,
`html-loopback.png`, `api.png`, `api-resized.png`, `api-html.png`,
`api-html-file.png`, `api-html-loopback.png`, `initialized-profile.png`,
`automatic-default.png`, `default-profile.tar`, and synthetic `user-data/`.
The fixture extension lives at `/src/testdata/extension` in the container; the
exported synthetic profile requires that path and is not a production bundle.

`container-smoke.py` invokes `profile-smoke.py` before its REST scenarios. Tests
cover authentication, independent captures, freshness/source transitions,
delay/errors, resize/CLI, HTML/encoding and shutdown/immutability. Profile checks
include tar paths/types/permissions/ownership, automatic/named/imported profiles,
template loss and SIGTERM/`check` cleanup while containers and scratch remain
alive. Container deletion or tmpfs disappearance is not proof of app cleanup.

Keep scenario URLs and configuration explicit. Count render starts by before/
after differences within a scenario, not a global count inherited from an earlier
one. Keep related cache transitions in the same scenario. Shared helpers must
register Docker names before creation, bound readiness waits, drain exec output,
reap clients, release containers before networks and preserve the original error
while reporting cleanup failures. Failures need scenario, HTTP and log context.

Avoid running deadline-sensitive smoke scenarios alongside heavy builds. A
10-second smoke job budget includes time queued behind independent captures;
host contention can cause a timeout. Preserve the first failure's diagnostics,
rerun in isolation and report both outcomes without silently changing deadlines.

For Python edits, parse the changed files with `ast.parse`; use `bash -n` for
shell edits, then perform real-container checks. Syntax success is not runtime
verification. After failure-path changes, check readiness failure, mid-scenario
exceptions, partial launch and attached-command timeout; verify diagnostics and
the absence of test containers/networks. Use a live container to inspect browser
and file cleanup after listen failure or active-capture SIGTERM.

## Releases and GHCR

The [Release workflow](../.github/workflows/release.yml) runs on pushes of `v*`
tags. A tag must be `vX.Y.Z` or `vX.Y.Z-prerelease`, point to the event commit,
and identify a clean checkout whose commit belongs to `origin/main`. Leading
zeros in numeric version identifiers and SemVer build metadata (`+...`) are
rejected; the latter cannot be represented directly in a Docker tag. Git tags
are the source of truth; there is no separately maintained VERSION file.
Choose subsequent versions according to API/CLI compatibility. The first
chart-enabled release is planned as `v0.1.2`; `v0.1.0` and `v0.1.1` predate
chart distribution. Prereleases such as `v0.2.0-rc.1` are supported.

After merging the implementation to main, release a reviewed main commit:

```sh
git switch main
git pull --ff-only
git tag -a v0.1.2 -m 'Release v0.1.2'
git push origin v0.1.2
```

The tag push starts release validation and the full reusable verification suite,
including native amd64 and arm64 browser/API checks. Only after those pass does
the publishing job build the versioned CLI binaries and multi-platform OCI
archive. The build verifier checks both platform configurations, image labels
and executable hashes against the exported CLI binaries. The archive is
extracted into a temporary OCI directory without restoring file ownership
(Python 3.12 or newer); Skopeo copies all platforms with preserved digests,
without rebuilding the image. The temporary directory is removed on failure
or completion, and publication does not require running Skopeo as root.
The published platform manifests and top-level digest are checked again. The
final OCI archive is build-verified; native runtime qualification is supplied by
the preceding browser/API jobs using the same source commit and build identity.

Image publishing uses `GITHUB_TOKEN` with `contents: read` and
`packages: write`, confined to the image publishing job. The subsequent GitHub
Release job has `contents: write` and `packages: read`; validation and native
verification retain read-only repository access. No PAT is required. Registry credentials live
in the runner's temporary directory and are removed after the job. The image's
`org.opencontainers.image.source` label links it to this repository. GHCR creates
new packages as private: after the first successful push, set the package
visibility to public if anonymous pulls are intended, and check its repository
Actions access. See [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

Git tag `v0.1.0` publishes image tag `0.1.0`; OCI labels and the binary retain
`v0.1.0` and the full source commit. Prerelease tags never update `latest`.
Publishing jobs are serialized, and numeric version comparison prevents an
older release or rerun from moving `latest` backwards. Existing exact version
tags are checked on both platforms and reused only when their version, source,
commit and clean state agree. A differing identity or digest fails rather than
overwriting the version. Reruns can complete a previously interrupted `latest`
update without rebuilding the released image. Do not move release Git tags,
manually overwrite version image tags, or run a second publisher for this image.
If several releases are queued, GitHub concurrency may replace a pending job;
rerun the affected workflow after the active publication finishes.

The job summary records the exact image and multi-platform digest for deployment
pinning and rollback. CLI binaries and `build-verification.json` are retained as
Actions artifacts for seven days. Chart-enabled tags next qualify the actual
distribution archive, publish/reuse it, and qualify its anonymous download on
both native architectures. Only then does the workflow call
[GitHub Release](../.github/workflows/github-release.yml) with the verified tag,
commit, image digest and chart acceptance artifact. The large OCI archive and
synthetic browser profiles are not uploaded.

### GitHub Release automation and recovery

[`scripts/github_release.py`](../scripts/github_release.py) verifies the local
and remote tag, its ancestry on main, both platform image labels and the pinned
multi-platform digest. It copies each runtime platform manifest by digest into
a temporary OCI layout using Skopeo, then extracts the application binary
without executing the image. Archive generation therefore also works for arm64
on an amd64 runner. The same published image supplies binaries on first runs,
retries and manual recovery; there is no rebuild or dependency on expiring
Actions artifacts. The image must remain available to the workflow repository.

A GitHub Release contains these assets (shown for `v0.1.1`):

- `webcappi_v0.1.1_linux_amd64.tar.gz`
- `webcappi_v0.1.1_linux_arm64.tar.gz`
- `release-manifest.json`, with source commit, image digest, platform manifest
  digests and executable SHA-256 hashes
- `SHA256SUMS`, covering both archives and the release manifest

Tar/gzip metadata is fixed, and the executable mode is 0755. Release notes are
generated by GitHub and include the container tag, pinned digest and source
commit. Existing user-written notes are preserved. A draft is created first;
all assets are uploaded and checked before it is published. This ordering
supports repositories with immutable releases enabled. An interrupted draft is
found through the authenticated release listing, because the tag endpoint only
guarantees published releases. Retries reuse matching assets and reject
conflicting contents without overwriting or deleting them. A published immutable
release missing required assets cannot be repaired by this workflow; publish a
new version instead. For example, an already published immutable `v0.1.0`
without CLI assets must remain as published; use a subsequent version.

Prerelease tags create GitHub prereleases and cannot become Latest. GitHub
Release jobs share a concurrency group, and numeric version comparison prevents
older versions from replacing a newer Latest release. GHCR image publication
has its separate existing concurrency group and version policy.

To recover the GitHub Release step after its workflow code is on main, choose
**GitHub Release → Run workflow**, select **main**, and enter an existing version
tag whose GHCR image was successfully published. The equivalent command is:

```sh
gh workflow run github-release.yml --ref main -f tag=v0.1.1
```

This manual workflow only creates/completes the GitHub Release from the
published image; it does not create tags, build images or update GHCR aliases.
For chart-enabled tags it first reconstructs the candidate chart from the target
commit and reruns both archive and anonymous-public acceptance with read-only
package permissions. It never pushes charts. Older tag sources without
`scripts/release-charts.py` retain the binary-only path and asset names.
It can handle older tags without rerunning the older tag's workflow definition.
The GitHub Release job uses the repository's `GITHUB_TOKEN`; no additional
secret or external release action is required. Temporary registry credentials
and downloaded OCI layouts are cleaned up on completion or failure. Prepared
assets remain under `artifacts/github-release/` during the job.

Local Make targets derive `WEBCAPPI_VERSION`, `WEBCAPPI_REVISION` and
`WEBCAPPI_MODIFIED` from the checkout. A unique release tag is used only for a
clean checkout; otherwise the version is `dev`. Outside Git, source metadata is
`unknown`. All three values may be overridden explicitly, for example when
building from a source archive:

```sh
GOTOOLCHAIN=go1.26.10 make build WEBCAPPI_VERSION=v0.1.0 WEBCAPPI_REVISION='REPLACE_WITH_FULL_COMMIT' WEBCAPPI_MODIFIED=false
```

Replace `REPLACE_WITH_FULL_COMMIT` before running the command. CI and CD pass
the same values to native image checks, CLI exports and OCI exports. Builds intentionally
do not embed the wall-clock build time; identity depends on the source version
and commit. Direct `docker buildx build` invocations must supply these values as
`--build-arg` options; otherwise the defaults are `dev`, `unknown`, `unknown`.
For images, labels include `org.opencontainers.image.version`,
`org.opencontainers.image.revision`, `org.opencontainers.image.source`, and
`io.github.dayflower.webcappi.modified`.

## Build artifacts and platforms

Docker's platform selects the application and browser architecture. The Go stage
runs on `BUILDPLATFORM` and cross-compiles for `TARGETOS`/`TARGETARCH`. Browser
dependency installation and `chrome --version` execute target-platform programs
and require a suitable native worker or build-time emulation.

```sh
env -u PLATFORM make binaries PLATFORMS=linux/amd64,linux/arm64
env -u PLATFORM make image-multi PLATFORMS=linux/amd64,linux/arm64 IMAGE=webcappi:verification
file artifacts/binaries/linux_amd64/webcappi artifacts/binaries/linux_arm64/webcappi
```

The targets export both CLI binaries under `artifacts/binaries/` and the OCI
archive `artifacts/webcappi.oci.tar`, without publishing to a registry. Preserve
previous outputs before reusing these paths. `PLATFORMS`, not the single-platform
`PLATFORM`, controls these targets. Check both ELF architectures and actual
runtime manifests/configuration blobs in the OCI index; attestation descriptors
are not runtime platforms. Compare image executables with CLI exports when
checking the exported pair.

Select a configured builder through `BUILDX_BUILDER`. For a temporary builder:

```sh
docker buildx create --name webcappi-builder --driver docker-container --bootstrap
BUILDX_BUILDER=webcappi-builder env -u PLATFORM make image-multi PLATFORMS=linux/amd64,linux/arm64 IMAGE=webcappi:verification
docker buildx rm webcappi-builder
```

Remove only a builder created for that run; keep the user's default selection.
BuildKit may include `buildkit-qemu-aarch64` even when its advertised native
platforms omit arm64. Its ability to run build steps does not establish Docker
runtime emulation. Some hosts require administrator-provided emulation.
See [Docker's multi-platform build documentation](https://docs.docker.com/build/building/multi-platform/).

CI uses native amd64/arm64 runners in a matrix with the same Go, image, browser
and REST checks; see [continuous integration](#continuous-integration). The Make
targets export local artifacts without publishing to a registry.

## Changing the browser or fixtures

Update the pinned browser version in Dockerfile, the Go constant, browser-test
image tag and documentation together. Keep the same full browser version on
amd64 and arm64, unified headless extension support and runtime download-free
images. Dependencies and OS packages can affect rendering; qualify both
architectures and promote built images by digest.

Recheck persisted installation, IDs/paths/storage, welcome-tab independence,
readiness, CDP font selection, immutable templates, HTML modes, resizing and
cleanup. Fontconfig matches alone do not establish browser selection. Consult
the [browser notes](BROWSER.md) for measured behavior and source references;
the [profile guide](PROFILES.md#validate-and-update-bundles) describes staging and
deploying profile updates.

## Recorded validation scope

Previous full local regression: **2026-10-25 (Asia/Tokyo)** against implementation
commit `dacdc05151110ddb8fe80c8aa66c8284ead95924`, with Go **1.26.10** and full
Chrome for Testing **156.0.8078.47**. These are historical results, not a claim
that future changes or arbitrary production environments have been verified.

| Platform / scope | Recorded result |
| --- | --- |
| linux/amd64 Go | Race tests without test-result cache reuse and `go vet ./...` passed. |
| linux/amd64 browser and REST | Image build, real-browser tests without skips, both CDP font suites, REST/CLI/profile smoke and final artifact checks passed. |
| Both architectures: builds | CLI exports were x86-64 and AArch64. A completed OCI archive contained matching platform descriptors/configurations and application binaries matching the corresponding CLI exports. Both build-time Chrome version checks passed. |
| linux/arm64 runtime | Unverified. Docker could not execute even `/bin/true` in an existing arm64 image (`exec format error`); this was an execution-support probe, not final application qualification. |
| Production sandbox / real deployment | Sandbox-enabled container deployment and a live Kubernetes rollout were not verified. Third-party extensions, native messaging, profile migration and display forwarding need target-environment validation. |

Docker client/server were **29.9.1** on linux/amd64. The default Docker-driver
builder used BuildKit **v0.34.0** and advertised only amd64 variants. A temporary
Docker-container builder using `moby/buildkit:buildx-stable-1`, BuildKit
**v0.33.1** and its bundled QEMU completed both image builds without installing
host emulation or changing the default builder. It was removed after the run.

The initial REST attempt timed out one queued capture while the multi-platform
build was active. An isolated retry passed all three smoke summaries with
unchanged settings. Supplemental one-off Docker probes also checked four
failure/cleanup paths, occupied-port startup and active-browser SIGTERM. They
confirmed original-error diagnostics, reaped exec clients, 503 cancellation and
no remaining browser/files/templates while containers stayed alive. These
supplemental probes are not maintained entrypoints in the repository. Final
resource inspection found no test containers/networks or temporary builder.

Earlier deployment-example checks on **2026-10-23** exercised a profile artifact
image and init-style copy as UID/GID 10001 with a read-only root, and Kubernetes
client dry-run with `--validate=false`. They did not deploy to a cluster and
were not repeated as part of the October 25 regression.

When recording a new result, include date, implementation commit, tool/browser
versions, platforms, commands, outcomes, skipped checks and cleanup. Separate
build evidence from runtime evidence, maintained tests from one-off probes,
and current execution from earlier cached or historical results.


### Redis backend verification

Verified on **2026-10-25 (Asia/Tokyo)** in the working tree based on
`e2cfc7dc541b5eab408b1c60433f4c64e9e3ad20`, using Go **1.26.10**,
go-redis **v9.23.0**, Redis **8.2.2**, Chrome for Testing **156.0.8078.47**,
and Docker client/server **29.9.1**. These results qualify the local changes,
not an arbitrary Redis deployment or production environment.

| Scope | Result |
| --- | --- |
| Go | `make test` with the race detector, `go vet ./...`, Staticcheck v0.8.1 and Go formatting checks passed. Unchanged Go packages reused test-result cache entries; the browser test binary was executed separately. |
| Redis protocol and storage | `make redis-test` passed with and without file-based passwords, without Go test-result cache reuse. The TLS proxy verified trusted/untrusted certificates with real Redis traffic. Completed-update ordering, no-store, read/failed-request races, epoch loss, incomplete-script recovery, retention, capacity, timeouts, script reload and errors passed. |
| linux/amd64 runtime | `make image`, `make browser-test`, `python3 scripts/container-smoke.py` and `python3 scripts/redis-smoke.py` passed. Both CDP font suites and extension/profile lifecycle checks ran. The final smoke exercised actual captures across replicas, restart reuse, generation isolation and Redis outage/recovery. |
| Both architectures: builds | `make binaries` and `make image-multi` passed using a temporary Docker-container BuildKit builder. Both build-time Chrome version commands succeeded. One-off OCI inspection checked both runtime platform descriptors/configurations, ELF architectures and application binaries against the corresponding CLI exports. The loaded amd64 image's application binary also matched the CLI export. |
| Deployment examples | Compose configuration validation and Kubernetes client dry-run with `--validate=false` passed. No live Kubernetes resources were created and no Compose stack was deployed. |
| linux/arm64 runtime | Unverified. A host support probe using `/bin/true` in the existing arm64 image failed with `exec format error`; this did not execute or qualify the new application. BuildKit's bundled emulation supported image build steps. |
| Production isolation | The existing sandbox exception remained confined to disposable tests. Sandbox-enabled production containers and a live Kubernetes rollout were not qualified. Redis Cluster/Sentinel discovery and HA failover were not qualified. |

Browser artifacts are under `artifacts/redis-20261025-amd64/`; one-off OCI
inspection wrote `multi-build.json` there. CLI exports remain under
`artifacts/binaries/` and the image archive is `artifacts/webcappi.oci.tar`.
Previous build exports were preserved under
`artifacts/redis-build-before-qlo_0fid/`. Test containers/networks and the
run-specific builder were removed; images and verification artifacts were kept.


### Application version and GHCR workflow verification

Verified on **2026-10-25 (Asia/Tokyo)** in the working tree based on
`f6df9510c8d0908fdcdc2ad0664973da280cdeb9`, using Go **1.26.10**,
Chrome for Testing **156.0.8078.47**, Docker client/server **29.9.1**,
Staticcheck **v0.8.1**, Ruff **0.16.8**, Actionlint **v1.7.12** and Skopeo
**1.13.3**. Native runtime checks used the development identity with
`modified true`. Export/transfer probes explicitly injected `v0.1.0` and
`modified false` as synthetic release metadata; they did not create a Git tag
or publish a GHCR package.

| Scope | Result |
| --- | --- |
| Go and static checks | `make test` with race detection, `go vet ./...`, `make format-check lint`, Actionlint and Python AST checks passed. Unchanged Go packages reused test-result cache entries. |
| Build/release regression tests | All 17 Python tests passed, including annotated tags, commit/main ancestry, dirty checkouts, release syntax, label mismatches, authentication failures, version overwrite rejection, latest ordering and temporary OCI cleanup. |
| linux/amd64 runtime | `make image`, `make browser-test` and `python3 scripts/container-smoke.py` passed. Both CDP font suites and extension/profile lifecycle tests executed. Native and container CLI version output agreed. |
| Both architectures: builds | `make binaries`, `make image-multi` and `python3 scripts/verify-build.py` passed using a temporary Docker-container BuildKit v0.33.1 builder and its bundled build-time emulation. Both Chrome version commands passed; CLI/image executable hashes and all build identity labels matched. |
| Registry transfer | A one-off probe against a disposable loopback Registry 3.0.0 exercised first publication, exact-version reuse, latest promotion without rollback, prerelease exclusion and identity/digest conflict rejection. The actual OCI archive, including attestations and both runtime platforms, was copied with preserved digest `sha256:5248bf4fc0a0040e79b1e5c36ac8305dd506b0cc05f51a506aa0ae53390c67a9` and checked after publication. Skopeo ran as the ordinary user; HTTP was enabled only by a temporary wrapper for the loopback registry. |
| External/runtime limits | GitHub-hosted workflow execution, real GHCR authentication/publication, native arm64 application runtime and production sandbox deployment were not exercised. GHCR first-publication and authentication errors were covered by mocked HTTP responses. |

Browser outputs and the one-off Registry probe log are under
`artifacts/release-validation-amd64/`. New CLI/OCI exports and their report are
under `artifacts/release-validation-build/`; previous exports were restored to
their original locations. The disposable registries, test containers/networks
and run-specific builder were removed. Images and validation artifacts were kept.


### GitHub Release automation verification

Verified on **2026-10-25 (Asia/Tokyo)** in a working tree based on
`6bb2129e6379d0b008a92bbf3e804d16355fe1de`, with Python **3.12.3**,
GitHub CLI **2.45.0**, Skopeo **1.13.3**, Ruff **0.16.8** and
Actionlint **v1.7.12**. Go sources, Dockerfile and application build targets were
unchanged; the existing Go static checks used Go **1.26.10**.

| Scope | Result |
| --- | --- |
| Python and workflow checks | All 28 Python tests passed. Ruff format/lint, AST parsing, Actionlint and the existing Go static checks passed. Tests cover draft lookup/recovery, partial uploads, immutable releases, checksum conflicts, deterministic executable archives, annotated/remote tag agreement, API errors, prereleases, Latest ordering and digest outputs for downstream jobs. |
| Read-only GitHub integration | The new helper successfully read the existing `v0.1.0` Release, handled a missing tag's HTTP 404, resolved the annotated remote tag and checked its main ancestry. This did not modify the existing immutable Release. |
| Actual image asset recovery | A one-off loopback Registry probe reused the previously verified OCI archive recorded above, including its synthetic release metadata. Both runtime images were recovered by pinned manifest digest, their CLI archives were generated, and recovered executable hashes matched the original amd64/arm64 CLI exports. Archive executable modes and SHA-256 metadata were checked. This was artifact verification, not a new application build or runtime qualification. |
| Publication limits | GitHub Release creation, asset upload and final publication were exercised with a stateful fake API/CLI. GitHub-hosted workflow execution and real GitHub Release mutations were not exercised. Application/browser/native arm64 runtime checks were not repeated for this workflow-only change. |

Probe assets, the registry log and `recovery-verification.json` are under
`artifacts/github-release-validation/`. The disposable Registry and its storage
were removed; no GitHub Release, tag or GHCR package was created or changed.

## Helm distribution and recovery

Chart and application releases share one version. Before preparing a tag, confirm
that it is unused in Git, the image registry and the chart registry, then update
both chart metadata fields in a reviewed change:

```sh
python3 scripts/release-charts.py prepare-version --tag v0.1.2 --dry-run
python3 scripts/release-charts.py prepare-version --tag v0.1.2
make chart-check
```

The helper changes only `Chart.yaml`'s `version` and `appVersion`. It preserves
browser pins, CRDs and source image defaults; prereleases are supported, while
leading zeroes and build metadata are rejected by the existing release rules.
Even chart-only changes require an application patch release. The first planned
chart version is **0.1.2**, unused when this implementation was prepared.

The Release workflow validates chart metadata before verification. After the
existing image publisher verifies the multi-platform image, the read-only
[chart workflow](../.github/workflows/chart-verify.yml) copies the chart from the
release revision into a temporary directory. It sets the image repository,
version tag without `v`, and verified multi-platform digest there, runs strict
lint/render/schema/Go-loader checks and packages that copy. The checkout is never
rewritten. The candidate `.tgz` is retained for 30 days as an Actions artifact.

Native amd64 and arm64 runners run the enabled lifecycle, explicit disabled
regression and enabled Ingress/Gateway suite against this exact archive. Each
suite first installs it with its unmodified image defaults, verifies the actual
Pod image and performs authenticated captures, probe and authentication checks.
The remaining policy/lifecycle/network tests use the existing diagnostic image
derived from that default image. The reports distinguish the original image,
diagnostic image, native platform, requested/effective sandbox mode, node policy
and active Chromium evidence. Diagnostic additions do not replace the standard
image installation check. Every cluster/policy uses the existing qualified setup
and cleanup; no ambient kubeconfig is used.

Only the chart publishing job receives `packages: write`. All its runs share
`ghcr-webcappi-chart-publish` concurrency, covering existence checks, comparison
and push. The helper logs Helm into an isolated temporary registry configuration
using password stdin, and removes it on success, exception or handled SIGTERM.
Only a structured registry manifest 404 with `MANIFEST_UNKNOWN`/`NAME_UNKNOWN`
means absence. Token denial, authorization errors, unknown responses and
transport failures stop publication. Existing versions are never pushed again:
all archive file names and bytes must match the candidate. Tar/gzip timestamps
and ownership do not participate in that comparison; regular-file modes do.
Additional, missing,
duplicate, non-regular or unsafe archive entries are rejected.

After publication, both native runners pull with empty Helm and Docker credential
locations. They compare every file with the verified candidate, verify the chart
layer SHA-256 against the OCI manifest, check that the manifest has not changed,
and rerun all three suites using the downloaded archive. There is no local source
fallback. A failure at either architecture or sandbox mode prevents GitHub Release
publication. The final `chart-distribution.json` contains chart version and OCI
manifest digest, the actual downloaded archive SHA-256, content fingerprint,
source revision, image digest and all four native archive/public acceptance
records, including sandbox and network evidence. Validation-only runs cannot
produce this proof. It is attached to the draft and covered by `SHA256SUMS` before
the GitHub Release is published, together with fixed-version installation notes.
Binary archive names and contents remain compatible with older releases.
Successful receipts from earlier attempts of the same Actions run can satisfy a
failed-job retry only when source/image/content identity still matches. All
public receipts must agree on the registry digest and downloaded archive hash.

For local sources or a specific archive, use the same static suite without a
registry mutation (Go/PyYAML and `make chart-tools` are prerequisites):

```sh
python3 scripts/release-charts.py check --tag v0.1.2 --source charts/webcappi --validation-only
python3 scripts/release-charts.py check --tag v0.1.2 --source artifacts/charts/webcappi-0.1.2.tgz --validation-only
```

Omit `--validation-only` to run all three native runtime suites too. The default
image must already be available to local Docker; for a public distribution image,
pull its pinned digest first. For PR acceptance, `check --local-image <built-image>`
sets that image only in a temporary chart copy, exports its `.tgz` under
`<output>/checked/`, and tests the archive with its default image. The native CI
uses this path before any release image is published. It cannot be used by the
publication command. To prepare a candidate from a release revision and
validate the actual registry download without pushing:

```sh
python3 scripts/release-charts.py package --source-revision --tag v0.1.2 \
  --revision "$RELEASE_REVISION" --image-digest "$IMAGE_DIGEST"
python3 scripts/release-charts.py accept --public --tag v0.1.2 \
  --revision "$RELEASE_REVISION" --image-digest "$IMAGE_DIGEST" \
  --archive artifacts/chart-release/webcappi-0.1.2.tgz --output artifacts/chart-public
```

Set `RELEASE_REVISION` to the full target commit and `IMAGE_DIGEST` to the verified
multi-platform digest. Packaging inspects image labels/platforms with Skopeo.
`accept` also supports `--validation-only` for file/identity/static verification;
that does not qualify runtime or create an acceptance proof. `--public` is
read-only and is the only public-chart source; omit it for candidate-archive
acceptance. A specified bad/missing archive is always an error.

Initial GHCR chart visibility is an operator step: after the first chart push,
open the **charts/webcappi** package settings, link the source repository if
needed, set visibility to **Public**, and grant this repository the required
**Manage Actions access**. Check the application image's public visibility too.
Do not broaden workflow write permissions to work around access failures. The
first anonymous check can fail until this setup is complete; no completed GitHub
Release is announced in that state. After fixing visibility, run **GitHub Release
→ Run workflow → main**, with `tag=v0.1.2`. This reconstructs and qualifies both
candidate and public chart again on both architectures, with read-only package
permissions. It does not overwrite, delete or republish the chart. It also works
after the original Actions artifacts expire.

If publication never occurred, rerun the original tag's Release workflow after
fixing its failure. If an identical package exists, rerunning that workflow
reuses it and records the actual registry digest/archive hash. If image or chart
contents conflict, stop and release a new version. Never move tags or remove
packages to reuse a version. If native acceptance fails, preserve evidence and
fix the issue before retrying; changed source requires a new version. Interrupted
draft uploads reuse matching assets. A previously accepted chart record is
downloaded, checked against fresh acceptance identity and retained byte-for-byte
so varying diagnostic process IDs/timestamps do not conflict with immutable
assets or checksum files. A conflicting or incomplete immutable GitHub Release
requires a new version. Historical pre-chart tags keep the binary-only recovery
path; the presence of the distribution helper in the target tag's source,
rather than a hard-coded version cutoff, selects the chart requirement.

Implementation/PR validation and actual GHCR publication are separate results.
The first post-merge `v*` release must supply real anonymous acceptance and Release
asset evidence; mock publication tests cannot establish public availability.

## Local Helm chart checks

The [chart guide](../charts/webcappi/README.md) documents local installation,
external Redis, prepared bundles, optional Ingress and HTTPRoute. OCI chart
publication remains a later phase.

Use the Dockerfile's Go toolchain, Docker, and Python with PyYAML **6.0.2**.
Create a virtual environment and install the Python dependency, then install the
checksummed tools in the ignored `bin/chart-tools/` directory:

```sh
python3 -m venv bin/chart-python
. bin/chart-python/bin/activate
python3 -m pip install PyYAML==6.0.2
make chart-tools
GOTOOLCHAIN=go1.26.10 make chart-check
GOTOOLCHAIN=go1.26.10 make chart-package
```

An existing uv installation can instead create an environment under `bin/`:
`uv venv --python /usr/bin/python3 bin/chart-python`, followed by
`uv pip install --python bin/chart-python/bin/python PyYAML==6.0.2`.
Add its `bin/` directory to PATH for Make's Python commands. Tool installation
needs network access; the chart check uses local pinned standalone Kubernetes
schemas after installation. `chart-tools` verifies SHA-256 values embedded in
`scripts/chart-tools.py` before extracting individual executables. It supports
Linux amd64 and arm64 and does not modify the active kubeconfig.

The fixed validation stack is Helm **4.3.0**, kind **0.33.0**, Kubernetes/kubectl
**1.36.4**, kubeconform **0.7.0**, and Kubernetes JSON schemas from revision
`f7d855e49c34b039c9ef2196444910275e31e09e`. The kind node is pinned to the
multi-platform index `sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed`.
Fixture images reuse the digest-pinned Python **3.13-alpine** and Redis
**8.2.2-alpine** constants in `scripts/smoke_support.py`. The setup script pins
binary/archive hashes for both supported host architectures. Network acceptance
adds the pinned controllers and Gateway API fixture described below.

`chart-check` runs strict Helm lint, valid/invalid rendering cases, strict
Kubernetes schemas with no missing-schema skips, and the application's actual
`config.Load` against every rendered JSON configuration. It checks zero-value
capacities, bounds, JSON escaping, sandbox selection, credentials/TLS, replica
strategies, digest selection, profile paths/counts/config size, checksums and
release isolation, Ingress/HTTPRoute paths, TLS references, API capabilities,
timeout ordering and Service port forwarding.
It also exports `artifacts/charts/webcappi-<version>.tgz` and compares all
packaged files to the source (parsed metadata for Helm-normalized Chart.yaml).
`chart-package` performs the same validation before packaging.

Run runtime acceptance on a native Linux host. Select the sandbox mode
explicitly when opting out:

```sh
GOTOOLCHAIN=go1.26.10 make image IMAGE=webcappi:helm-check
make helm-smoke IMAGE=webcappi:helm-check HELM_SMOKE_SANDBOX=disabled
```

The default is `HELM_SMOKE_SANDBOX=enabled`. Run that mode on hosts whose
node/runtime policy permits Chromium's sandbox. The helper accepts only
`enabled` and `disabled` (`--sandbox` when invoking the script directly); it
does not retry in another mode after a failure.

The smoke helper requires an explicit local image tag/digest matching the native
host architecture. It creates a uniquely named kind cluster, kubeconfig and
Helm configuration/cache directories, independently of operator Helm/Kubernetes
overrides. It uses kind's default shared Docker network and ignores an inherited
`KIND_EXPERIMENTAL_DOCKER_NETWORK` override. The helper does not create or delete
Docker networks directly; the shared network remains available after cluster
deletion. It loads the image, creates synthetic fixture
resources, and exercises Service-based authenticated captures, probes, two
releases in one namespace, memory config changes/restart, authenticated external
Redis, scaling, cross-Pod metadata reuse, outage readiness, automatic and
prepared profiles, image/bundle and generation changes, rollback and uninstall.
Prepared bundles are generated using the selected sandbox mode and built from
the local application image. Since `init-profile` does not accept a server
configuration, disabled-mode tests use a temporary browser wrapper that adds
`--no-sandbox` only while preparing the disposable fixture. Enabled-mode tests
use the real browser directly. Hidden files, ownership, modes and read-only
immutable bundle contents are checked. Test image aliases, the cluster and temporary
files are cleaned on success, failure and SIGINT/SIGTERM. The helper never uses
a shared production Redis or cluster.

Diagnostics and screenshots are written to `artifacts/helm-enabled/` or
`artifacts/helm-disabled/` by default. `WEBCAPPI_ARTIFACTS_DIR` changes the parent
directory; `--artifacts` selects an exact directory. CI places the mode-specific
directory under its existing browser artifact directory. `result.json` records
the selected sandbox mode, native image platform and success only after
owned-resource cleanup; failures retain Pod logs and resource/event
snapshots without exporting Secret contents. Reuse a clean artifact directory
for each run to avoid mixing evidence from different executions.

The chart defaults to `browser.noSandbox=false`; operators can explicitly set it
to `true` to disable Chromium's sandbox. Both modes are checked statically,
including the generated JSON, config checksum and unchanged container security
settings. See the chart guide for installation and upgrade examples. There is
no automatic opt-out after startup failure. Dedicated existing seccomp/AppArmor
profile references and scoped kind preparation are described in [the node policy guide](../deploy/sandbox/README.md).

The verification workflow adds chart checks and native amd64/arm64 Helm
smoke runs to the existing browser matrix. CI runs both
`HELM_SMOKE_SANDBOX=enabled` and the explicit `disabled` regression, with
mode-specific results and diagnostics. Enabled tests prepare scoped node
policies and record Chromium's active sandbox status. The native jobs also
inject setup failure and SIGTERM after policy load to qualify cleanup.
Workflow syntax validation alone does not establish runner compatibility;
executed results are recorded below.

### Helm chart verification

Executed on **2026-10-25 (Asia/Tokyo)** in a working tree based on
`553826de6f90e9c871be7b75060cbea946e5fa0a`. Go **1.26.10**, full Chrome for
Testing **156.0.8078.47**, Docker client/server **29.9.1**, Helm **4.3.0**, kind
**0.33.0**, Kubernetes/kubectl **1.36.4**, kubeconform **0.7.0**, Ruff
**0.16.8** and Actionlint **v1.7.12** were used. Static chart validation used
PyYAML **6.0.2** in an isolated CPython **3.13.16** environment; earlier local
iterations used system Python **3.12.3** and PyYAML **6.0.1**.

| Scope | Result |
| --- | --- |
| Chart and package | `make chart-check` passed: 18 valid configurations, 36 rejected configurations, 54 Kubernetes resources validated without skips, strict Go JSON loading and package content comparison. |
| Python and workflow checks | All 42 Python tests, `make format-check lint` and Actionlint passed. The unit tests include owned-resource cleanup, asynchronous Pod deletion and image-inspection failures. |
| Go and existing runtime | `make test` with race detection and `go vet ./...` passed; unchanged Go tests reused cached results. `make image IMAGE=webcappi:helm-phase1`, `make browser-test` and `python3 scripts/container-smoke.py` passed on linux/amd64. Browser tests exercised the existing CDP font and extension/profile suites. Those existing disposable suites retain their documented sandbox exception. |
| Helm production runtime | The owned linux/amd64 kind cluster reached the memory Deployment, but Chromium failed startup with `No usable sandbox!`; Helm install timed out without a Ready API Pod. Service captures, Redis sharing, prepared bundle and upgrade/rollback acceptance in this suite remain **unverified**. The chart's sandbox and security constraints were preserved. |
| Cleanup | Failed-install diagnostics were retained and the cluster/kubeconfig removed. Before the default-network change, a separate one-off real SIGTERM probe against the maintained Cluster helper confirmed dedicated cluster, network and temporary-directory removal. The current helper retains kind's shared network. Maintained unit tests also cover creation failure, interruption, diagnostics failure, ambient configuration isolation and cleanup errors. |
| Other platforms and CI | Native arm64 chart runtime and GitHub-hosted workflow execution were not run. The node index contains amd64 and arm64 manifests; this is build/distribution evidence, not application runtime qualification. No multi-platform application rebuild or release publication was required for these chart/tooling changes. |

Chart results are in `artifacts/helm-phase1-chart-check.log` and
`artifacts/charts/`. The failed sandbox deployment is recorded under
`artifacts/helm-smoke/`, including the API log and `result.json`. Browser/REST
outputs are under `artifacts/helm-phase1-browser/`, with logs at
`artifacts/helm-phase1-browser.log` and `artifacts/helm-phase1-container.log`.
The one-off interruption result is under `artifacts/helm-phase1-interruption/`.
Images and evidence were retained; owned test containers, networks and kubeconfigs
were removed. These results do not meet the phase's production-runtime completion
condition; rerun `make helm-smoke` on suitable native amd64 and arm64 hosts.

### Sandbox selection verification

Verified on **2026-10-25 (Asia/Tokyo)** in the working tree based on
`553826de6f90e9c871be7b75060cbea946e5fa0a`. This change exposes the existing
application `no_sandbox` setting as the boolean Helm value `browser.noSandbox`,
with default `false`. The application/browser sources and runtime image are
unchanged; the Docker probe reused `webcappi:helm-phase1` from the earlier build.

- `make chart-check` passed with Helm **4.3.0**, Kubernetes schemas **1.36.4**,
  PyYAML **6.0.2** and Go **1.26.10**: 21 valid configurations, 38 rejected
  configurations and 63 Kubernetes resources, with no schema skips. Both sandbox
  modes passed the application's strict JSON loader. Explicit `false` matched
  the default Deployment, while `true` changed its config checksum without
  changing the Pod's security settings. String and numeric opt-outs were rejected.
- All 42 Python tests and `make format-check lint` passed with Ruff **0.16.8**
  and Staticcheck **v0.8.1**.
- A one-off native linux/amd64 Docker probe used the chart-rendered
  `no_sandbox=true` config with bearer authentication. It passed readiness,
  unauthorized-request rejection and an authenticated HTML-to-PNG capture using
  UID/GID 10001, a read-only root, all capabilities dropped, no-new-privileges,
  and default Docker seccomp/AppArmor. Its container and temporary files were
  removed. This was a configuration/integration probe, not a Helm installation.

Static results are in `artifacts/helm-sandbox-toggle-check.log`; the capture and
Docker result are under `artifacts/helm-sandbox-toggle/`. Browser suites and
multi-platform image builds were not repeated for this chart-only mapping.
Sandbox-enabled Kubernetes/Actions acceptance, native arm64 runtime and custom
profile provisioning remain unverified here. Disabled-mode success does not
qualify sandbox-enabled operation.

### Disabled-mode Helm lifecycle verification

Verified on **2026-10-25 (Asia/Tokyo)** in the working tree based on
`553826de6f90e9c871be7b75060cbea946e5fa0a`, using the versions recorded above.
The unchanged application/browser sources reused the previously built
`webcappi:helm-phase1` image. No host Docker, AppArmor or sysctl settings were
changed.

- `make helm-smoke IMAGE=webcappi:helm-phase1 HELM_SMOKE_SANDBOX=disabled`
  passed on native **linux/amd64** with Kubernetes **1.36.4**. It exercised
  memory installation, two releases, authentication and probes, config changes
  and restart, authenticated external Redis, scaling, cross-Pod metadata reuse,
  Redis outage/recovery, automatic and prepared profiles, immutable bundles,
  image/bundle and generation updates, Service port changes, rollback,
  uninstall and preservation of external resources. The installed config was
  checked against the requested sandbox mode after install and rollback.
- All **49 Python tests**, `make format-check lint` and Actionlint passed.
  The additional tests cover the enabled default, explicit opt-out, separate
  artifact paths, success/failure mode records, profile preparation and cleanup
  without fallback.
- Owned containers, test image aliases and temporary kubeconfigs were removed.
  The shared default `kind` Docker network was retained with no attached
  containers.
- The workflow now selects disabled-mode tests on both native architectures.
  GitHub-hosted execution and native **arm64** runtime were not run here.
  Sandbox-enabled kind/Actions runtime and custom profile provisioning remain
  unverified. This result does not establish release readiness on both native
  architectures or qualify sandbox-enabled operation.

The full log is `artifacts/helm-lifecycle-amd64-r1.log`. Screenshots,
diagnostics and the successful `result.json` with `sandbox=false`,
`sandbox_mode=disabled` and `platform=linux/amd64` are under
`artifacts/helm-lifecycle-amd64-r1/helm-disabled/`. Earlier enabled-mode failure
evidence remains separate at the locations recorded above.


## Phase 1a sandbox runtime

The [node policy guide](../deploy/sandbox/README.md) describes the independently
authored MIT policies, required filesystem layout, host preparation and cleanup.
Helm references operator-provisioned profiles; it does not install node policy
or change runtime defaults. Localhost profiles apply to the API container, while
bundle init containers retain the Pod defaults. The API runs as UID/GID 10001
with no privilege escalation, all capabilities dropped and a read-only root.

The seccomp policy rejects unlisted syscalls, restricts namespace creation with
a full-width clone mask, returns ENOSYS for clone3 fallback, and bounds process,
ioctl and socket operations. AppArmor enforces named executable/input paths and
scratch writes without scratch execution. Its namespace-local capability
mediation supports Chrome's identity mapping and chroot; the initial container
capability sets remain empty. The guide explains these permissions and limits.

On **2026-10-26 (Asia/Tokyo)**,
[Actions run 40255863961](https://github.com/dayflower/webcappi/actions/runs/40255863961)
validated implementation commit `1c65062a207179a11d813743e72238225ef056f2`
with Go **1.26.10** and full Chrome for Testing **156.0.8078.47** on native
Linux amd64 and arm64. Runtime evidence and build verification are separate.

| Scope | Result |
| --- | --- |
| Go, static analysis and chart | Race tests, vet, formatting/static checks, all 58 Python tests and chart/package validation passed: 26 valid configurations, 63 rejected configurations and 78 Kubernetes resources. |
| Browser and API | Browser, REST/profile and Redis suites passed on both native platforms. Tests under the application policies also verified CDP font selection, multilingual/emoji fonts, installed extension state and welcome tabs, complete profile clones, file/loopback HTML, selectors, images, resizing and timeout/failure cleanup. |
| Active sandbox | Both enabled reports show Namespace/PID/network/Seccomp-BPF/TSYNC in Chrome's sandbox page, no --no-sandbox argument, enforced AppArmor, initial CapEff=0 and NoNewPrivs=1. Six stable renderers per platform had two filters against one outer/browser filter, with nested NSpid. Protected namespace symlink read errors remain explicit. |
| Helm lifecycle | Both sandbox-enabled and explicitly disabled modes passed automatic/prepared profiles, immutable template checks, authenticated plain/TLS Redis with projected custom CA, certificate-hostname rejection, cross-Pod metadata, restart/outage recovery, upgrades, generation changes, rollback and uninstall. |
| Negative permissions | Probes rejected reserved/high namespace bits, raw sockets, ptrace/BPF/keyring/process-memory/io_uring, initial-namespace SYS_ADMIN/root elevation and outside-canary/scratch execution. Matching baselines proved canary access and execution on all three diagnostic scratch mounts before custom-policy rejection. Four missing/incompatible policy cases failed without an unsandboxed retry. |
| Cleanup | Normal completion, setup failure after profile loading and real SIGTERM removed the owned profiles/nodes on both platforms. Local native amd64 validation independently passed both lifecycle modes and cleanup scenarios. |
| Build | The separate build job passed make binaries, make image-multi and ELF/OCI verification for linux/amd64 and linux/arm64. |

Both native runners used Ubuntu **24.04.5 LTS**, kernel
**6.17.0-1022-azure**, Docker **28.0.4**, kind **0.33.0**, Kubernetes
**1.36.4**, node containerd **2.3.4**, runc **1.4.3** and libseccomp **2.6.0**.
AppArmor was active, `kernel.unprivileged_userns_clone=1`,
`kernel.apparmor_restrict_unprivileged_userns=1` and user namespace limits were
nonzero. The checksummed parser was **4.0.1** with explicit ABI **4.0**.

The smoke harness records image identities, policy hashes, actual browser
arguments, Chrome's sandbox status, per-role process/filter observations and
profile removal. Diagnostic binaries are installed at immutable image paths.
Headed extension fixture preparation has a separate explicit sandbox-disabled
record; the subsequent confined extension tests supply enabled-mode evidence.
See the node policy guide for artifact names and repeatable validation commands.

Qualification covers these host/runtime combinations and the supported layout.
Hosts without AppArmor, SELinux, other kernel/CPU/runtime combinations and
arbitrary configured paths remain unqualified. Anonymous executable memory/memfd
and source-derived syscall alternatives remain coverage allowances rather than
a proven minimal set. Policy or browser changes require fresh native validation.


## Phase 2 network acceptance

`make helm-network-smoke IMAGE=<local-image>` runs
`scripts/helm-smoke.py --network-only --sandbox enabled`. It creates its own
kind cluster using the qualified Phase 1a node setup and cleanup. It never
uses an ambient kubeconfig or installs controllers into an existing cluster.
`make helm-smoke` retains both enabled and explicitly disabled lifecycle
modes. CI runs all three suites on native amd64 and arm64; disabled-mode
success does not qualify the network suite.

The fixture installs Traefik **3.7.16** for Ingress and Envoy Gateway **1.9.3**
with Envoy **1.39.5** for HTTPRoute. Image indexes are fixed by digest in
`scripts/helm_network.py` for both architectures. Kubernetes pulls these pinned
indexes directly; Docker-to-kind import of digest-only multi-platform indexes
can fail when Docker exports an index without all architecture blobs. The
Envoy Gateway install YAML is downloaded by `make chart-tools` and verified by
SHA-256 before static schema extraction or cluster application. It bundles
Gateway API **1.6.1** CRDs (including its experimental channel); the chart emits
only the stable `gateway.networking.k8s.io/v1` HTTPRoute. The application chart
installs none of the fixture's controller/CRD/Gateway resources.

Traefik's file-provider transport sets a 40-second response-header deadline;
HTTP/HTTPS entrypoints set 45-second read/write deadlines. The selected
buffering middleware allows 2 MiB without retries. Envoy Gateway routes set
45-second request and 40-second backend deadlines. Short-deadline probes
independently verify each HTTPRoute timeout and count API capture starts to
reject automatic retries. Traefik's pinned release lacks HTTPRoute timeout
implementation, so it is used only for Ingress.

The API remains sandbox-enabled with UID/GID 10001, dropped capabilities,
read-only root and operator-profile references. The suite records active
Chromium sandbox evidence, serves a private Kubernetes Service DNS target,
and verifies each routing method independently: unauthenticated/incorrect-token
rejection, authenticated PNG captures, real TLS/SNI and hostname validation,
a delayed HTML POST larger than 1 MiB, rejection above the configured 2 MiB
body limit, Service port 9090, routing updates and Helm rollback. HTTPRoute
parents attach to an existing Gateway in a separate namespace through
`allowedRoutes`. Accepted and ResolvedRefs must both be True for every desired
parent/listener, the expected controller and the current generation; stale
status is rejected. Request retries/redirects are also covered by a local
HTTP-server unit test.

Artifacts default to `artifacts/helm-network/`; `WEBCAPPI_ARTIFACTS_DIR` selects
the parent or `--artifacts` an exact directory. Screenshots, route status,
controller logs and namespace-wide resource/event snapshots are retained.
`network.json` records individual passed cases; the outer `result.json` records
success only after the cluster, scoped policies, test image aliases and temporary
kubeconfig are removed. Secrets are excluded from diagnostics. Failed runs
retain their error and partial evidence. The shared kind Docker network remains.

The [chart guide](../charts/webcappi/README.md#ingress-and-gateway-api) provides
operator-managed TLS, separate-namespace Gateway, transport/body policy,
capability and update/rollback examples. The tested examples publish root paths;
URL-prefix rewrites and other controllers require separate qualification.

### Local Phase 2 execution

On **2026-10-26 (Asia/Tokyo)**, implementation commit
`73818abac5d5e2bd46364e8896c5c81e9afe9580` passed
`make helm-network-smoke IMAGE=webcappi:helm-cleanroom` on native **linux/amd64**.
It reused the unchanged application/browser image, source image ID
`sha256:88168024c48aaf60512f7097ea59f41f5162494426467e71c12b565ad0702e99`;
the harness built its own disposable diagnostic image. The host used Ubuntu
**24.04.5 LTS**, kernel **6.8.0-147-generic**, Docker **29.9.1** and the pinned
kind **0.33.0** / Kubernetes **1.36.4** stack, with containerd **2.3.4** inside
the node. Chromium's sandbox was enabled with the scoped Phase 1a policies.

- Ingress and HTTPRoute independently passed HTTP/TLS captures, token rejection,
  private Service DNS targets, 1,100,075-byte delayed HTML POSTs, rejection above
  2 MiB, route updates, rollback, uninstall and external-resource preservation.
- HTTPRoute reported fresh Accepted/ResolvedRefs for the requested parent;
  independently configured backend and request deadlines returned 504 after
  **6.006 s** and **6.008 s**, respectively. Each probe started exactly one
  capture and cancellation removed its capture scratch directories.
- `result.json` reports success after owned cluster/image/policy cleanup;
  AppArmor removal is recorded. The shared kind network was retained.
- `make chart-check` passed **33** valid configurations, **95** rejected
  configurations and **107** resources with no schema skips; strict Go JSON
  loading and package comparison passed. All **63** Python tests, Go race
  tests, vet, formatting/static analysis and Actionlint passed.

Evidence is under `artifacts/helm-phase2-r5/helm-network/`, including
`network.json`, `result.json`, `chromium-sandbox.json`, route status,
port-forward/controller logs and eight PNG captures. Earlier failed fixture
iterations remain separate under `artifacts/helm-phase2-r2/` through `-r4/`
and `artifacts/helm-phase2-local/`. Native arm64 was not
run locally. Native amd64/arm64 Actions results for the final PR are linked
from [PR #5 checks](https://github.com/dayflower/webcappi/pull/5/checks),
including the existing browser suites and both sandbox lifecycle modes.
Application/browser sources and build recipes were unchanged; no multi-platform
application rebuild was performed locally for Phase 2.

## Phase 3 distribution validation

On **2026-10-26 (Asia/Tokyo)**, the working tree based on
`0fd116ea97babe4d81dba074aa45873810c53010` passed local native **linux/amd64**
acceptance of a packaged `0.1.2` chart with the existing local application image
`webcappi:helm-cleanroom`, ID
`sha256:88168024c48aaf60512f7097ea59f41f5162494426467e71c12b565ad0702e99`.
This is the local-image archive variant, not a published release chart. The
application/browser sources, pinned policies and build recipes were unchanged.

- Enabled and explicitly disabled lifecycles both passed authentication/probes,
  standard default-image captures, automatic/prepared profiles, active sandbox
  selection, immutable templates, plain/TLS Redis, CA/hostname validation,
  shared metadata, restart/outage recovery, image/bundle/generation changes,
  Service port changes, rollback, uninstall and cleanup. Enabled mode also
  passed browser/policy/negative-permission checks and missing-policy failure
  without an unsandboxed retry.
- The enabled network suite passed standard default-image captures and both
  Ingress/HTTPRoute HTTP/TLS/authentication paths, private Service DNS captures,
  large delayed POSTs, body limits, independent deadlines, route updates,
  rollback and external-resource preservation. All three `result.json` files
  record success after scoped cluster/image/policy cleanup.
- Static validation passed 33 valid configurations, 95 rejected configurations
  and 107 Kubernetes resources without schema skips. Go **1.26.10** race tests,
  vet, Staticcheck **v0.8.1**, Ruff **0.16.8**, Actionlint **v1.7.12** and all
  **81** Python tests passed. Final static/Python checks used PyYAML **6.0.2**;
  the local runtime harness used system PyYAML **6.0.1**. CI pins **6.0.2**.
- A disposable loopback OCI Registry **3.0.0** probe performed real Helm push
  and anonymous pull, compared every file and mode, and matched the downloaded
  archive SHA-256 with the OCI chart layer. This was not GHCR publication.
- An initial archive run exposed duplicate fixture preparation replacing the
  Redis CA while Redis retained its startup certificate. The harness now
  prepares shared fixtures once per cluster, with a regression test. Its failed
  evidence remains separate from the successful rerun.

Successful runtime evidence is under `artifacts/helm-phase3-r2/`, with the full
log at `artifacts/helm-phase3-r2.log`; initial failed evidence is under
`artifacts/helm-phase3/`. Fixed-dependency static/Python logs are
`artifacts/helm-phase3-{chart,python,lint,go}.log`. The actual CI packaging helper
also passed locally under `artifacts/helm-phase3-ci-package/`. OCI probe evidence
is under `artifacts/helm-phase3-registry/`; its container was removed.

Native amd64/arm64 PR CI now builds the application and tests actual archives
with the chart defaults before running the existing diagnostic suites. The
release workflow separately requires the final image-digest-pinned archive and
the anonymously downloaded GHCR chart on both native platforms. Native arm64
was not run locally. Real GHCR chart publication, first-package visibility setup,
anonymous GHCR runtime acceptance and the chart-enabled GitHub Release remain
unverified until the first post-merge release. No release tag or real GHCR/GitHub
Release was created or changed by local validation. The public-distribution
completion condition remains pending that evidence.
