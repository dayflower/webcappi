# webcappi

A Go REST server that captures a URL or posted HTML as a PNG with independent
viewport and output sizes, using a fresh Chromium process and a disposable copy
of a browser profile. The first release targets **Linux amd64 and arm64**, one
replica and a bounded, process-local cache of validation metadata. PNG bytes are
never cached by the server.
Private addresses and cluster Service DNS names are intentionally supported.

## Intended use and security

webcappi is intended for local use and controlled internal networks, with a
small set of trusted callers. It is not designed for direct Internet exposure
or use as an anonymous, public screenshot service.

Chromium executes JavaScript from target pages and posted HTML normally.
Pages, redirects, scripts and other resources can make requests using the
browser container's network access, and the selected profile may supply saved
credentials. A caller can capture internal pages and receive their contents as
screenshots. Page scripts can also contact internal services or send data they
can access to external endpoints. Access to this API can therefore expose
information from the local network or prepared profiles.

Captures can consume significant CPU, memory and network resources. A caller
or a captured page can overload the capture host or cause it to generate heavy
traffic toward other services, making it a potential source of denial-of-service
traffic. Bounded queues, concurrency and deadlines do not bound all activity
or resource use inside Chromium.

PNG images are not cached: unconditional GET and all HTML POST requests perform
independent captures. Only bounded validation metadata is retained for
conditional GET responses. This does not provide an image cache for large
numbers of unrelated callers.

Keep the API accessible only to trusted callers. Shared Bearer authentication
controls API access, but does not restrict which reachable sites a token holder
can capture or provide per-caller authorization. Restrict ingress and egress
with network policy, apply container resource limits, and use an authentication
gateway when finer access control is needed. The service intentionally accepts
private addresses and does not provide an SSRF prevention boundary.

## Quick start

