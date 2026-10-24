#!/usr/bin/env bash
# Optional GUI editing of an existing bundle. The source is never modified.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DISPLAY:?Set DISPLAY to a Linux X11 display for interactive editing}"
command -v xauth >/dev/null
command -v python3 >/dev/null
image="${WEBCAPPI_IMAGE:-webcappi:dev}"
platform_args=()
if [[ -n "${PLATFORM:-}" ]]; then
  case "$PLATFORM" in
    linux/amd64|linux/arm64) platform_args=(--platform "$PLATFORM") ;;
    *) echo "Unsupported platform: $PLATFORM" >&2; exit 1 ;;
  esac
fi
source_bundle="${1:-bundles/default}"
destination="${2:-bundles/default-edited}"
if [[ -e "$destination" ]]; then
  echo 'Refusing to overwrite an existing output bundle.' >&2
  exit 1
fi
python3 - "$source_bundle" <<'PY'
import json
from pathlib import Path
import sys
source = Path(sys.argv[1])
profile = json.loads((source / "bundle.json").read_text())
if profile["user_data_dir"] != "/bundles/default/user-data" or profile["profile_directory"] != "Default":
    raise SystemExit("This helper edits default bundles created by init-profile.")
for path in source.joinpath("extensions").glob("*/manifest.json"):
    if not json.loads(path.read_text()).get("key"):
        raise SystemExit(f"A stable manifest key is required: {path}")
PY
mkdir -p "$(dirname "$destination")"
staging="$(mktemp -d "$(dirname "$destination")/.webcappi-edit-XXXXXX")"
auth=""
container="webcappi-edit-$$"
trap 'if [[ -n "$auth" ]]; then rm -f "$auth"; fi; if [[ -n "$staging" ]]; then rm -rf "$staging"; fi; docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
auth="$(mktemp)"
# Copy a scoped X11 cookie instead of changing the desktop's xhost policy.
xauth nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$auth" nmerge -
docker create "${platform_args[@]}" --name "$container" --shm-size=512m \
  -e DISPLAY -e XAUTHORITY=/tmp/xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  "$image" sleep infinity >/dev/null
docker start "$container" >/dev/null
docker exec --user 0 "$container" mkdir -p /work/default /bundles/default/extensions
docker cp "$auth" "$container:/tmp/xauthority"
docker cp "$source_bundle/user-data" "$container:/work/default/user-data"
docker cp "$source_bundle/extensions/." "$container:/bundles/default/extensions/"
docker exec --user 0 "$container" chown -R 10001:10001 /work/default /bundles/default /tmp/xauthority
echo 'Load your unpacked extensions from /bundles/default/extensions/<name>.'
echo 'Finish settings/logins, then use Chrome > Exit to save the profile.'
docker exec --user 10001:10001 "$container" /opt/chrome/chrome \
  --user-data-dir=/work/default/user-data --profile-directory=Default \
  --no-first-run --no-default-browser-check --password-store=basic chrome://extensions/
docker cp "$container:/work/default/user-data" "$staging/user-data"
docker cp "$container:/bundles/default/extensions" "$staging/extensions"
python3 - "$source_bundle/bundle.json" "$staging" <<'PY'
import json
from pathlib import Path
import sys
profile = json.loads(Path(sys.argv[1]).read_text())
destination = Path(sys.argv[2])
profile["extensions"] = [
    f"/bundles/default/extensions/{path.parent.name}"
    for path in sorted(destination.joinpath("extensions").glob("*/manifest.json"))
]
(destination / "bundle.json").write_text(json.dumps(profile, indent=2) + "\n")
PY
if [[ -e "$destination" ]]; then
  echo 'Output bundle appeared during editing; refusing to overwrite it.' >&2
  exit 1
fi
mv -T "$staging" "$destination"
staging=""
echo "Saved $destination. Deploy it at /bundles/default and update profiles.default to match bundle.json."
