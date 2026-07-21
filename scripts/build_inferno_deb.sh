#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$PROJECT_DIR/dist}"
PACKAGE_NAME="teletool-inferno"
PACKAGE_ARCH="arm64"
VERSION="${TELETOOL_PACKAGE_VERSION:-$(tr -d '\r\n' < "$PROJECT_DIR/VERSION")}"
VERSION="${VERSION#V}"
VERSION="${VERSION#v}"
INFERNO_REF="${TELETOOL_INFERNO_REF:-$(tr -d '\r\n' < "$PROJECT_DIR/INFERNO_REF")}"
STATIME_REF="${TELETOOL_STATIME_REF:-$(tr -d '\r\n' < "$PROJECT_DIR/STATIME_REF")}"
INFERNO_REPO="${TELETOOL_INFERNO_REPO:-https://github.com/teodly/inferno}"
STATIME_REPO="${TELETOOL_STATIME_REPO:-https://github.com/teodly/statime}"

if ! [[ "$VERSION" =~ ^[0-9][0-9A-Za-z.+:~-]*$ ]]; then
  echo "Invalid TeleTool Inferno package version: $VERSION" >&2
  exit 1
fi
if [ -z "$INFERNO_REF" ] || [ -z "$STATIME_REF" ]; then
  echo "Inferno and Statime refs are required." >&2
  exit 1
fi

for command_name in cargo dpkg dpkg-deb dpkg-architecture file git install tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing Inferno package build command: $command_name" >&2
    exit 1
  }
done

native_arch="$(dpkg --print-architecture)"
if [ "$native_arch" != "$PACKAGE_ARCH" ]; then
  echo "TeleTool Inferno packages must be built on ARM64; current architecture is $native_arch." >&2
  exit 1
fi

build_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

clone_checkout() {
  repo="$1"
  ref="$2"
  dest="$3"
  git clone --recurse-submodules "$repo" "$dest"
  git -C "$dest" checkout --detach "$ref"
  git -C "$dest" submodule update --init --recursive
}

if [ -n "${TELETOOL_INFERNO_SOURCE_DIR:-}" ]; then
  inferno_src="$(cd "$TELETOOL_INFERNO_SOURCE_DIR" && pwd)"
else
  inferno_src="$build_dir/inferno"
  clone_checkout "$INFERNO_REPO" "$INFERNO_REF" "$inferno_src"
fi

if [ -n "${TELETOOL_STATIME_SOURCE_DIR:-}" ]; then
  statime_src="$(cd "$TELETOOL_STATIME_SOURCE_DIR" && pwd)"
else
  statime_src="$build_dir/statime"
  clone_checkout "$STATIME_REPO" "$STATIME_REF" "$statime_src"
fi

cargo build --locked --release --manifest-path "$inferno_src/Cargo.toml" -p alsa_pcm_inferno
cargo build --locked --release --manifest-path "$statime_src/Cargo.toml" -p statime-linux --bin statime

inferno_lib="$inferno_src/target/release/libasound_module_pcm_inferno.so"
statime_bin="$statime_src/target/release/statime"
if [ ! -f "$inferno_lib" ]; then
  echo "Inferno ALSA PCM build output was not found: $inferno_lib" >&2
  exit 1
fi
if [ ! -f "$statime_bin" ]; then
  echo "Statime build output was not found: $statime_bin" >&2
  exit 1
fi
if ! file "$inferno_lib" | grep -Eq 'ELF 64-bit.*(ARM aarch64|aarch64)'; then
  echo "$inferno_lib is not an ARM64 ELF shared library." >&2
  file "$inferno_lib" >&2
  exit 1
fi
if ! file "$statime_bin" | grep -Eq 'ELF 64-bit.*(ARM aarch64|aarch64)'; then
  echo "$statime_bin is not an ARM64 ELF executable." >&2
  file "$statime_bin" >&2
  exit 1
fi

multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
package_root="$build_dir/${PACKAGE_NAME}_${VERSION}_${PACKAGE_ARCH}"
control_root="$package_root/DEBIAN"
doc_root="$package_root/usr/share/doc/teletool-inferno"

install -d \
  "$control_root" \
  "$package_root/etc/alsa/conf.d" \
  "$package_root/lib/systemd/system" \
  "$package_root/usr/lib/$multiarch/alsa-lib" \
  "$package_root/usr/lib/teletool-inferno/bin" \
  "$doc_root/licenses" \
  "$doc_root/source"

install -m 0644 "$inferno_lib" \
  "$package_root/usr/lib/$multiarch/alsa-lib/libasound_module_pcm_inferno.so"
