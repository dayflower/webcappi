# webcappi

webcappi captures URLs or HTML as PNG images through an HTTP API. Set viewport
and output sizes independently, and optionally use browser profiles with
extensions or saved logins. Published Docker images include Chrome and fonts
for Linux amd64 and arm64.

> [!CAUTION]
> Use it locally or with trusted callers on an internal network.
> See [Intended use and security](#intended-use-and-security) for details.

## Quick start

Create a default configuration and start the published image with Docker:

```sh
printf '{}\n' > config.json
docker run --rm --name webcappi -p 127.0.0.1:8080:8080 \
  --read-only --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  ghcr.io/dayflower/webcappi:latest
```

In another terminal:

```sh
curl --get http://localhost:8080/capture \
  --data-urlencode url=https://example.com/ \
  --output capture.png
```

The server generates a clean profile automatically. This example has no API
authentication and binds to localhost; configure [authentication](#authentication)
before making it available to other callers. An API guide is also available at
`http://localhost:8080/`.

Chromium's sandbox is enabled by default. If startup reports `No usable sandbox`,
see the [host policy setup](deploy/sandbox/README.md).

## HTTP API

### Capture a URL

Use `GET /capture` with a URL and optional dimensions:

```sh
curl --get http://localhost:8080/capture \
  --data-urlencode url=https://example.com/ \
  --data-urlencode width=1200 \
  --data-urlencode height=800 \
  --data-urlencode output_width=600 \
  --data-urlencode output_height=600 \
  --output capture.png
```

This lays out the page at 1200 × 800, then resizes and center-crops the visible
viewport into a 600 × 600 PNG. To capture the full page at its natural size,
use `full_page=true` and omit the output dimensions as shown below.

### Capture the full page

```sh
curl --get http://localhost:8080/capture \
  --data-urlencode url=https://example.com/ \
  --data-urlencode full_page=true \
  --output full-page.png
```

The viewport still determines layout. Omitted output dimensions use the full
page dimensions; explicit output dimensions retain resizing and center cropping.
Omitted or empty `full_page` means `false`. This does not scroll to trigger lazy
loading or expand nested scroll containers. The complete source and output must
fit `max_width`, `max_height` and `max_pixels`, even when resizing down. The default
maximum height is 4096 pixels; increase the configured limits for longer pages.

### Capture HTML

Send an HTML file as the body of `POST /capture`:

```sh
curl http://localhost:8080/capture \
  --header 'Content-Type: text/html; charset=utf-8' \
  --data-binary @page.html \
  --output capture.png
```

The same size, profile and readiness parameters work on POST. Use absolute
resource URLs or `<base href="https://example.com/">` for relative paths.
HTML opens as a temporary file by default; use `?html_mode=loopback` to serve it
over local HTTP during capture.

### Parameters and responses

| Parameter | Purpose | Default |
| --- | --- | --- |
| `url` | HTTP(S) URL; required for GET, unavailable on POST | — |
| `width`, `height` | Viewport size in CSS pixels | `1280`, `720` |
| `output_width`, `output_height` | PNG dimensions after resizing and center cropping | Captured size |
| `full_page` | Capture the full scrollable page; empty means `false` | `false` |
| `profile` | Configured browser profile | `default` |
| `ready_selector` | Wait for an element matching this CSS selector | — |
| `delay_ms` | Extra wait after readiness checks, in milliseconds | `0` |
| `html_mode` | POST only: `file` or `loopback` | `file` |

Captures wait for page load, configured selectors, fonts and visible images.
Full-page capture uses the same waits; offscreen lazy images may remain unloaded.
Use `ready_selector` for application-specific readiness, such as content loaded
by JavaScript; `delay_ms` adds a fixed wait within the capture deadline.

Successful captures return `image/png`. Errors return JSON with an `error` field.
GET supports `ETag` / `If-None-Match`; the server retains validation metadata,
not PNG images. POST always creates a new capture.

See the [API reference](notes/API.md) for input limits, status codes, HTML
origins and encodings, readiness behavior and conditional GET semantics.

## Authentication

Add a shared Bearer token file to the configuration:

```json
{
  "auth": {
    "bearer_token_file": "/run/secrets/webcappi/token"
  }
}
```

Store your token in a file readable by container UID 10001, and add a read-only
mount to the Docker command:

```sh
-v /secure/path/webcappi-token:/run/secrets/webcappi/token:ro
```

Send the token with capture requests:

```sh
curl --get http://localhost:8080/capture \
  --header 'Authorization: Bearer YOUR_TOKEN' \
  --data-urlencode url=https://example.com/ \
  --output capture.png
```

Omitting `auth` disables authentication. `/`, `/healthz` and `/readyz` remain
public. Restart after changing the token; use TLS at your reverse proxy for
network access.

## Configuration

All settings are optional. For example, to run two captures concurrently with
a 60-second deadline:

```json
{
  "concurrency": 2,
  "capture_timeout_seconds": 60
}
```

| Setting | Default | Purpose |
| --- | --- | --- |
| `concurrency` | `1` | Active browser captures |
| `queue_size` | `8` | Pending captures |
| `capture_timeout_seconds` | `30` | Total deadline, including queueing |
| `max_width`, `max_height` | `4096` each | Maximum viewport and output dimensions |
| `max_pixels` | `8388608` | Maximum viewport or output area |
| `max_post_bytes` | `1048576` | HTML body size limit (1 MiB) |
| `resize_algorithm` | `bilinear` | Image resize filter |

Configuration changes require a restart. The
[operations guide](notes/OPERATIONS.md#configuration) lists all settings;
[config.example.json](config.example.json) includes authentication and a
prepared profile example.

## Optional features

### Profiles and extensions

Prepare a browser profile when you need extensions, browser settings or saved
logins. Each capture uses a disposable copy, leaving the template unchanged.
Follow the [profile guide](notes/PROFILES.md) to create, customize and mount a
bundle, then select it with `profile=NAME`.

### Single capture with Docker

The image also supports a CLI for URL captures without starting the API.
Using the `config.json` from the quick start:

```sh
docker run --name webcappi-capture --shm-size=512m --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  ghcr.io/dayflower/webcappi:latest webcappi capture \
  -config /etc/webcappi/config.json -url https://example.com/ -out /work/capture.png
docker cp webcappi-capture:/work/capture.png ./capture.png
docker rm webcappi-capture
```

Keep the stopped container until `docker cp` retrieves the PNG. Use
`webcappi capture -help` for available flags.

### Redis

Multiple instances can share validation metadata through Redis. See the
[Redis configuration](notes/OPERATIONS.md#redis-metadata-cache), including
generation changes when updating images or profiles.

### Kubernetes and Helm

Use the [Helm chart guide](charts/webcappi/README.md) for installation and values,
or the [operations guide](notes/OPERATIONS.md#kubernetes) for raw manifests.
`/healthz` and `/readyz` provide liveness and readiness probes.

For source builds and contributing, see the [development guide](notes/DEVELOP.md).

## Intended use and security

webcappi is intended for local use and controlled internal networks with trusted
callers. It is not designed for direct Internet exposure or as an anonymous
public screenshot service.

Chromium executes JavaScript from target pages and posted HTML. Pages, redirects,
scripts and other resources can make requests using the container's network
access, and a prepared profile may supply saved credentials. Callers can capture
internal or authenticated pages and receive their contents as screenshots.
Page scripts can also contact internal services or send accessible data to
external endpoints. Private addresses and Kubernetes Service DNS names are
supported intentionally; the service does not provide an SSRF prevention boundary.

Shared Bearer authentication controls access to the API. Every token holder can
capture any reachable site and use all configured profiles; there is no
per-caller authorization. Validation metadata is also shared within the
configured cache scope.

Captures can consume significant CPU, memory and network resources. A caller or
captured page can overload the host or generate heavy traffic toward other
services. Queue, concurrency and deadline limits do not bound all activity
inside Chromium. The server retains validation metadata rather than PNG data,
so unconditional GET and all HTML POST requests perform independent captures.

Restrict API access to trusted callers, apply ingress/egress policies and
container resource limits, and keep Chromium's sandbox enabled. Use an
authentication gateway when per-caller access control is needed. See the
[operations guide](notes/OPERATIONS.md) for deployment settings.

## License

Project-authored code and deployment policies use the [MIT License](LICENSE).
Bundled Chrome, system packages and other dependencies retain their own licenses.
