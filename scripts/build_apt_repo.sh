#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEB_DIR="${1:-$PROJECT_DIR/dist}"
REPO_DIR="${2:-$PROJECT_DIR/apt-repo}"
SUITE="${TELETOOL_APT_SUITE:-stable}"
COMPONENT="main"
ARCH="arm64"
INSTALLER_VERSION="${TELETOOL_INSTALLER_VERSION:-$(tr -d '\r\n' < "$PROJECT_DIR/INSTALLER_VERSION")}"

if ! [[ "$INSTALLER_VERSION" =~ ^[0-9]+([.][0-9]+)*$ ]]; then
  echo "Invalid TeleTool installer version: $INSTALLER_VERSION" >&2
  exit 1
fi

for command_name in apt-ftparchive dpkg-deb dpkg-scanpackages gzip install tail xz; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing repository build command: $command_name" >&2
    exit 1
  }
done

if [ -e "$REPO_DIR" ] && [ -n "$(find "$REPO_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
  echo "Repository output directory must be empty: $REPO_DIR" >&2
  exit 1
fi

mapfile -t debs < <(find "$DEB_DIR" -maxdepth 1 -type f -name '*_arm64.deb' -print | sort)
if [ "${#debs[@]}" -eq 0 ]; then
  echo "No TeleTool ARM64 .deb files found in $DEB_DIR" >&2
  exit 1
fi

packages_dir="$REPO_DIR/dists/$SUITE/$COMPONENT/binary-$ARCH"
install -d "$packages_dir"
for deb in "${debs[@]}"; do
  package_name="$(dpkg-deb -f "$deb" Package)"
  case "$package_name" in
    teletool|teletool-inferno) ;;
    *)
      echo "Unexpected package in repository build: $package_name ($deb)" >&2
      exit 1
      ;;
  esac
  pool_dir="$REPO_DIR/pool/main/t/$package_name"
  install -d "$pool_dir"
  install -m 0644 "$deb" "$pool_dir/$(basename "$deb")"
done

(
  cd "$REPO_DIR"
  dpkg-scanpackages --arch "$ARCH" pool /dev/null > \
    "dists/$SUITE/$COMPONENT/binary-$ARCH/Packages"
)
gzip -n -9 -c "$packages_dir/Packages" > "$packages_dir/Packages.gz"
xz -9e -c "$packages_dir/Packages" > "$packages_dir/Packages.xz"

release_dir="$REPO_DIR/dists/$SUITE"
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="TeleTool" \
  -o APT::FTPArchive::Release::Label="TeleTool" \
  -o APT::FTPArchive::Release::Suite="$SUITE" \
  -o APT::FTPArchive::Release::Codename="$SUITE" \
  -o APT::FTPArchive::Release::Architectures="$ARCH" \
  -o APT::FTPArchive::Release::Components="$COMPONENT" \
  -o APT::FTPArchive::Release::Description="TeleTool Raspberry Pi packages" \
  release "$release_dir" > "$release_dir/Release"

if [ -n "${TELETOOL_GPG_KEY:-}" ]; then
  command -v gpg >/dev/null 2>&1 || {
    echo "gpg is required to sign the repository" >&2
    exit 1
  }
  gpg --batch --yes --local-user "$TELETOOL_GPG_KEY" \
    --digest-algo SHA256 --clearsign \
    -o "$release_dir/InRelease" "$release_dir/Release"
  gpg --batch --yes --local-user "$TELETOOL_GPG_KEY" \
    --digest-algo SHA256 --armor --detach-sign \
    -o "$release_dir/Release.gpg" "$release_dir/Release"
  gpg --batch --yes --export "$TELETOOL_GPG_KEY" > \
    "$REPO_DIR/teletool-archive-keyring.gpg"
else
  echo "WARNING: TELETOOL_GPG_KEY is unset; repository metadata is unsigned." >&2
fi

{
  sed "s/__INSTALLER_VERSION__/$INSTALLER_VERSION/g" \
    "$PROJECT_DIR/packaging/debian/terminal-ui"
  tail -n +2 "$PROJECT_DIR/packaging/apt/install.sh"
} > "$REPO_DIR/install.sh"
chmod 0755 "$REPO_DIR/install.sh"
echo "Built APT repository at $REPO_DIR"
