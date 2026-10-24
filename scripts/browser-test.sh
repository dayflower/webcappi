#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
platform="${PLATFORM:-$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')}"
case "$platform" in
  linux/amd64|linux/arm64) ;;
  *) echo "Unsupported test platform: $platform" >&2; exit 1 ;;
esac
arch="${platform#linux/}"
image="webcappi-test-browser:156.0.8078.47-$arch"
artifacts="${WEBCAPPI_ARTIFACTS_DIR:-artifacts}"
mkdir -p "$artifacts"
artifacts="$(cd "$artifacts" && pwd)"
docker buildx build --load --platform "$platform" --target test-browser -t "$image" .
CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go test -c ./internal/capture -o "$artifacts/capture.test"
container="webcappi-browser-test-$$"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
# Writable image layer is only used to export synthetic test data and images.
docker run --platform "$platform" --name "$container" --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$artifacts/capture.test:/app/capture.test:ro" \
  -v "$PWD:/src:ro" -w /src/internal/capture \
  -e WEBCAPPI_TEST_BROWSER=/opt/chrome/chrome -e WEBCAPPI_TEST_NO_SANDBOX=1 \
  -e WEBCAPPI_TEST_ARTIFACTS=/home/webcappi \
  "$image" \
  xvfb-run -a /app/capture.test -test.v -test.timeout=180s
docker cp "$container:/home/webcappi/extension.png" "$artifacts/extension.png"
docker cp "$container:/home/webcappi/html.png" "$artifacts/html.png"
docker cp "$container:/home/webcappi/html-loopback.png" "$artifacts/html-loopback.png"
if [[ -d "$artifacts/user-data" ]]; then
  echo "Remove $artifacts/user-data before exporting another synthetic template." >&2
else
  docker cp "$container:/home/webcappi/user-data" "$artifacts/user-data"
fi
