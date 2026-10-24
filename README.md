# webcappi

A Go REST server that captures a URL as a viewport-sized PNG using a fresh
Chromium process and a disposable copy of a prepared browser profile. The first
release targets **Linux amd64 and arm64**, one replica and an in-process bounded
PNG cache.
Private addresses and cluster Service DNS names are intentionally supported.

## Browser selection and verified startup behavior

The image contains the complete **Chrome for Testing 156.0.8078.47** browser,
based on Chromium, and Noto fonts for multilingual text, CJK and color emoji.
The image explicitly installs `fonts-noto-core`, `fonts-noto-cjk`,
`fonts-noto-extra`, `fonts-noto-color-emoji`, `fonts-noto-mono`,
`fonts-roboto-unhinted`, and `fonts-liberation`.
Core supplies script-specific families, extra supplies additional weights and
widths, and Liberation provides metric-compatible alternatives for common web
fonts. Fontconfig uses the Debian/package defaults without custom family
overrides. This does not guarantee coverage of every Unicode character or
language; qualify representative text in Chromium. The binary is downloaded only at
image build time. Startup rejects another browser brand/version. The Go build
uses Go 1.26.10 and chromedp v0.21.0; go.mod records the minimum Go version.
Both architectures use this same pinned browser version: `linux64` for amd64
and `linux-arm64` for arm64. Linux ARM64 downloads are provided by
[Chrome for Testing](https://github.com/GoogleChromeLabs/chrome-for-testing#supported-platforms).

`fonts-noto-mono` supplies normal-width Noto Sans Mono Regular/Bold, which are
separate from the other weights/widths in `fonts-noto-extra`. Roboto is installed
via its concrete `fonts-roboto-unhinted` package.

Fontconfig resolution and Chromium's actual rendering are different checks.
The following were measured in the application image using `fc-match` and CDP
`CSS.getPlatformFontsForNode`. The browser sample uses Latin text, `lang=en`,
normal weight, one CSS family at a time, a fresh profile and no web fonts:

| Requested family | Fontconfig match | Chromium rendered family |
| --- | --- | --- |
| Arial / Times New Roman / Courier New | Liberation Sans / Serif / Mono | Liberation Sans / Serif / Mono |
| Arial Narrow | Liberation Sans Narrow | Liberation Serif (fallback) |
| Helvetica / Times / Courier | Liberation Sans / Serif / Mono | Liberation Sans / Serif / Mono |
| Roboto | Roboto | Roboto |
| Noto Sans Mono / monospace | Noto Sans Mono Regular | Noto Sans Mono Regular |

Do not assume an OS alias proves the browser will select a particular font.
Browser profile settings, CSS fallback lists, text language,
character coverage and page web fonts also influence actual Chromium rendering.
Font additions can affect line wrapping and capture layout even without changing
CSS. Requalify captures after updating the image's font set.

`fc-match` returns the best available match, while Skia additionally checks
whether a named-family substitution is acceptable before using it. Its
hardcoded equivalence groups include Arial/Liberation Sans, Times New
Roman/Liberation Serif and Courier New/Liberation Mono. Blink can also retry
using built-in alternate names such as Helvetica/Arial, Times/Times New Roman
and Courier/Courier New. Sources for the pinned browser:
[Skia substitution checks](https://skia.googlesource.com/skia/+/886e42fda11610a56dd1b22e8697e27f9d1e34f2/src/ports/SkFontConfigInterface_direct.cpp),
[Blink alternate names](https://chromium.googlesource.com/chromium/src/+/156.0.8078.47/third_party/blink/renderer/platform/fonts/alternate_font_family.h),
[Blink alternate-name retry](https://chromium.googlesource.com/chromium/src/+/156.0.8078.47/third_party/blink/renderer/platform/fonts/font_platform_data_cache.cc).

Use the full browser, not `chrome-headless-shell`. Unified headless shares the
normal browser implementation and supports extensions. Ordinary branded Chrome
has removed `--load-extension`; Chromium and Chrome for Testing retain it.
Sources: [headless modes](https://developer.chrome.com/docs/automation-and-testing/headless),
[extension testing](https://developer.chrome.com/docs/extensions/how-to/test/end-to-end-testing),
[Chrome extension flag announcement](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/my-groups),
[version availability](https://googlechromelabs.github.io/chrome-for-testing/).

The allocator explicitly overrides chromedp's defaults:

| Setting | Value and reason |
| --- | --- |
| `headless` | `new`, the full browser's unified headless mode |
| `disable-extensions` | `false`, removing the default disabling flag |
| `user-data-dir` | A unique writable clone of the complete template |
| `profile-directory` | The configured individual profile, usually `Default` |
| `no-first-run`, `no-default-browser-check` | Browser-level first-run suppression |
| `password-store` | `basic`, consistently during preparation and capture |
| `disable-dev-shm-usage` | `false`; provision `/dev/shm` explicitly |
| `force-device-scale-factor`, CDP device scale | `1` |
| `no-sandbox` | Explicit boolean from configuration, default `false` |

The implementation was checked against the pinned
[allocator source](https://github.com/chromedp/chromedp/blob/v0.21.0/allocate.go)
and its context/cancellation implementation, rather than assuming older API
behavior. This release uses chromedp's local pipe transport, without exposing a
remote debugging TCP port. Linux process groups and cancellation terminate
Chromium descendants; cleanup also checks the unique user-data-dir in `/proc`.

**Do not use `--load-extension` for each capture.** Our local fixture demonstrated
that this command-line installation repeats the initial installation event on
cloned profiles. CDP `Extensions.loadUnpacked` also did not provide the desired
restart behavior in this browser. Instead, install through the visible browser's
**Developer mode → Load unpacked** flow once and let Chromium reload its saved
registration at startup. Runtime does not pass an installation flag or call an
installation API. The real-browser test exercises the native folder picker.

## Quick start

Build and run automated verification without a personal profile or external site:

```sh
go version
make test
make image
make browser-test
python3 scripts/container-smoke.py
```

Use a Go 1.25+ toolchain; Go 1.26.10 is the tested build toolchain. On an older Go
installation, `GOTOOLCHAIN=go1.26.10 make test` selects the tested toolchain.
Docker is required for the image/browser tests; Python 3 is required for the REST
smoke test. Browser tests use only synthetic state, an ephemeral loopback page,
Xvfb and a minimal Manifest V3 extension. They explicitly disable the browser
sandbox inside disposable test containers on hosts whose default container
policy does not support it. This is separate from production defaults.

The tests export `artifacts/extension.png`, `artifacts/api.png`, and a synthetic
User Data Directory at `artifacts/user-data`. Remove the old synthetic directory
before exporting another one; the script will not overwrite it. The fixture
extension is installed at `/src/testdata/extension` in the test container, so its
synthetic profile is usable only with that same path. It is not the production
bundle prepared by the following workflow.

To test a specific architecture, set `PLATFORM=linux/amd64` or
`PLATFORM=linux/arm64`. The browser test compiles its test binary for that
platform and uses the corresponding browser image. Set
`WEBCAPPI_ARTIFACTS_DIR=artifacts/arm64` (or `artifacts/amd64`) to keep synthetic
profiles and images separate. The REST smoke test accepts the same variables
and `WEBCAPPI_IMAGE` to select the application image. It checks that an explicit
platform agrees with that image. Running foreign-architecture containers needs
working runtime emulation; native runners are preferable for browser tests.

The screenshot fixture fits an 800 x 480 viewport and contains seven writing
system groups plus symbols: Latin (including accented text and Vietnamese),
CJK (Japanese, simplified/traditional Chinese and Korean), RTL (Arabic and
Hebrew mixed with Latin text and digits), Thai, Devanagari (Hindi), Cyrillic
(Russian), and Greek, plus currency signs, arrows, mathematical symbols and
color emoji. It uses generic `sans-serif` with per-sample `lang` and RTL `dir`
attributes, so the browser selects the installed system fonts.
`TestMultilingualRenderedFonts` loads the same local page, checks that it fits
the viewport, and inspects actual rendered font families through CDP.
The PNG artifacts are available for visual inspection; glyph shapes are not
compared against reference images.

## Prepare and import a production profile

The **User Data Directory** is the parent of individual profiles. Copy all of it,
including `Local State`, `Default/Preferences`, extension storage and databases.
Passing `Default/` itself as `user_data_dir` is incorrect. See Chromium's
[directory documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md).

Keep extension code and browser data separate:

```text
bundles/reader/
  bundle.json
  extensions/verification/manifest.json
  extensions/verification/...
  user-data/Local State
  user-data/Default/Preferences
  user-data/Default/Local Extension Settings/...
```

`bundle.json` is the profile descriptor shown in `config.example.json`. The
server checks that it exactly matches the configured descriptor, including the
revision, browser version, individual profile, extension paths and readiness
signal. Do not edit a mounted bundle in place. Treat it as immutable.

On a **Linux X11 desktop**, launch the exact production image and browser:

```sh
make image
bash scripts/prepare-profile.sh bundles/reader
```

For another architecture, select its built image explicitly, for example
`PLATFORM=linux/arm64 WEBCAPPI_IMAGE=webcappi:dev-arm64 bash scripts/prepare-profile.sh bundles/arm64/reader`.
Prepare and qualify profiles with the production architecture as well as the
same browser version, OS image and extension paths. Do not assume an amd64
template is qualified for arm64. Keep architecture-specific bundles in separate
staging directories and give each a distinct revision. A multi-platform bundle
image must package the matching prepared data for each architecture; rebuilding
one template twice does not establish profile portability.

The helper imports a scoped X11 cookie with mode 0600 and runs Chromium as UID/GID
10001. It does not change `xhost` access control. It opens `chrome://extensions`
without headless mode, using an isolated User Data Directory. In that browser:

1. Enable Developer mode and choose **Load unpacked**.
2. Select `/bundles/reader/extensions/verification` for the sample extension.
3. For real extensions, configure their permissions/settings, finish onboarding
   and logins if needed, and verify operation on representative target pages.
4. Close welcome/setup tabs. Use the browser's **Exit** command so databases flush.
5. The helper exports the normally closed User Data Directory and `bundle.json`.

The helper defaults to the verification extension. Adapt its extension source,
fixed container mount path and descriptor together for other extensions. Multiple
extension directories are supported in `extensions`. The server verifies each
extension's manifest public key, saved unpacked registration, enabled state and
exact absolute path. Startup opens a browser on a clone and checks the extensions
actually enabled by Chromium, before the API starts listening.

Preserve the manifest's public `key` field to preserve extension ID across
machines; keep the original public key for an existing extension, rather than
generating another. No private signing key is needed in the deployment. Without a
key, unpacked extension IDs can depend on the absolute path. webcappi requires a
key and also requires **the same absolute loading path** during preparation and
runtime. See [consistent extension IDs](https://developer.chrome.com/docs/extensions/reference/manifest/key).

Do not assume a Windows/macOS profile, another Chrome brand/version, a personal
profile or encrypted credentials can be moved into this image. Even with the same
Linux image, extension/native messaging dependencies, encrypted storage, login
expiry, machine identity and browser migrations require extension-specific
verification. On macOS/Windows or a headless workstation, use a Linux VM or a
remote Linux desktop with this same image. Wayland/X11 forwarding and the manual
desktop helper were not tested in this headless development environment; the
same image's headed browser flow was tested under Xvfb.

Persisting an installed profile preserves installation/settings state for the
verified fixture; **it cannot guarantee every extension suppresses onboarding**.
An extension may open tabs on every startup, respond to updates, consult remote
state, or require interactive UI/active-tab behavior. Qualify each bundle with
repeat launches. `no-first-run` suppresses browser prompts, not arbitrary
extension behavior. webcappi always creates its own capture target and brings
that target forward before the final asset/frame check, so another tab is never
mistaken for the requested page.

## Local CLI and API

For native Linux execution, use the same Chrome for Testing binary, dependencies
and fonts as the image, edit absolute paths in the configuration and descriptor,
and prepare the profile at those exact extension paths. The container is the
preferred way to keep these dependencies consistent.

```sh
make build
cp config.example.json config.json
# Edit config.json and the corresponding bundle.json for your prepared bundle.
./bin/webcappi check -config config.json
./bin/webcappi serve -config config.json
./bin/webcappi capture -config config.json -profile reader \
  -url http://page-service:8000/ -width 800 -height 480 -out capture.png
```

`check` validates configuration, bundle descriptors, browser version, scratch
access and a real browser launch on each profile clone. The CLI capture command
uses the same validation and rendering path as the HTTP server; its cache lasts
only for that CLI process. `version` prints the supported browser version.

```sh
curl --get http://localhost:8080/capture \
  --data-urlencode profile=reader \
  --data-urlencode url=http://page-service:8000/ \
  --data-urlencode width=800 --data-urlencode height=480 \
  --output capture.png
```

| GET endpoint | Behavior |
| --- | --- |
| `/capture` | Direct `image/png` response; no JSON image wrapper |
| `/healthz` | Process liveness; does not start Chromium |
| `/readyz` | Checks immutable bundle readability and writable scratch; 503 during shutdown |

`profile` and absolute HTTP(S) `url` are required. `width` defaults to 800 and
`height` to 480. Optional `ready_selector` waits for an element in the page;
profile `ready_selector` separately expresses an extension-specific signal. Both
must succeed when configured. Only GET is accepted; repeated and unknown query
parameters, credentials in URLs, unsupported schemes and excessive dimensions
are rejected. URL fragments remain part of the capture URL and cache key.

Errors are JSON with an `error` field and `Cache-Control: no-store`: 400 for input,
404 for an unknown endpoint, 405 for another method, 502 for browser/navigation,
HTTP 4xx/5xx from the target or asset failure, 503 for exhausted capacity,
cancellation or shutdown, and 504 for the capture deadline. Overload responses
include `Retry-After: 1`. Target URL errors are logged without logging the full
requested URL. Do not put credentials in query strings.

Navigation waits for the main document's load event. Next come the requested page
and extension selectors, `document.fonts.ready`, load/decode of `<img>` elements
intersecting the viewport, and two animation frames. Broken visible images fail
the capture. This does not infer application readiness, load all lazy/offscreen
images, inspect cross-origin frames, or independently wait for CSS backgrounds,
video, canvas, animations or future DOM changes. Use a page/extension readiness
contract for those cases. No network-idle or fixed-duration delay is used as a
capture readiness contract. All waits share the overall deadline.

## Configuration

JSON configuration rejects unknown top-level fields. All omitted limits take
these defaults. Durations are whole seconds; zero TTL or zero cache capacity
disables internal caching. Configuration changes require a process restart.

| Field | Default | Meaning |
| --- | --- | --- |
| `listen` | `:8080` | HTTP bind address |
| `browser_path` | `/opt/chrome/chrome` | Absolute, preinstalled browser path |
| `no_sandbox` | `false` | Explicit browser sandbox opt-out |
| `work_dir` | `/work` | Writable scratch, separate from templates/extensions |
| `concurrency` | 1 | Active browser captures (1–32) |
| `queue_size` | 8 | Additional distinct pending captures (0–1024) |
| `max_requests` | 64 | Concurrent HTTP capture handlers, including coalesced waiters |
| `capture_timeout_seconds` | 30 | Queue + profile copy + browser start + readiness + PNG (1–300) |
| `cache_ttl_seconds` | 60 | Fixed freshness lifetime from generation (0–86400) |
| `cache_max_bytes` | 67108864 | LRU PNG byte capacity |
| `cache_max_entries` | 128 | Additional cache metadata/entry bound |
| `max_width`, `max_height` | 4096 each | Maximum viewport dimensions |
| `max_pixels` | 8388608 | Additional viewport area bound |
| `max_profile_bytes` | 536870912 | Maximum template bytes copied per capture |
| `max_png_bytes` | 41943040 | Maximum returned/cached PNG size |
| `profiles` | required | Named immutable descriptors (1–128 profiles) |

Profile fields are `revision`, `browser_version`, `user_data_dir`,
`profile_directory`, `extensions`, and optional `ready_selector`.
`user_data_dir` must have a sibling `bundle.json`. Scratch and templates cannot
overlap; copies reject special files and symlinks except obsolete top-level
Chromium lock/endpoint files, which are excluded. Files in copies are independent
and writable even when original files are read-only. No hard links, shared work
profiles, template launches or writes back to the template are used.

Each distinct job has its own deadline and directory. Identical callers share a
job. Canceling one caller keeps the shared job alive for others; canceling its
last caller terminates it. Successful browsers close normally, then descendants
are reaped/terminated before scratch is removed. Cleanup has a separate short
grace period. SIGTERM stops admission/readiness, cancels active/queued captures,
waits for their cleanup and shuts down HTTP. SIGKILL/node loss cannot run cleanup;
the example's emptyDir is reclaimed with the pod.

## Cache semantics

The key contains URL, viewport, both readiness selectors, the complete profile
descriptor (including revision and browser version), scale factor and renderer
schema. Change `revision` whenever any extension, setting, template data or
rendering contract changes; changing bundle files without changing revision is
unsupported. Cache entries use an LRU bounded by PNG bytes and entry count;
expired entries are removed lazily on requests. Errors are not cached. Oversized
cache entries can still be returned, up to `max_png_bytes`.

Responses use `Cache-Control: private, max-age=<TTL>, must-revalidate` and an
`Age` rounded conservatively from the **original generation time**. A hit or 304
does not reset that time. ETags are quoted SHA-256 hashes of the actual PNG bytes;
GET supports `If-None-Match`, weak tags, lists and `*`. With TTL zero, responses
use `private, no-cache` and every request revalidates by rendering.

An expired/missing image is rendered **before** testing `If-None-Match`; the old
PNG's tag alone never produces 304 after expiry. If a new render produces the
same bytes, 304 is legitimate. Source HTML validators/Cache-Control are not
consulted, so changes may be invisible during the configured TTL. Shared proxies
do not cache these private responses by default. Internal reuse applies across
all authorized callers: use separate profiles/processes where caller-specific
authentication/state must be isolated. No disk cache, persistence, cross-replica
singleflight, distributed cache or HTTP cache-bypass directive is implemented.

## Docker and Kubernetes

Build the host architecture with `make image`, or select one explicitly:

```sh
make image PLATFORM=linux/amd64 IMAGE=webcappi:dev-amd64
make image PLATFORM=linux/arm64 IMAGE=webcappi:dev-arm64
```

Docker's `--platform` determines both the base image/browser architecture and
the Go executable architecture. The Go stage runs on `BUILDPLATFORM` and
cross-compiles with `TARGETOS`/`TARGETARCH`, so Go compilation does not require
QEMU. Browser dependency installation and its build-time version check run for
the target platform and require a native BuildKit worker or working emulation.
Only `linux/amd64` and `linux/arm64` runtime images are supported.

Export both CLI executables without running target-platform build steps:

```sh
make binaries
# artifacts/binaries/linux_amd64/webcappi
# artifacts/binaries/linux_arm64/webcappi
```

With a builder capable of both target platforms, export a multi-platform OCI
archive without publishing anything:

```sh
make image-multi IMAGE=webcappi:dev
# artifacts/webcappi.oci.tar
```

`PLATFORMS` overrides the default `linux/amd64,linux/arm64` list. Select a
configured builder through `BUILDX_BUILDER` when needed. A docker-container
builder can be created with
`docker buildx create --name webcappi-builder --driver docker-container --bootstrap`.
Some hosts also need emulation configured by their administrator; do not assume
cross-compiling Go makes foreign browser/container execution work.
See Docker's [multi-platform build documentation](https://docs.docker.com/build/building/multi-platform/).

For future CI/CD, use native Linux amd64 and arm64 runners in a matrix. Run the
same steps in each job, substituting that job's architecture:

```sh
export PLATFORM=linux/arm64
export WEBCAPPI_IMAGE=webcappi:ci-arm64
export WEBCAPPI_ARTIFACTS_DIR=artifacts/arm64
make test
make image IMAGE="$WEBCAPPI_IMAGE"
make browser-test
python3 scripts/container-smoke.py
```

This verifies each architecture's actual extension/profile lifecycle, fonts
and REST behavior as well as its image build. After both jobs pass, a release
job with registry credentials can publish a single multi-platform image using
`docker buildx build --platform linux/amd64,linux/arm64 --tag REGISTRY/IMAGE:TAG --push .`
on a builder supporting both platforms. Registry publication and an installed
CI workflow are not part of these local build commands.

After preparing a bundle and configuring matching descriptors:

```sh
docker run --rm --name webcappi -p 127.0.0.1:8080:8080 \
  --read-only --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  -v "$PWD/bundles:/bundles:ro" \
  webcappi:dev
```

The runtime uses UID/GID **10001**; it must read bundles and configuration and
write `/work`, `/tmp` and `/dev/shm`. Each capture has private HOME, XDG config/cache
and a profile copy under its work directory. Browser temporary files use a
separate private `/tmp/wc-*` directory, removed after browser exit; the short path
avoids Chromium's Unix socket path-length limit. Tini reaps orphaned descendants. The final image has no
runtime download step and no Go compiler. Multilingual fonts and Chromium's shared
libraries are installed at build time. Base OS package updates are not a
byte-for-byte reproducibility guarantee; promote the built image by digest.
For host bind mounts, explicitly grant UID 10001 read/traverse access using
ownership or a narrowly scoped ACL; `docker cp` normally exports files owned by
the invoking host user, with private file modes. Do not make credential-bearing
profiles world-readable. The bundle Dockerfile uses `COPY --chown=10001:10001`
so the initContainer can read private bundle files and copy them as that user.

The default keeps Chromium's own sandbox enabled. Your node/container policy
must allow Chromium's user-namespace sandbox. The development host's stock Docker
policy produced **No usable sandbox**; a sandbox-enabled deployment was not
validated here. webcappi fails startup in this situation, rather than advertising
readiness. Do not silently add privileged mode or grant SYS_ADMIN to make the
sample work. An administrator can evaluate the explicit `no_sandbox` option for
a controlled workload; doing so removes Chromium's sandbox isolation. See
[Chromium's AppArmor/user-namespace guidance](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).

`deploy/kubernetes.yaml` supplies a single-replica Deployment, ClusterIP Service,
ConfigMap, probes, resource limits, graceful termination, non-root execution,
read-only root/bundle mounts, writable emptyDir scratch and memory-backed shared
memory. Browser startup checks occur before listening. Readiness does not launch
a browser on each probe or mark the server unready simply because its one slot
is busy. A previously valid bundle becoming unreadable does make it unready.

Build the profile artifact independently of the API image:

```sh
docker build -f deploy/bundle.Dockerfile -t webcappi-profiles:reader-20261023-1 bundles
# Set registry/digest references in deploy/kubernetes.yaml before applying.
kubectl apply -f deploy/kubernetes.yaml
```

The initContainer copies the artifact to a pod-local emptyDir; the API mounts
that volume read-only. No PVC is required because the template is reproducible
from its artifact. Limit bundle size to the initContainer and volume resources;
adjust defaults for larger profiles. API and bundle images can be updated
independently, but the configured/browser/bundle versions must agree. For more
profiles, add artifact directories, initContainer copies and descriptors. The
example uses Recreate to avoid overlapping single-replica caches on updates.

This is an internal capture service with intentional access to private/cluster
addresses. It has no built-in authentication or SSRF boundary. Keep it behind
authorized callers and restrict ingress/egress using your network policy and
authentication gateway. A captured page's requests, redirects and extensions
inherit the browser pod's network reachability and prepared profile credentials.
Disk, memory, CPU and target-page behavior still require container resource
limits; PNG/cache limits do not bound all memory consumed by Chromium.

## Update a browser/extension/profile bundle

1. Stage a new bundle; retain extension public keys and absolute runtime paths.
2. For a browser update, update the pinned version in the Dockerfile, Go constant,
   descriptors, helper/test scripts and deployment. Build the new browser image.
3. Open a copy of the previous template with that exact image's headed browser;
   apply extension changes/settings/migrations, finish any new onboarding and
   close normally. Keep the previous bundle available for rollback.
4. Assign a new revision and update `bundle.json` and the configured descriptor
   together. Re-run fixture tests and qualification tests for real extensions.
5. Build/promote API and profile images with immutable tags/digests. Roll out a
   matching pair. Never migrate the live template or reuse a capture's mutated
   state as the new template.

Browser patch updates should be explicit, regular releases. The fixed browser
does not auto-update at runtime. Restarts discard the internal cache. Independent
replicas have independent caches and browser concurrency limits; this release
does not coordinate them.

## Verification and known limits

The 2026-10-23 verification run passed `go test -race ./...`, `go vet ./...`,
native binary/image builds, the real-browser lifecycle test and the actual REST
container smoke test. The profile artifact image was built and its init-style
copy was exercised as UID/GID 10001 with a read-only root. The Kubernetes objects
passed `kubectl create --dry-run=client --validate=false`; no resources were
created in a cluster by this verification.

The 2026-10-24 font update rebuilt the application image and passed the REST
container smoke test. CDP platform-font inspection on a local synthetic page
confirmed Noto fonts for representative Japanese, simplified/traditional
Chinese, Korean, Arabic, Hebrew, Devanagari, Thai, Vietnamese, Myanmar and Khmer
text, and Noto Color Emoji for representative emoji. This checks representative
strings, not complete language or Unicode coverage.

The Noto Mono and Roboto font set passed the REST container
smoke test, `go test -race ./...` and `go vet ./...`. The real-browser
`TestRenderedFontFamilies` checks the measured families above, including the
Liberation substitutions, Roboto and
normal-width `NotoSansMono-Regular` for both named and generic monospace text.

The multilingual screenshot fixture and `TestMultilingualRenderedFonts` also
passed in the application container. The expanded page passed the real-browser
extension/profile lifecycle test and REST smoke test, producing updated PNGs.

The 2026-10-24 multi-architecture verification built both runtime images and
exported both CLI executables through Docker. ELF inspection confirmed x86-64
and AArch64 binaries. `make image-multi` produced an OCI archive whose image
index and configuration blobs contain both `linux/amd64` and `linux/arm64`.
The ARM64 image's build-time browser version command ran under BuildKit's
QEMU emulation; its browser/profile/REST runtime behavior was not tested.
The amd64 browser tests and REST smoke test passed with explicit platform,
image and artifact-directory settings. Go race tests, vet, script syntax checks
and Dockerfile build checks passed. No images were published to a registry.

Implemented tests cover exact PNG dimensions, Japanese rendering, a pixel-level
extension marker, real headed unpacked installation, two independent headless
clones retaining extension storage without another initial welcome, unrelated
foreground tabs, immutable templates, cleanup on timeout/navigation error and
absence of surviving Chromium processes referring to private work directories.
The fixture deliberately opens extra tabs on normal page messages as well as
initial installation, so target selection is exercised.

Unit/integration tests cover input and bundle validation, LRU byte/entry limits,
cache expiry and content tags, coalescing, bounded admission/queueing, cancellation
with multiple waiters, deadlines, shutdown, HTTP errors and health/readiness.
`go test -race ./...` and `go vet ./...` are the standard checks.

`scripts/container-smoke.py` runs the actual REST image with a read-only root,
read-only template/extension mounts, writable tmpfs and a local page in another
container addressed through private Docker DNS. It checks PNG output, concurrent
coalescing and cache hits without another capture, TTL expiry after page changes,
conditional responses, input/timeout/load errors, scratch cleanup, template
integrity and normal SIGTERM exit. It does not contact an external page.

The supplied Kubernetes manifest is an example, not evidence of a live cluster
rollout. Real third-party extensions, native messaging hosts, cross-platform
profile migration, sandbox-enabled container execution on this host and desktop
display forwarding need environment-specific validation. Runtime verification
on this host uses Linux amd64 and the included Manifest V3 fixture; ARM64
browser/profile/REST runtime tests require a native ARM64 runner or working
runtime emulation. Full-page
capture, scale factors other than one, PDF, arbitrary caller JavaScript, custom
request headers and state write-back are intentionally outside this release.

## License

This project is licensed under the [MIT License](LICENSE).
