#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$PROJECT_DIR/dist}"
PACKAGE_NAME="teletool"
PACKAGE_ARCH="arm64"
VERSION="${TELETOOL_PACKAGE_VERSION:-$(tr -d '\r\n' < "$PROJECT_DIR/VERSION")}"
VERSION="${VERSION#V}"
VERSION="${VERSION#v}"

for command_name in dpkg dpkg-deb dpkg-architecture file install sed; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing package build command: $command_name" >&2
    exit 1
  }
done

native_arch="$(dpkg --print-architecture)"
if [ "$native_arch" != "$PACKAGE_ARCH" ]; then
  echo "TeleTool packages must be built on ARM64; current architecture is $native_arch." >&2
  exit 1
fi

plugin_source="${TELETOOL_GST_NDI_PLUGIN:-}"
if [ -z "$plugin_source" ] && command -v pkg-config >/dev/null 2>&1; then
  plugin_dir="$(pkg-config --variable=pluginsdir gstreamer-1.0 2>/dev/null || true)"
  if [ -n "$plugin_dir" ] && [ -f "$plugin_dir/libgstndi.so" ]; then
    plugin_source="$plugin_dir/libgstndi.so"
  fi
fi
if [ -z "$plugin_source" ] || [ ! -f "$plugin_source" ]; then
  echo "Set TELETOOL_GST_NDI_PLUGIN to a built ARM64 libgstndi.so." >&2
  exit 1
fi
if ! file "$plugin_source" | grep -Eq 'ELF 64-bit.*(ARM aarch64|aarch64)'; then
  echo "$plugin_source is not an ARM64 ELF shared library." >&2
  file "$plugin_source" >&2
  exit 1
fi

multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
build_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

package_root="$build_dir/${PACKAGE_NAME}_${VERSION}_${PACKAGE_ARCH}"
app_root="$package_root/usr/lib/teletool"
control_root="$package_root/DEBIAN"

install -d \
  "$control_root" \
  "$app_root/bin" \
  "$app_root/static" \
  "$package_root/etc/sudoers.d" \
  "$package_root/lib/systemd/system" \
  "$package_root/usr/lib/$multiarch/gstreamer-1.0" \
  "$package_root/usr/share/doc/teletool"

for source_file in app.py gst_base.py gst_ndi.py tvh.py config.example.json VERSION; do
  install -m 0644 "$PROJECT_DIR/$source_file" "$app_root/$source_file"
done
cp -a "$PROJECT_DIR/static/." "$app_root/static/"
install -m 0644 "$PROJECT_DIR/README.md" "$package_root/usr/share/doc/teletool/README.md"
install -m 0644 "$PROJECT_DIR/API.md" "$package_root/usr/share/doc/teletool/API.md"

cat > "$app_root/.teletool_release.json" <<JSON
{
  "branch": "main",
  "label": "APT package",
  "development": false
}
JSON
chmod 0644 "$app_root/.teletool_release.json"

sed "s/__VERSION__/$VERSION/g" \
  "$PROJECT_DIR/packaging/debian/control.in" > "$control_root/control"
for maintainer_script in postinst prerm postrm; do
  install -m 0755 "$PROJECT_DIR/packaging/debian/$maintainer_script" \
    "$control_root/$maintainer_script"
done

install -m 0644 "$PROJECT_DIR/packaging/debian/teletool.service" \
  "$package_root/lib/systemd/system/teletool.service"
install -m 0440 "$PROJECT_DIR/packaging/debian/teletool.sudoers" \
  "$package_root/etc/sudoers.d/teletool"
install -m 0755 "$PROJECT_DIR/packaging/debian/configure-tvheadend" \
  "$app_root/bin/configure-tvheadend"
install -m 0644 "$PROJECT_DIR/packaging/debian/terminal-ui" \
  "$app_root/bin/terminal-ui"
sed 's|__NDI_DROP_PATH__|/var/lib/teletool/libndi.so.6|g' \
  "$PROJECT_DIR/deploy/ndi/teletool-install-ndi-runtime" > \
  "$app_root/bin/install-ndi-runtime"
chmod 0755 "$app_root/bin/install-ndi-runtime"
install -m 0644 "$plugin_source" \
  "$package_root/usr/lib/$multiarch/gstreamer-1.0/libgstndi.so"

(
  cd "$package_root"
  find . -path ./DEBIAN -prune -o -type f -print0 | sort -z | \
    xargs -0 md5sum > DEBIAN/md5sums
)

install -d "$OUTPUT_DIR"
deb_path="$OUTPUT_DIR/${PACKAGE_NAME}_${VERSION}_${PACKAGE_ARCH}.deb"
dpkg-deb --root-owner-group --build "$package_root" "$deb_path"
echo "Built $deb_path"
