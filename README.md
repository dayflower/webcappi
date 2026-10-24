# webcappi

A Go REST server that captures a URL or posted HTML as a PNG with independent
viewport and output sizes, using a fresh Chromium process and a disposable copy
of a browser profile. The first release targets **Linux amd64 and arm64**, one
replica and a bounded, process-local cache of validation metadata. PNG bytes are
never cached by the server.
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

The tests export `artifacts/extension.png`, `artifacts/html.png`,
`artifacts/html-loopback.png`, `artifacts/api.png`, `artifacts/api-html.png`,
`artifacts/api-html-file.png`, `artifacts/api-html-loopback.png`, and a synthetic User Data Directory
at `artifacts/user-data`. Remove the old synthetic directory
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

## Start without a prepared profile

Omit `profiles` (or use an empty object) to start without preparing any browser
data. For example, this configuration uses all application defaults:

```json
{}
```

At startup, if `profiles.default` is absent, webcappi initializes the pinned
Chromium in headless mode, closes it normally, and retains a complete,
extension-free default template under `work_dir`. This also happens when other
named profiles are configured. If `profiles.default` is configured, it is used
instead; invalid or unreadable configured profiles fail startup.

Initialization and profile validation share a 30-second startup deadline and
finish before the API listens. Every capture uses a separate clone of the
template. Normal exit removes the generated template after captures finish;
it is never reused by a later process. In Kubernetes, the example's `emptyDir`
also removes any remaining data when the Pod is deleted. No profile artifact or
persistent volume is needed for this mode.

## Optional: prepare and import a production profile

Create a clean default profile with the application image. No existing Chrome
profile, configuration file, extension, display server or `xauth` is needed:

```sh
make image
docker run --rm --shm-size=512m webcappi:dev webcappi init-profile > default-profile.tar
mkdir -p bundles
tar -xf default-profile.tar -C bundles
```

The command starts the pinned full Chrome for Testing in headless mode on
`about:blank`, closes it normally to flush browser data, and exports the complete
User Data Directory. It does not navigate to a website or install an extension.
It keeps the browser sandbox enabled, uses a 30-second deadline and the default
512 MiB profile byte limit, and removes temporary browser data on completion or
failure. The container host must support the browser sandbox described below.
A failed command must not be followed by importing its output archive.

Standard output contains only the tar archive; errors go to standard error.
Native Linux users can specify `-browser-path` and `-work-dir` if needed.
`-out profile.tar` writes to a container/local path instead of stdout and refuses
to overwrite an existing file. This subcommand does not load server configuration.

The archive contains:

```text
bundles/default/          # after extracting into bundles/
  bundle.json
  extensions/            # empty; optional extensions can be added later
  user-data/Local State
  user-data/Default/Preferences
  user-data/...
```

The Kubernetes example generates its default automatically; a prepared bundle
is optional.
`config.example.json` shows the full configuration, including example extension
paths and readiness selectors. For a newly initialized bundle, set
`profiles.default.extensions` to `[]` and omit `ready_selectors`, matching its
generated `bundle.json`. Configure authentication as needed. Import the extracted directory through the
read-only `/bundles` mount, or build the profile artifact image as shown below.
Archive entries use private permissions and UID/GID 10001; host extraction may
assign the invoking user's ownership. The runtime must have read/traverse access
as described in the container instructions. Do not make credential-bearing
profiles world-readable.

The **User Data Directory** is the parent of individual profiles. Initialization
creates it for you, including `Local State` and `Default/`. Keep all of it when
importing or moving a bundle; passing `Default/` itself as `user_data_dir` is
incorrect. See Chromium's
[directory documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md).
The server checks that `bundle.json` exactly matches the configured descriptor.
Each capture runs on a disposable clone and never changes the template.

Generate and qualify each bundle with the production architecture and the same
application image. For example, use `docker run --platform linux/arm64 ...` with
an ARM64 image to initialize an ARM64 bundle. Keep architecture-specific bundles
in separate staging directories. An amd64 template is not automatically qualified
for arm64; preparing one template twice does not establish profile portability.

### Optional: add extensions, settings or logins

The basic initialization above does not require a GUI. To customize a profile,
work on an offline bundle copy and open it with the same image's visible browser.
A Linux X11 desktop with `xauth`, Docker and Python 3 is needed only for this
optional editing workflow:

