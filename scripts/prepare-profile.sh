#!/usr/bin/env bash
# Prepare the template using the production Linux browser, never a desktop's
# unrelated personal browser. Requires a Linux X11 desktop and xauth.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DISPLAY:?Set DISPLAY to a local X11 display}"
command -v xauth >/dev/null
image="${WEBCAPPI_IMAGE:-webcappi:dev}"
platform_args=()
if [[ -n "${PLATFORM:-}" ]]; then
  case "$PLATFORM" in
    linux/amd64|linux/arm64) platform_args=(--platform "$PLATFORM") ;;
    *) echo "Unsupported platform: $PLATFORM" >&2; exit 1 ;;
  esac
fi
destination="${1:-bundles/default}"
if [[ -e "$destination/user-data" ]]; then
  echo 'Refusing to overwrite a prepared User Data Directory.' >&2
  exit 1
fi
mkdir -p "$destination/extensions"
if [[ ! -d "$destination/extensions/verification" ]]; then
  cp -R testdata/extension "$destination/extensions/verification"
fi
destination="$(cd "$destination" && pwd)"
auth="$(mktemp)"
container="webcappi-prepare-$$"
trap 'rm -f "$auth"; docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
# FamilyWild makes a copied cookie usable with the container hostname. The
# cookie stays mode 0600; no xhost access-control changes are made.
xauth nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$auth" nmerge -
docker create "${platform_args[@]}" --name "$container" --shm-size=512m \
  -e DISPLAY -e XAUTHORITY=/tmp/xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  -v "$destination/extensions/verification:/bundles/default/extensions/verification:ro" \
  "$image" sleep infinity >/dev/null
docker cp "$auth" "$container:/tmp/xauthority"
docker start "$container" >/dev/null
docker exec --user 0 "$container" chown 10001:10001 /tmp/xauthority
flags=(--user-data-dir=/work/default/user-data --profile-directory=Default \
  --no-first-run --no-default-browser-check --password-store=basic)
if [[ "${WEBCAPPI_NO_SANDBOX:-0}" == 1 ]]; then flags+=(--no-sandbox); fi
echo 'Enable Developer mode, Load unpacked /bundles/default/extensions/verification,'
echo 'finish extension setup, close welcome tabs, then choose Chrome > Exit.'
docker exec --user 10001:10001 "$container" /opt/chrome/chrome "${flags[@]}" chrome://extensions/
docker cp "$container:/work/default/user-data" "$destination/user-data"
cat > "$destination/bundle.json" <<'JSON'
{
  "user_data_dir": "/bundles/default/user-data",
  "profile_directory": "Default",
  "extensions": ["/bundles/default/extensions/verification"],
  "ready_selectors": ["#webcappi-extension-ready"]
}
JSON
echo "Prepared $destination. Keep bundle.json, extensions and user-data together."
