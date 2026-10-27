# Browser profiles and extensions

The default setup needs no prepared profile. Omit `profiles` (or use `{}`) and
webcappi generates an extension-free default at startup, even when other named
profiles are configured. An invalid configured `default` fails startup instead
of falling back. Generated templates are temporary and removed at shutdown.

Use a prepared profile to supply extensions, settings or saved logins. Every
capture clones the complete User Data Directory; it never modifies the template.

## Create a bundle

Using the published application image:

```sh
docker run --rm --shm-size=512m ghcr.io/dayflower/webcappi:latest \
  webcappi init-profile > default-profile.tar
mkdir -p bundles
tar -xf default-profile.tar -C bundles
```

Only extract the archive after `init-profile` succeeds. The command starts the
bundled browser on `about:blank`, closes it normally and exports its complete
User Data Directory. It needs no GUI, configuration file or existing profile.
The sandbox stays enabled and requires the same
[host support](OPERATIONS.md#browser-sandbox) as the server.

The extracted layout is:

```text
bundles/default/
  bundle.json
  extensions/
  user-data/Local State
  user-data/Default/Preferences
  user-data/...
```

`init-profile` writes only the tar archive to stdout and errors to stderr.
`-out profile.tar` writes to a container/local path and refuses to overwrite an
existing file. Native users can set `-browser-path` and `-work-dir`. Initialization
has a 30-second deadline and a default 512 MiB profile byte limit.

## Configure and mount a bundle

Set `profiles.default` to the descriptor in `bundles/default/bundle.json`:

```json
{
  "profiles": {
    "default": {
      "user_data_dir": "/bundles/default/user-data",
      "profile_directory": "Default",
      "extensions": []
    }
  }
}
```

Add this mount to the [quick-start command](../README.md#quick-start):

```sh
-v "$PWD/bundles:/bundles:ro"
```

Archive entries have private permissions and UID/GID 10001; extraction may assign
the host user's ownership. Grant runtime UID 10001 read/traverse access using
ownership or a narrowly scoped ACL. Keep credential-bearing profiles private.

`user_data_dir` is the complete User Data Directory, containing `Local State`
and `Default/`; do not point it at `Default/` itself. `bundle.json` must sit
beside `user-data` and match the configured descriptor exactly. Scratch and
template paths must not overlap. Profile copies reject special files and
symlinks, apart from obsolete top-level Chromium lock/endpoint files, which are
excluded.

For additional profiles, mount their bundles and add descriptors under other
names in `profiles`. Select one with the API's `profile` parameter or CLI
`-profile`. `profile_directory` names the individual Chromium profile inside
the User Data Directory, usually `Default`.

## Add extensions, settings or logins

The optional editing helper requires a repository checkout, Docker, Python 3,
and a Linux X11 desktop with `xauth`. Run from the repository root:

1. Add unpacked extensions under `bundles/default/extensions/<name>/`, each with
   a `manifest.json` containing a stable public `key`. Keep extension code
   separate from `user-data`.
2. Open a copy with the same application image used for captures:

   ```sh
   WEBCAPPI_IMAGE=ghcr.io/dayflower/webcappi:latest \
     bash scripts/edit-profile.sh bundles/default bundles/default-edited
   ```

   Set `PLATFORM=linux/arm64` or `linux/amd64` if needed. The source bundle is
   unchanged and the output directory must not already exist.
3. In Chrome, enable **Developer mode** and **Load unpacked** from
   `/bundles/default/extensions/<name>`. Finish permissions, onboarding and
   logins. For HTML file captures, enable **Allow access to file URLs** and
   content-script matches for `file:` URLs.
4. Use Chrome's **Exit** command to flush its databases. The helper saves the
   edited data and generates `bundle.json`. If the page or extension supplies
   readiness signals, add `ready_selectors` to both the descriptor and config.
5. Deploy the edited bundle at `/bundles/default`, update `profiles.default`
   to match, and verify captures. The staging directory's name does not change
   extension loading paths.

Preserve an existing extension's manifest public `key` to retain its ID, and
keep the same absolute loading paths during preparation and capture. No private
signing key is needed. The server checks saved registration, enabled state,
public keys and exact paths before accepting requests. It reuses the saved
installation instead of installing extensions for each capture.

Repeated launches should verify that each extension works without interactive
UI and provides the expected readiness signals. Extensions can still open
onboarding tabs or depend on remote state. webcappi always captures an explicitly
created page, rather than an extension's welcome tab.

## Validate and update bundles

Prepare and qualify bundles with the production architecture and application
image. For example, use `docker run --platform linux/arm64 ...` when preparing
an ARM64 bundle. Keep architecture-specific bundles in separate directories.
Windows/macOS profiles, other Chrome brands/versions, native messaging and
encrypted credentials may not transfer; verify representative pages and logins.
For interactive editing from other hosts, use a Linux VM or remote Linux desktop
with the same application image.

Validate a mounted configuration before serving:

```sh
docker run --rm --read-only --shm-size=512m \
  --tmpfs /work:uid=10001,gid=10001,mode=700 --tmpfs /tmp \
  -v "$PWD/config.json:/etc/webcappi/config.json:ro" \
  -v "$PWD/bundles:/bundles:ro" \
  ghcr.io/dayflower/webcappi:latest webcappi check -config /etc/webcappi/config.json
```

If authentication is configured, also mount its token file. `check` validates
the configuration and performs a real browser launch on each profile clone.

To update, stage a copy, edit it with the target image, close Chrome normally,
then keep the descriptor/configuration in sync and qualify captures. Retain the
previous image and bundle for rollback. Never edit a mounted live template or
promote the mutated data from a capture. When using Redis, change
[`cache.redis.generation`](OPERATIONS.md#redis-metadata-cache) whenever image,
font, extension or bundle contents change.

For Kubernetes, the [chart guide](../charts/webcappi/README.md#profiles) describes
using a profile artifact image and an initContainer. Building that artifact is
covered in the [development guide](DEVELOP.md#profile-artifact-images).