1. Put your own unpacked extension directories under
   `bundles/default/extensions/<name>/`, each with its `manifest.json`. Keep
   extension code separate from `user-data/`. No verification extension is added.
2. Run `bash scripts/edit-profile.sh bundles/default bundles/default-edited`.
   Set `WEBCAPPI_IMAGE` and `PLATFORM` to select another qualified image/platform.
   The helper copies browser data and extension code into the container and
   leaves its source bundle unchanged. It refuses an existing output directory.
3. In Chrome, enable Developer mode and **Load unpacked** from
   `/bundles/default/extensions/<name>` for each extension you provided.
   Configure permissions, finish onboarding/logins and verify representative
   target pages. For posted HTML in file mode, enable **Allow access to file
   URLs** and use content-script matches that include `file:` URLs.
4. Use Chrome's **Exit** command so its databases flush. The helper saves the
   edited browser data and generates `bundle.json` with the provided extension
   paths. It does not guess readiness selectors; add those only when your
   extension or page provides a readiness signal.
5. Use the edited bundle as `/bundles/default` in a new deployment and update
   `profiles.default` to match its `bundle.json`. Run `webcappi check` and qualify
   captures before promoting it. The staging directory's name does not change
   the extension's fixed runtime paths.

The helper never installs extensions through per-capture flags. The server
verifies each configured extension's manifest public key, saved unpacked
registration, enabled state and exact absolute path, then checks that Chromium
actually enables it before the API starts listening. Mounted runtime bundles
remain immutable; prepare an updated bundle separately instead of editing it
in place.

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
verification. For optional interactive editing on macOS/Windows or a headless workstation,
use a Linux VM or a remote Linux desktop with this same image. Wayland/X11 forwarding and the manual
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
# Match profiles.default to the generated bundle.json.
# For an initialized bundle: set extensions to [] and omit ready_selectors.
# Create the configured token file, or omit auth to disable HTTP authentication.
./bin/webcappi check -config config.json
./bin/webcappi serve -config config.json
./bin/webcappi capture -config config.json -url http://page-service:8000/
```

`check` validates configuration, bundle descriptors, browser version, scratch
access and a real browser launch on each profile clone. The CLI capture command
uses the same validation and rendering path as the HTTP server and always
generates a new PNG. `version` prints the supported browser version.

```sh
curl --get http://localhost:8080/capture \
  --header 'Authorization: Bearer YOUR_TOKEN' \
  --data-urlencode url=http://page-service:8000/ \
  --output capture.png
```

| Method and endpoint | Behavior |
| --- | --- |
| `GET /` | Public capture API guide with curl examples and parameter descriptions; no authentication required |
| `GET /capture` | Capture an HTTP(S) URL as a direct `image/png` response |
| `POST /capture` | Capture a raw HTML body as a direct `image/png` response |
| `GET /healthz` | Process liveness; does not start Chromium |
| `GET /readyz` | Checks immutable bundle readability and writable scratch; 503 during shutdown |

To render a local HTML file, send its contents with `Content-Type: text/html`:

```sh
curl 'http://localhost:8080/capture' \
  --header 'Authorization: Bearer YOUR_TOKEN' \
  --header 'Content-Type: text/html; charset=utf-8' \
  --data-binary @page.html \
  --output capture.png
```

POST requires a non-empty HTML body and `Content-Type: text/html`.
`max_post_bytes` sets the size limit in bytes (default: 1 MiB, or 1,048,576 bytes).
JSON, form bodies and compressed bodies are not accepted. `url` is not accepted
on POST. The query parameters `profile`,
`width`, `height`, `output_width`, `output_height`, `ready_selector` and `delay_ms`
work as on GET.

The POST-only query parameter `html_mode` selects how Chromium opens the HTML:

| `html_mode` | Behavior |
| --- | --- |
| `file` (default when omitted) | Write a private temporary UTF-8 file and open its `file:///` URL |
| `loopback` | Serve the original body and uploaded `Content-Type` from `http://127.0.0.1:<temporary-port>/` during the capture |

For example, append `&html_mode=loopback` to the POST URL above to use HTTP.
An empty, unknown or repeated `html_mode` is rejected with 400. GET does not
accept this parameter. This is a request option, not a server configuration field.