Docker with Buildx is required. The host must support Chromium's sandbox; see
[container requirements](#docker-and-kubernetes) if startup reports
`No usable sandbox`.

Build the application image and create a configuration using the defaults:

```sh
make image
printf '{}\n' > config.json
docker run --rm --name webcappi -p 127.0.0.1:8080:8080 \
  --read-only --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  webcappi:dev
```

In another terminal, capture a page:

```sh
curl --get http://localhost:8080/capture \
  --data-urlencode url=https://example.com/ \
  --output capture.png
```

This starts with an automatically generated, extension-free profile and no API
authentication. The published port is bound to loopback. Configure
[authentication](#api-authentication) before exposing the API to other callers.
The public guide at `http://localhost:8080/` has capture examples.

For a single capture without starting the API server, use the same image's
[`capture` command](#one-shot-capture-with-docker).

## Browser and fonts

The image includes full **Chrome for Testing 156.0.8078.47**, extensions in
unified headless mode, and Noto fonts for multilingual text, CJK and color emoji.
Both supported architectures use the same browser version. No browser is
downloaded at runtime, and startup rejects a different browser brand/version.
Fonts and CSS fallback influence layout; qualify representative pages after
changing the image or profile. Installed fonts do not guarantee complete
language or Unicode coverage. Detailed font measurements and browser rationale
are in the [browser notes](notes/BROWSER.md).

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

## One-shot capture with Docker

The same Docker image supports both a long-running API server (`serve`) and a
single capture (`capture`). Use `capture` for ad hoc screenshots, scripts or CI
jobs: it starts Chromium, saves a PNG and exits, without starting an HTTP server.
The image supplies the pinned browser, fonts and libraries, so no native browser
environment is needed on the host.

`capture` accepts an HTTP(S) URL through `-url` and writes the PNG to `-out`.
To render raw HTML, use the [HTTP API](#http-api).
Native Linux setup and command examples are in the
[development guide](notes/DEVELOP.md#toolchain-and-local-build).

The command syntax is:

```text
webcappi capture -url URL [flags]
```

| Flag | Purpose | Default |
| --- | --- | --- |
| `-url URL` | HTTP(S) URL to capture; required | None |
| `-config PATH` | Configuration file | `config.json` |
| `-out PATH` | PNG output file | `capture.png` |
| `-width N` | Viewport width in pixels | `1280` |
| `-height N` | Viewport height in pixels | `720` |
| `-output-width N` | PNG canvas width in pixels | Viewport width |
| `-output-height N` | PNG canvas height in pixels | Viewport height |
| `-profile NAME` | Profile name from the configuration | `default` |
| `-ready-selector CSS` | Wait for an element matching this CSS selector | No additional selector |

Output dimensions resize and center-crop the captured viewport; they do not
change page layout. Select a configured profile with `-profile` to use its
extensions or saved state. `-ready-selector` adds a page readiness check to the
configured profile selectors. Use `webcappi capture -help` to print the flags.

After building the image, create a default configuration and run:

```sh
printf '{}\n' > capture-config.json
docker run --name webcappi-capture --shm-size=512m --tmpfs /tmp \
  -v "$PWD/capture-config.json:/etc/webcappi/config.json:ro" \
  webcappi:dev webcappi capture -config /etc/webcappi/config.json \
  -url https://example.com/ -out /work/capture.png
```

Here, `webcappi:dev` is the Docker image; the following `webcappi capture` runs
the executable's `capture` subcommand. `-config` and `-out` refer to paths inside
the container. Set `-out` to a writable location such as `/work/capture.png`.
Append `-width 1200 -height 800` to set the viewport and
`-output-width 600 -output-height 600` to produce a square PNG.

After a successful capture, copy the PNG to the host and remove the container:

```sh
docker cp webcappi-capture:/work/capture.png ./capture.png
docker rm webcappi-capture
```

This uses an automatically generated, extension-free profile. The container's
writable `/work` directory retains the output after exit until `docker rm`;
omit `--rm` and a `/work` tmpfs so `docker cp` can retrieve it. Remove a failed
container before retrying with the same name. Chromium's sandbox stays enabled
and requires the same [host support](#docker-and-kubernetes) as the API server.

## HTTP API

Start the API server as shown in the [quick start](#quick-start), then request a
capture with an HTTP client. For a server with Bearer authentication enabled:

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

GET requires an absolute HTTP(S) `url` of at most 8192 bytes; all other query
parameters are optional.
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
padding. Scaling can enlarge or shrink the image, using bilinear filtering after
capture by default. Set the server configuration
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

Optional `ready_selector` (at most 1024 bytes) waits for
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
are rejected. The encoded HTTP query is limited to 16 KiB. URL fragments remain
part of the capture URL and cache key.

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

The platform selects both the application and browser architecture. Running
foreign-architecture containers requires working runtime emulation; native
runners are preferable. For CLI exports and multi-platform image builds, see
[build artifacts](notes/DEVELOP.md#build-artifacts-and-platforms).

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

The runtime uses UID/GID **10001**; it must read configuration and any supplied
bundles and write `/work`, `/tmp` and `/dev/shm`. Browser processes and temporary
capture data are cleaned up after each job. The final image has no runtime
download step and no Go compiler. Multilingual fonts and Chromium's shared
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

## Update a browser/extension/profile bundle

1. Stage a new bundle; retain extension public keys and absolute runtime paths.
2. Build or obtain the API image with the desired pinned browser. Maintainers
   changing the pin should follow the
   [browser update checks](notes/DEVELOP.md#changing-the-browser-or-fixtures).
3. Open a copy of the previous template with that exact image's headed browser;
   apply extension changes/settings/migrations, finish any new onboarding and
   close normally. Keep the previous bundle available for rollback.
4. Keep `bundle.json` and the configured descriptor in sync. Repeat
   qualification on representative pages and run `webcappi check` with the
   target configuration.
5. Build/promote API and profile images with immutable tags/digests. Roll out a
   matching pair. Never migrate the live template or reuse a capture's mutated
   state as the new template.

Browser patch updates should be explicit, regular releases. The fixed browser
does not auto-update at runtime. Restarts discard the metadata cache. Independent
replicas have independent validation metadata and browser concurrency limits;
this release does not coordinate them.

## Support and limitations

Linux amd64 browser/extension/profile/API behavior has been verified with the
included synthetic fixture. Both amd64 and arm64 CLI binaries and images have
been built, but arm64 browser/profile/API runtime remains unverified. Qualify
arm64 deployments on a native runner or with working runtime emulation.

The development host does not support sandbox-enabled container execution under
its stock policy; disposable tests use an explicit sandbox exception. Verify the
production host's sandbox support. Third-party extensions, native messaging,
cross-platform profile migration and manual display forwarding require
validation in the target environment. The supplied Kubernetes manifest is an
example; no live cluster rollout was verified.

Full-page capture, device scale factors other than one, PDF, a dedicated
JavaScript execution API, custom request headers and state write-back are outside
this release. Page scripts still execute during capture as described above.

For source changes and regression verification, see the
[development guide](notes/DEVELOP.md). Browser implementation details and measured
font behavior are documented in the [browser notes](notes/BROWSER.md).

## License

This project is licensed under the [MIT License](LICENSE).
