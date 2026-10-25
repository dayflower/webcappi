# Development guide

The [README](../README.md) covers setup, capture requests and operations.
This guide describes implementation boundaries, regression checks and the scope
of recorded validation. Browser choices, extension experiments and font
measurements are in [BROWSER.md](BROWSER.md).

## Toolchain and local build

Use the Go toolchain pinned in [Dockerfile](../Dockerfile): **Go 1.26.10**.
The module minimum is Go 1.25.0. The browser is full Chrome for Testing
**156.0.8078.47** on both Linux amd64 and arm64; chromedp is **v0.21.0**.
Docker with Buildx is needed for image/browser checks, and Python 3 for smoke
tests. Host Go compilation is needed by the browser-test script even when the
application runs entirely in containers.

```sh
GOTOOLCHAIN=go1.26.10 make build
```

This writes `bin/webcappi`. Native captures still require the pinned browser,
shared libraries, fonts and a suitable sandbox policy; see the README for
runtime configuration.

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
an HTTP server. `version` prints the supported browser version.

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
an HTTP request. Capture errors retain `capture HTTP <status>: <JSON>`, JSON
HTML escaping and the final newline. `main` logs errors to stderr and exits 1;
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

The existing LRU stores only validation metadata. Do not add PNG retention or
coalesce independent captures. Keys include URL, viewport/output dimensions,
profile name, request selector and delay, plus fixed renderer attributes.
Omitted and explicit viewport-sized outputs share a key.

Allocate the metadata request-order number after admission and POST body work,
before conditional lookup or a service call. Later completion of an older
request must not replace a newer stored result. A newer `no-store` result removes
the previous entry. Source 304 processing merges policy/validators, clears old
Date/Age accounting and retains the PNG ETag without changing the service's
`Image` or its source headers. Keep size/entry enforcement in the existing cache.

Fresh matching metadata may return 304 without a browser and without renewing
its validation time. Expired metadata needs source validation or recapture
before deciding 304. Unconditional GET and all HTML POST requests remain
independent captures. See the README for client-visible freshness semantics and
the limits of treating main-document validators as evidence about a screenshot.

## Regression checks

Run checks relevant to the change with the pinned toolchain:

```sh
GOTOOLCHAIN=go1.26.10 GOFLAGS=-count=1 make test
GOTOOLCHAIN=go1.26.10 go vet ./...
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

For CI, prefer native amd64/arm64 runners in a matrix with the same Go, image,
browser and REST checks. Release publication is separate from local validation;
an installed CI workflow and registry publication are not supplied by these
Make targets.

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
the README describes staging and deploying profile updates.

## Recorded validation scope

Last full local regression: **2026-10-25 (Asia/Tokyo)** against implementation
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