Both modes open a new capture tab, run scripts, and use the same disposable
profile, viewport, readiness/asset checks and deadline. File mode requires valid
UTF-8 bytes and either an omitted charset or `charset=utf-8` (case-insensitive).
Other charset values return HTTP 415; invalid UTF-8 bytes return 400. File mode
adds a UTF-8 BOM, so no `<meta charset>` is needed.
Loopback mode preserves the uploaded `Content-Type`, including charset parameters,
and the body bytes without conversion or an added BOM. If charset is omitted,
Chromium detects the encoding using the document's BOM, `<meta charset>` or its
normal fallback behavior. Supply an explicit charset for predictable decoding.
Temporary files and loopback listeners are
removed on completion, failure or cancellation. The loopback server exposes
only the posted document, not a directory or another application endpoint.

Use absolute resource URLs or an HTML `<base href="https://example.com/">` for
relative paths. A base URL only resolves resources; it does not give the page
that site's origin or same-origin access to its storage. Avoid relying on a
fixed `location`: file paths and loopback ports are temporary. Browser
origin/CORS rules still apply to external resources. File mode requires
extensions to have file access enabled in the prepared profile and
content-script matches for `file:` URLs. Loopback mode requires content scripts
to match HTTP on `127.0.0.1`; HTTPS-only or domain-specific scripts may not run.
The pinned browser's real-browser tests check `isSecureContext` in both modes;
secure-context status does not remove API-specific scheme or origin restrictions.
The local CLI continues to accept URLs through `-url`.

### API authentication

Configure a shared Bearer token file to require authentication for HTTP requests
other than `/`, `/healthz` and `/readyz`:

```json
{
  "auth": {
    "bearer_token_file": "/run/secrets/webcappi/token"
  }
}
```

Omitting `auth` disables authentication for compatibility with existing
configurations. The guide at `/` is always public. It includes authentication
instructions only when authentication is enabled and displays the configured
capture dimension, pixel count, HTML body size and deadline limits. It does not
expose tokens, configured profile names or filesystem paths.
When present, `auth.bearer_token_file` must be an absolute path.
The file must contain one token of 1–4096 ASCII bytes using the Bearer token
alphabet (letters, digits, `-._~+/`, and optional trailing `=` padding).
A single trailing LF or CRLF is accepted; spaces, empty files and multiple
lines are rejected. For example, generate a random token in a protected local
directory:

```sh
umask 077
openssl rand -hex 32 > /secure/path/webcappi-token
```

The token is loaded once, before browser startup, by `serve`, `check` and
`capture`. Missing, unreadable or invalid files fail startup. Only a SHA-256
digest is retained by the authenticator, and tokens are never included in its
errors. Restart the process after rotating the token; mounted Secret updates
are not automatically reloaded. The local `capture` CLI does not require an
Authorization header, but still validates the configured token file.

Send `Authorization: Bearer <token>` on each API request. Missing or invalid
credentials return JSON with HTTP 401, `WWW-Authenticate: Bearer` and
`Cache-Control: no-store`, before input validation, admission or cache access.
Tokens in query parameters or cookies are not accepted. Authentication applies
to conditional requests as well as new captures. The API token
is not forwarded to the captured site. All holders of the shared token can use
all configured profiles and share the validation metadata cache.

The example configuration enables authentication. Use TLS at your ingress or
reverse proxy to protect Bearer tokens in transit; the application itself serves
HTTP. Keep the token file readable only by the intended runtime identity. For
Kubernetes, mount a Secret separately from the configuration ConfigMap, as in
the supplied manifest.

### Capture parameters and responses