install -m 0755 "$statime_bin" "$package_root/usr/lib/teletool-inferno/bin/statime-inferno"
install -m 0755 "$PROJECT_DIR/packaging/inferno/write-statime-config" \
  "$package_root/usr/lib/teletool-inferno/bin/write-statime-config"
install -m 0644 "$PROJECT_DIR/packaging/inferno/alsa-teletool-inferno.conf" \
  "$package_root/etc/alsa/conf.d/60-teletool-inferno.conf"
install -m 0644 "$PROJECT_DIR/packaging/inferno/teletool-inferno-clock.service" \
  "$package_root/lib/systemd/system/teletool-inferno-clock.service"

for maintainer_script in postinst prerm postrm; do
  install -m 0755 "$PROJECT_DIR/packaging/inferno/$maintainer_script" \
    "$control_root/$maintainer_script"
done

for inferno_doc in LICENSE LICENSE.GPL LICENSE.AGPL README.md; do
  if [ -f "$inferno_src/$inferno_doc" ]; then
    install -m 0644 "$inferno_src/$inferno_doc" "$doc_root/licenses/inferno-$inferno_doc"
  fi
done
for statime_doc in LICENSE-MIT LICENSE-APACHE COPYRIGHT README.md; do
  if [ -f "$statime_src/$statime_doc" ]; then
    install -m 0644 "$statime_src/$statime_doc" "$doc_root/licenses/statime-$statime_doc"
  fi
done

tar --exclude-vcs --exclude='./target' --exclude='./target/*' \
  -czf "$doc_root/source/inferno-${INFERNO_REF//\//_}.tar.gz" -C "$inferno_src" .
tar --exclude-vcs --exclude='./target' --exclude='./target/*' \
  -czf "$doc_root/source/statime-${STATIME_REF//\//_}.tar.gz" -C "$statime_src" .

cat > "$doc_root/BUILD_INFO" <<EOF
TeleTool Inferno package version: $VERSION
Inferno repository: $INFERNO_REPO
Inferno ref: $INFERNO_REF
Statime repository: $STATIME_REPO
Statime ref: $STATIME_REF
ALSA PCM name: teletool_inferno
Clock socket: /run/teletool-inferno/usrvclock.sock
EOF

cat > "$control_root/control" <<EOF
Package: teletool-inferno
Version: $VERSION
Architecture: arm64
Maintainer: TeleTool Project <teletool@localhost>
Section: sound
Priority: optional
Homepage: https://github.com/teodly/inferno
Depends: alsa-utils, iproute2, libc6, systemd
Recommends: teletool (= $VERSION)
Description: Inferno-AoIP companion package for TeleTool
 Provides a pinned upstream Inferno-AoIP ALSA PCM and the Inferno Statime
 clock service used by TeleTool's experimental Dante-compatible network
 audio output. Inferno-AoIP is unofficial and is not affiliated with or
 endorsed by Audinate.
X-TeleTool-Inferno-Ref: $INFERNO_REF
X-TeleTool-Statime-Ref: $STATIME_REF
EOF

cat > "$doc_root/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Inferno-AoIP and Statime Inferno fork
Upstream-Contact: Teodor Wozniak
Source: $INFERNO_REPO

Files: usr/lib/*/alsa-lib/libasound_module_pcm_inferno.so
Copyright: 2023-2026 Teodor Wozniak and Inferno-AoIP contributors
License: GPL-3.0-or-later or AGPL-3.0-or-later
 Inferno-AoIP is dual licensed under GPLv3-or-later and AGPLv3-or-later.
 The complete upstream licence texts are installed in
 /usr/share/doc/teletool-inferno/licenses/.

Files: usr/lib/teletool-inferno/bin/statime-inferno
Copyright: Statime contributors; Inferno fork changes by Teodor Wozniak
License: Apache-2.0 or MIT
 The complete upstream licence texts are installed in
 /usr/share/doc/teletool-inferno/licenses/.

Files: *
Copyright: 2026 John Lightfoot
License: TeleTool-Proprietary
 TeleTool packaging material is proprietary TeleTool material. This does not
 restrict rights granted by the upstream Inferno-AoIP or Statime licences.
EOF

(
  cd "$package_root"
  find . -path ./DEBIAN -prune -o -type f -print0 | sort -z | \
    xargs -0 md5sum > DEBIAN/md5sums
)

install -d "$OUTPUT_DIR"
deb_path="$OUTPUT_DIR/${PACKAGE_NAME}_${VERSION}_${PACKAGE_ARCH}.deb"
dpkg-deb --root-owner-group --build "$package_root" "$deb_path"
echo "Built $deb_path"
