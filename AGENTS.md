# AGENTS.md

See `README.md` for configuration and operating procedures, `notes/DEVELOP.md`
for architecture and validation, and `notes/BROWSER.md` for browser details.

## Constraints

- Support Linux amd64 and arm64 with the same pinned full Chrome for Testing
  version. Preserve extension support in unified headless; no runtime downloads.
- Keep the browser sandbox enabled by default. Allow an explicit operator
  opt-out through `no_sandbox` / Helm `browser.noSandbox`; never disable it
  automatically. Record sandbox-enabled and sandbox-disabled validation separately.
- Clone the complete User Data Directory per capture; never modify the template.
  Keep extension code separate, preserve IDs/paths and reuse its installation.
  Capture an explicitly created page, not an extension's welcome tab.
- Bound concurrency, queues and the metadata cache; never cache PNG bytes.
  Preserve deadlines and browser/file cleanup on failure or cancellation. Wait for selectors and fonts/images.
- Include URL, viewport, request readiness selector and profile name in cache keys.
  Preserve TTL/Age and content-based ETags. After metadata expiry, validate the
  source or regenerate before returning 304.
- Private URLs and Kubernetes Service DNS names are intentional targets.
- Verify font selection through Chromium/CDP, not only `fc-match`.

## Validation

Use the Go toolchain pinned in `Dockerfile`. Run checks relevant to the change:

- Go: `make test` and `go vet ./...`.
- Browser, profile, extension, font or fixture: `make image`,
  `make browser-test`, then `python3 scripts/container-smoke.py`.
- Build: `make binaries` and `make image-multi` for both architectures on a
  suitable BuildKit builder. See `notes/DEVELOP.md` for platform/artifact settings.

`make test` alone skips real-browser tests. Distinguish build verification from
runtime verification, and report untested platforms explicitly.

## Git commit attribution

- Use Conventional Commits: `<type>: <description>`, without a scope.
- Use a single-line title and no body or footer except co-author trailers.