GET requires an absolute HTTP(S) `url`; all other query parameters are optional.
HTML POST requires no query parameters. Omitting `profile` selects the
`default` profile (configured or generated), as does omitting CLI `-profile`. Specify another configured
name to select a different profile. An explicitly empty or unknown profile name
is rejected.
`width` and `height` set the page's viewport in CSS pixels, defaulting to 1280 and
720 in both REST and the capture CLI. They control responsive layout and the
captured visible area, not the full scrollable page. Optional `output_width` and
`output_height` set the PNG canvas dimensions in pixels; each omitted dimension
defaults to the corresponding
viewport dimension. The viewport image is resized proportionally to fill the
output canvas, then cropped around the center. Different aspect ratios remove
equal portions from the left/right or top/bottom, without stretching or adding
padding. Scaling can enlarge or shrink the image, using bilinear filtering from
`golang.org/x/image/draw` after capture by default. Set the server configuration
`resize_algorithm` to `bilinear`, `catmull_rom`, `approx_bilinear` or
`nearest_neighbor`. `catmull_rom` uses bicubic filtering; `approx_bilinear` uses
bilinear interpolation when enlarging and a faster approximation when shrinking;
`nearest_neighbor` selects the closest source pixel without blending colors.
This setting applies to GET, HTML POST and
the local capture CLI. It requires a process restart, which clears the validation
metadata; callers cannot override the algorithm per request.
Browser device scale remains fixed at 1; resizing does not reflow the page or
render at a higher device pixel ratio. With matching viewport/output sizes,
the original screenshot is returned without re-encoding.

For example, `&width=1200&height=800&output_width=600&output_height=600` lays out
the page at 1200 x 800, scales its image to 900 x 600, and removes 150 pixels
from each side to return a 600 x 600 PNG. The CLI equivalents are `-width`,
`-height`, `-output-width` and `-output-height`:

```sh
webcappi capture -config config.json -url http://example.internal/ \
  -width 1200 -height 800 -output-width 600 -output-height 600 -out capture.png
```

All four dimensions must be positive integers. Viewport and output dimensions
are independently checked against the same `max_width`, `max_height` and
`max_pixels` limits. Explicitly empty, zero, negative, fractional, repeated or
oversized values are rejected with HTTP 400.
Configuration limits must accommodate the default viewport: `max_width` must
be at least 1280, `max_height` at least 720 and `max_pixels` at least 921600.

Optional `ready_selector` waits for
an element in the page;
profile `ready_selectors` lists readiness signals, for example one per extension:

```json
"ready_selectors": ["#extension-a-ready", "#extension-b-ready"]
```

All configured selectors, including the request's `ready_selector`, must match
an element in the capture document in the same polling check before capture
continues. Elements may be hidden. The page or extensions must create these
signals; entries are not automatically associated with extension paths or IDs.
The optional array accepts at most 64 non-empty selectors, each up to 1024 bytes.
Omit `ready_selectors` or use an empty array to skip profile-specific readiness.
Keep the profile descriptor in `bundle.json` identical to the configuration when
changing these settings. The HTTP query parameter remains `ready_selector`.

The optional GET/POST query parameter `delay_ms` adds a wait after all readiness
checks, including the two animation frames, immediately before the screenshot.
It accepts an integer from 0 to 300000 milliseconds and defaults to 0 (no extra
wait). For example, `&delay_ms=1000` waits one additional second. Empty, negative,
fractional, out-of-range or repeated values are rejected with 400. The delay
shares the overall capture deadline and is interrupted by cancellation; it does
not extend `capture_timeout_seconds`. A metadata-based 304 skips capture and
its delay. Assets and readiness signals are not rechecked after the delay.

GET and POST are accepted on `/capture`; repeated
and unknown query parameters, credentials in URLs, unsupported schemes and excessive dimensions
are rejected. URL fragments remain part of the capture URL and cache key.

Errors are JSON with an `error` field and `Cache-Control: no-store`: 400 for input,
404 for an unknown endpoint, 405 for another method, 413 for an oversized HTML
body, 415 for an unsupported
HTML media type, charset or content encoding, 502 for browser/navigation,
HTTP 4xx/5xx from the target or asset failure, 503 for exhausted capacity,
cancellation or shutdown, and 504 for the capture deadline. Overload responses
include `Retry-After: 1`. Target URL errors are logged without logging the full
requested URL. Do not put credentials in query strings.

Navigation waits for the main document's load event. Next come the requested page
and extension selectors, `document.fonts.ready`, load/decode of `<img>` elements
intersecting the viewport, two animation frames, and the optional `delay_ms` wait.
Broken visible images fail the capture. This does not infer application
readiness, load all lazy/offscreen
images, inspect cross-origin frames, or independently wait for CSS backgrounds,
video, canvas, animations or future DOM changes. Use a page/extension readiness
contract for those cases. No network-idle check or automatic fixed-duration delay
is used. The optional delay does not guarantee application readiness. All waits
share the overall deadline.

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
| `no_sandbox` | `false` | Explicit browser sandbox opt-out |
| `work_dir` | `/work` | Writable scratch and automatic template; separate from supplied templates/extensions |
| `concurrency` | 1 | Active browser captures (1–32) |
| `queue_size` | 8 | Additional pending captures or source validations (0–1024) |
| `max_requests` | 64 | Concurrent HTTP capture handlers, including metadata lookups |
| `capture_timeout_seconds` | 30 | Queue + profile copy + browser start + readiness + delay + PNG (1–300) |
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

