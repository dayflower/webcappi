# Browser implementation and qualification

This document records browser choices and observed behavior for maintainers.
See the [README](../README.md) for capture examples, the [profile guide](PROFILES.md)
for preparing browser data, the [operations guide](OPERATIONS.md) for deployment,
and the [development guide](DEVELOP.md) for build and regression commands.

## Browser selection

Both supported architectures use full Chrome for Testing **156.0.8078.47**:
`linux64` for amd64 and `linux-arm64` for arm64. Startup checks the brand and
version. The browser is downloaded only at image build time; captures do not
install or update it. ARM64 downloads are listed by
[Chrome for Testing](https://github.com/GoogleChromeLabs/chrome-for-testing#supported-platforms).
The Go browser adapter uses chromedp **v0.21.0**.

Use the full browser, not `chrome-headless-shell`. Unified headless shares the
normal browser implementation and supports extensions. Ordinary branded Chrome
has removed `--load-extension`; Chromium and Chrome for Testing retain it.
Sources: [headless modes](https://developer.chrome.com/docs/automation-and-testing/headless),
[extension testing](https://developer.chrome.com/docs/extensions/how-to/test/end-to-end-testing),
[Chrome extension flag announcement](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/my-groups),
[version availability](https://googlechromelabs.github.io/chrome-for-testing/).

## Allocator and process cleanup

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

Each capture has private HOME, XDG configuration/cache and a complete profile
clone. Browser temporary files use a separate private `/tmp/wc-*` directory;
the short path avoids Chromium's Unix socket path-length limit. Remove this
scratch only after the browser and its descendants have exited.

## Persisted extension installation

**Do not use `--load-extension` for each capture.** Our local fixture demonstrated
that this command-line installation repeats the initial installation event on
cloned profiles. CDP `Extensions.loadUnpacked` also did not provide the desired
restart behavior in this browser. Instead, install through the visible browser's
**Developer mode → Load unpacked** flow once and let Chromium reload its saved
registration at startup. Runtime does not pass an installation flag or call an
installation API. The real-browser test exercises the native folder picker.

The fixture deliberately opens welcome and unrelated foreground tabs. Captures
must create their own target and bring it forward before the final asset/frame
check. Do not substitute the browser's initial or active tab. Repeat headless
launches verify the saved extension ID, absolute loading path, storage and
installation state, plus a pixel marker in the captured page. These findings
qualify the included Manifest V3 fixture, not every third-party extension.

## Font selection

The image explicitly installs `fonts-noto-core`, `fonts-noto-cjk`,
`fonts-noto-extra`, `fonts-noto-color-emoji`, `fonts-noto-mono`,
`fonts-roboto-unhinted`, and `fonts-liberation`.
Core supplies script-specific families, extra supplies additional weights and
widths, and Liberation provides metric-compatible alternatives for common web
fonts. Fontconfig uses the Debian/package defaults without custom family
overrides. This does not guarantee coverage of every Unicode character or
language; qualify representative text in Chromium.

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

The 2026-10-24 CDP checks also observed Noto families for representative Japanese,
simplified/traditional Chinese, Korean, Arabic, Hebrew, Devanagari, Thai,
Vietnamese, Myanmar and Khmer strings, and Noto Color Emoji for representative
emoji. Those observations do not establish full script or Unicode coverage.

## Browser fixtures and qualification boundaries

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
compared against reference images. See the maintained
[font tests](../internal/capture/fonts_test.go) for the CDP assertions.

The maintained browser tests cover headed installation through the native
folder picker, repeated independent headless clones, immutable templates,
explicit target selection, selectors and fonts/images/frames, HTML file and
loopback modes and secure-context status, charset handling, conditional main
navigation, extra delay, resizing, and browser/file cleanup after failure or
cancellation. Font selection is checked with CDP, not only Fontconfig.

Keep the production sandbox enabled by default. The disposable test containers
explicitly opt out on hosts whose policy does not allow Chromium's sandbox;
passing those tests does not qualify sandbox-enabled deployment. Real
third-party extensions, native messaging, cross-platform profile migration and
manual X11/Wayland forwarding need their own environment-specific checks.
See the [recorded validation scope](DEVELOP.md#recorded-validation-scope) before
claiming platform coverage.
