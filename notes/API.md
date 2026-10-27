# HTTP API reference

See the [quick start](../README.md#quick-start) to run the server and
[authentication](../README.md#authentication) to enable a shared Bearer token.

## Endpoints and HTML input

| Method and endpoint | Behavior |
| --- | --- |
| `GET /` | Public capture API guide with curl examples and parameter descriptions; no authentication required |
| `GET /capture` | Capture an HTTP(S) URL as a direct `image/png` response |
| `POST /capture` | Capture a raw HTML body as a direct `image/png` response |
| `GET /healthz` | Process liveness; does not start Chromium |
| `GET /readyz` | Checks bundle readability, writable scratch and Redis when configured; 503 during shutdown |

When authentication is enabled, send `Authorization: Bearer <token>` on capture
requests, including conditional GETs. Missing or invalid credentials return 401
with `WWW-Authenticate: Bearer` and `Cache-Control: no-store`. Query parameters
and cookies cannot supply the token, and it is not forwarded to the captured
site. `/`, `/healthz` and `/readyz` remain public.

To render a local HTML file, send its contents with `Content-Type: text/html`:

```sh
curl 'http://localhost:8080/capture' \
  --header 'Content-Type: text/html; charset=utf-8' \
  --data-binary @page.html \
  --output capture.png
```

POST requires a non-empty HTML body and `Content-Type: text/html`.
`max_post_bytes` sets the size limit in bytes (default: 1 MiB, or 1,048,576 bytes).
JSON, form bodies and compressed bodies are not accepted. `url` is not accepted
on POST. The query parameters `profile`,
`width`, `height`, `output_width`, `output_height`, `full_page`,
`ready_selector` and `delay_ms`
work as on GET.

The POST-only query parameter `html_mode` selects how Chromium opens the HTML:

| `html_mode` | Behavior |
| --- | --- |
| `file` (default when omitted) | Write a private temporary UTF-8 file and open its `file:///` URL |
| `loopback` | Serve the original body and uploaded `Content-Type` from `http://127.0.0.1:<temporary-port>/` during the capture |

For example, append `?html_mode=loopback` to the POST URL above to use HTTP.
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
Secure-context status does not remove API-specific scheme or origin restrictions.

## Capture parameters and responses

GET requires an absolute HTTP(S) `url` of at most 8192 bytes; all other query
parameters are optional.
HTML POST requires no query parameters. Omitting `profile` selects the
`default` profile (configured or generated). Specify another configured
name to select a different profile. An explicitly empty or unknown profile name
is rejected.
`width` and `height` set the page's viewport in CSS pixels, defaulting to 1280 and
720 in both REST and the capture CLI. They control responsive layout and the
captured visible area by default. Use `full_page=true` to capture the full
scrollable document while retaining this viewport for layout. Optional
`output_width` and
`output_height` set the PNG canvas dimensions in pixels; each omitted dimension
defaults to the corresponding
captured dimension (the viewport normally, or the measured full page when
`full_page=true`). The captured image is resized proportionally to fill the
output canvas, then cropped around the center. Different aspect ratios remove
equal portions from the left/right or top/bottom, without stretching or adding
padding. Scaling can enlarge or shrink the image, using bilinear filtering after
capture by default. Set the server configuration
`resize_algorithm` to `bilinear`, `catmull_rom`, `approx_bilinear` or
`nearest_neighbor`. `catmull_rom` uses bicubic filtering; `approx_bilinear` uses
bilinear interpolation when enlarging and a faster approximation when shrinking;
`nearest_neighbor` selects the closest source pixel without blending colors.
This setting applies to GET, HTML POST and the local capture CLI. It requires a
process restart; callers cannot override the algorithm per request. A restart
clears memory metadata; Redis uses a separate scope for each resize algorithm.
Browser device scale remains fixed at 1; resizing does not reflow the page or
render at a higher device pixel ratio. With matching captured/output sizes,
the original screenshot is returned without re-encoding.

For example, `&width=1200&height=800&output_width=600&output_height=600` lays out
the page at 1200 x 800, scales its image to 900 x 600, and removes 150 pixels
from each side to return a 600 x 600 PNG.

All four dimensions must be positive integers. Viewport and output dimensions
are independently checked against the same `max_width`, `max_height` and
`max_pixels` limits. Explicitly empty, zero, negative, fractional, repeated or
oversized values are rejected with HTTP 400.
Configuration limits must accommodate the default viewport: `max_width` must
be at least 1280, `max_height` at least 720 and `max_pixels` at least 921600.

The optional GET/POST parameter `full_page` accepts `true`, `false` or an empty
value. Omission and empty values (including a bare `?full_page`) mean `false`.
Other values, including `1` and `TRUE`, and repeated parameters return 400.
The CLI equivalent is `-full-page` (or `-full-page=false`).

Full-page mode measures the scrollable document after the existing readiness
checks and optional delay, then captures that bounded area at device scale 1.
It includes horizontal document overflow and does not resize the viewport to
match the document. Omitted output dimensions are resolved independently from
the measured source: for example, setting only `output_width` keeps the full
source height as the output height and applies the same fill-and-center-crop
rule. Set both output dimensions for a proportional downscaled full-page image.

The complete source and resolved output are checked against `max_width`,
`max_height` and `max_pixels` before screenshot generation, regardless of output
scaling. A document or resolved output exceeding these runtime limits returns
502 without a partial image. Explicit request dimensions outside the limits
still return 400. The default maximum height is 4096 pixels; configure larger
limits for longer documents. Content growing after measurement can fall outside
the captured area. No automatic scrolling, lazy-loading activation, stitching,
nested-scroll expansion or virtualized-list traversal is performed. Readiness
still checks fonts and viewport-intersecting images; offscreen content may not
have loaded. The same deadline, profiles and cleanup rules apply.

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

Captures use a device scale factor of one and do not support
PDF output, custom request headers or profile state write-back. There is no
dedicated JavaScript execution endpoint; page scripts execute normally.

## Conditional GET and freshness

The server never retains PNG bytes. Unconditional GET requests always capture a
new image, including concurrent requests for identical conditions. POST also
always captures a new image and returns `Cache-Control: no-store`, without ETag
or Age; it ignores `If-None-Match` and never uses validation metadata.

GET ETags are quoted SHA-256 hashes of the actual PNG bytes. The server retains
only the PNG ETag, validation time, freshness policy and main document URL,
ETag/Last-Modified and freshness headers. Metadata retention is bounded by
`metadata_max_bytes` and `metadata_max_entries`. Expired entries can retain source
validators until eviction or the Redis retention deadline. Process restarts
discard memory metadata; Redis metadata can be reused by another instance.
Errors and responses marked `no-store` are not retained.

The metadata key contains URL (including fragments), viewport, full-page mode,
output dimensions,
the request's `ready_selector` and `delay_ms`, profile name, scale factor and
renderer schema. Omitted output dimensions and explicit viewport-sized values
share a key for viewport capture. Full-page mode keeps omitted output dimensions
distinct from explicit dimensions, since the document size is determined later.
Profile settings and bundle contents are fixed for the process.
Redis also isolates rendering settings, capture dimension limits and deployment
generations; see the
[operations guide](OPERATIONS.md#redis-metadata-cache).

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

Metadata is shared by authorized callers within the configured cache scope.
Responses remain private by default; deployments that need image caching can
configure an appropriately isolated reverse proxy. No image cache, capture
coalescing or client cache-bypass directive is implemented.