`profiles.default` is optional. When absent, a temporary default is generated at
startup, including when only other named profiles are supplied. An explicitly
configured invalid `default` fails startup instead of falling back.
The name `default` selects a capture profile; `profile_directory` identifies the
individual Chromium profile inside its User Data Directory, usually `Default`.

Profile fields are `user_data_dir`, `profile_directory`, `extensions`, and optional
`ready_selectors`. The application pins and checks the browser version; profiles
do not declare it.
`user_data_dir` must have a sibling `bundle.json`. Scratch and supplied templates cannot
overlap; copies reject special files and symlinks except obsolete top-level
Chromium lock/endpoint files, which are excluded. Files in copies are independent
and writable even when original files are read-only. No hard links, shared work
profiles, template launches or writes back to the template are used.

Each capture or source-validation job has its own deadline and directory.
Unconditional GET and all POST requests create independent jobs; no PNG results
are shared between requests. Canceling a request cancels its own job.
Successful browsers close normally, then descendants
are reaped/terminated before scratch is removed. Cleanup has a separate short
grace period. SIGTERM stops admission/readiness, cancels active/queued captures,
waits for their cleanup and shuts down HTTP. SIGKILL/node loss cannot run cleanup;
the example's emptyDir is reclaimed with the pod.

## Conditional GET and freshness

The server never retains PNG bytes. Unconditional GET requests always capture a
new image, including concurrent requests for identical conditions. POST also
always captures a new image and returns `Cache-Control: no-store`, without ETag
or Age; it ignores `If-None-Match` and never uses validation metadata.

GET ETags are quoted SHA-256 hashes of the actual PNG bytes. The server retains
only the PNG ETag, validation time, freshness policy and main document URL,
ETag/Last-Modified and freshness headers. Metadata uses an LRU bounded by
`metadata_max_bytes` and `metadata_max_entries`. Expired entries can retain source
validators until eviction. Process restarts discard all metadata. Errors and
responses marked `no-store` are not retained.

The metadata key contains URL (including fragments), viewport, output dimensions,
the request's `ready_selector` and `delay_ms`, profile name, scale factor and
renderer schema. Omitted output dimensions and explicit viewport-sized values
share a key. Profile settings and bundle contents are fixed for the process and
are not part of the key.

Conditional GET supports weak tags, lists and `*`:

1. Matching fresh metadata returns 304 without accessing the source or starting a
   browser. Its validation time is not renewed by this shortcut.
2. Matching expired metadata with a source ETag or Last-Modified starts the same
   disposable browser/profile used for capture and sends a conditional request
   to the previously captured main document URL. A source 304 renews metadata
   and returns 304 without readiness waits, subresource loading or PNG generation.
   Changed sources continue through normal capture. Validators are not sent to
   unrelated redirects, child frames or subresources.
3. Missing metadata, a mismatching client tag, or an expired entry without source
   validators triggers a new capture. If the new PNG still matches the client's
   tag, the response is 304; otherwise it is 200 with the PNG.

The default freshness lifetime is 60 seconds. A source `Cache-Control: max-age`
limits this to the source's remaining lifetime, conservatively accounting for
Age, Date and time spent navigating, capturing and closing the browser. Invalid
or repeated source max-age values require validation. Source `no-cache` requires
validation on each request; `no-store` prevents metadata retention and is
propagated downstream. A source 304 merges its updated policy/validators with
previous metadata and resets Date/Age accounting for the new validation.
`max_age_seconds: 0` also requires validation on every conditional request.

GET responses use `Cache-Control: private, max-age=<lifetime>, must-revalidate`,
or `private, no-cache` when immediately stale. Age reflects elapsed time since
the image was generated or the source was last validated, rounded upward on
metadata reuse. It does not restart on a fresh-metadata 304. With no-store,
responses use `private, no-store` and omit Age.

This intentionally treats the main HTML document's validation as evidence that
the capture is unchanged. CSS, images, scripts, time-dependent rendering and
extension state can change independently; those changes are not checked on a
metadata/source-based 304. Without source validators, the configured lifetime
is an explicit window in which changes are assumed absent. Chromium's HTTP
cache is disabled during capture/navigation so prepared profile disk cache does
not mask source updates. Service workers retain their normal profile behavior.

Metadata is shared by authorized callers within the process. Responses remain
private by default; deployments that need image caching can configure an
appropriately isolated reverse proxy. No image cache, capture coalescing,
distributed state or client cache-bypass directive is implemented.

Configuration migration: replace `cache_ttl_seconds` with `max_age_seconds`,
remove `cache_max_bytes` and `cache_max_entries`, and optionally set the new
metadata limits. The old fields are rejected as unknown configuration keys.

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

Configure authentication as needed. With automatic profile generation, omit
`profiles` and run without a bundle mount:

```sh
docker run --rm --name webcappi -p 127.0.0.1:8080:8080 \
  --read-only --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  -v /secure/path/webcappi-token:/run/secrets/webcappi/token:ro \
  webcappi:dev
```

To use prepared profiles, add a read-only `/bundles` mount and matching profile
descriptors. `config.example.json` illustrates those optional settings.

The runtime uses UID/GID **10001**; it must read configuration and any supplied bundles and
write `/work`, `/tmp` and `/dev/shm`. Each capture has private HOME, XDG config/cache
and a profile copy under its work directory. Browser temporary files use a
separate private `/tmp/wc-*` directory, removed after browser exit; the short path
avoids Chromium's Unix socket path-length limit. Tini reaps orphaned descendants. The final image has no
runtime download step and no Go compiler. Multilingual fonts and Chromium's shared
libraries are installed at build time. Base OS package updates are not a
byte-for-byte reproducibility guarantee; promote the built image by digest.
For host bind mounts, explicitly grant UID 10001 read/traverse access to the
token file and bundles using ownership or a narrowly scoped ACL; `docker cp`
normally exports files owned by
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
read-only root mounts, writable emptyDir scratch and memory-backed shared
memory. Browser startup checks occur before listening. Readiness does not launch
a browser on each probe or mark the server unready simply because its one slot
is busy. A previously valid bundle becoming unreadable does make it unready.

Create the authentication Secret from your protected token file. The example
mounts it read-only with mode 0440; the pod's group 10001 can read it. Provision
this Secret in the same namespace as the Deployment and keep the client copy
secure:

```sh
kubectl create secret generic webcappi-auth \
  --from-file=token=/secure/path/webcappi-token
```

Set the API image's registry/digest reference before applying:

```sh
kubectl apply -f deploy/kubernetes.yaml
```

For optional prepared profiles, build the artifact independently of the API image:

```sh
docker build -f deploy/bundle.Dockerfile -t webcappi-profiles:default-20261023-1 bundles
```

Add an initContainer to copy `/bundle/default` from this artifact to a pod-local
emptyDir, mount that volume read-only at `/bundles` in the API container, and set
`profiles.default` to match the bundle descriptor. No PVC is required because the template is reproducible
from its artifact. Limit bundle size to the initContainer and volume resources;
adjust defaults for larger profiles. API and bundle images can be updated
independently. Prepare and qualify each bundle with the browser shipped in the
target API image. For more profiles, add artifact directories, initContainer
copies and descriptors. The example uses Recreate to avoid overlapping
replicas on updates.

This is an internal capture service with intentional access to private/cluster
addresses. Optional shared Bearer authentication controls API access; it does
not provide per-caller authorization or an SSRF boundary. Restrict ingress/egress
using your network policy and use an authentication gateway when more granular
access control is needed. A captured page's requests, redirects and extensions
inherit the browser pod's network reachability and prepared profile credentials.
Disk, memory, CPU and target-page behavior still require container resource
limits; PNG/metadata limits do not bound all memory consumed by Chromium.

## Update a browser/extension/profile bundle

1. Stage a new bundle; retain extension public keys and absolute runtime paths.
2. For a browser update, update the pinned version in the Dockerfile, Go constant,
   documentation and test scripts. Build the new browser image.
3. Open a copy of the previous template with that exact image's headed browser;
   apply extension changes/settings/migrations, finish any new onboarding and
   close normally. Keep the previous bundle available for rollback.
4. Keep `bundle.json` and the configured descriptor in sync. Re-run fixture
   tests and qualification tests for real extensions.
5. Build/promote API and profile images with immutable tags/digests. Roll out a
   matching pair. Never migrate the live template or reuse a capture's mutated
   state as the new template.

Browser patch updates should be explicit, regular releases. The fixed browser
does not auto-update at runtime. Restarts discard the metadata cache. Independent
replicas have independent validation metadata and browser concurrency limits;
this release does not coordinate them.

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

The Bearer authentication update passed Go 1.26.10 race tests and vet, the amd64
image build, real-browser tests and the authenticated REST container smoke test.
Both amd64 and arm64 CLI binaries were built and their ELF architectures checked.
The Kubernetes manifest passed client-side dry-run validation. The current
builder could not execute ARM64 build steps (`exec format error`), so this
update's multi-platform image build did not complete and ARM64 runtime behavior
was not verified. No cluster resources were created.

Automatic default profiles passed Go 1.26.10 race tests and vet, the amd64 image
build and real-browser tests. The container smoke test verifies omitted profiles,
named-only profiles, configured-default precedence, immutable generated templates,
readiness after template loss, and cleanup on SIGTERM and `check` exit. Cleanup is
checked while the container and its scratch volume remain alive. Both CLI
architectures build; the current builder's ARM64 execution limitation described
above still prevents multi-platform image and ARM64 runtime verification.

Independent viewport/output sizes passed Go 1.26.10 race tests and vet, the amd64
image build, real-browser tests and container smoke tests, including REST, HTML
POST and CLI resizing. Pixel checks verify unchanged responsive layout and
proportional center cropping; unit tests cover both crop axes, up/downscaling,
extreme aspect ratios, validation and cache separation. All four resize filters
passed these checks. Container smoke tests exercise the default bilinear API
filter and explicit Catmull-Rom, approximate bilinear and nearest-neighbor CLI
configurations. GET, HTML POST and CLI smoke tests also verify the 1280 x 720
default dimensions. CLI binaries for both architectures built successfully, and ELF
inspection confirmed their architectures. The multi-platform image build
failed at ARM64 execution with `exec format error`; ARM64 runtime behavior was
not verified.

The metadata-only conditional GET update passed Go 1.26.10 race tests and vet,
the amd64 image build, real-browser tests and REST container smoke tests. Tests
verify fresh-metadata 304s without a browser, source ETag/Last-Modified validation
without PNG generation, validator-free expiry with recapture, independent
unconditional captures and metadata limits. Both CLI architectures built and
passed ELF architecture inspection. The multi-platform image build still fails
at ARM64 execution with `exec format error`; ARM64 runtime remains unverified.
Browser-test startup waits and failure cleanup were hardened for loaded hosts;
production capture deadlines and sandbox defaults were preserved.

Implemented tests cover exact PNG dimensions, Japanese rendering, both HTML
source modes and their secure-context status, a pixel-level
extension marker, real headed unpacked installation, two independent headless
clones retaining extension storage without another initial welcome, unrelated
foreground tabs, immutable templates, cleanup on timeout/navigation error and
absence of surviving Chromium processes referring to private work directories.
The fixture deliberately opens extra tabs on normal page messages as well as
initial installation, so target selection is exercised.

Unit/integration tests cover input and bundle validation, LRU byte/entry limits,
metadata freshness and content tags, source revalidation, bounded
admission/queueing, independent cancellation, deadlines, shutdown, HTTP errors
and health/readiness.
`go test -race ./...` and `go vet ./...` are the standard checks.

`scripts/container-smoke.py` runs the actual REST image with a read-only root,
read-only template/extension mounts, writable tmpfs and a local page in another
container addressed through private Docker DNS. It checks PNG output,
metadata-based conditional responses without another capture, independent
unconditional captures, Bearer authentication including conditional responses
and unauthenticated probes, expiry and source validation after page changes,
validator-free fallback, input/timeout/load errors, scratch cleanup, template
integrity and normal SIGTERM exit. HTML POST tests check the default file mode,
explicit file/loopback selection, size limits, and generation on every request.
It does not contact an external page.

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
