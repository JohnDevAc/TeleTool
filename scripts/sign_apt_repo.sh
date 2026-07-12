#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <repository-directory> <suite> <expected-package-version>" >&2
  exit 2
fi

REPO_DIR="$(cd "$1" && pwd)"
SUITE="$2"
EXPECTED_VERSION="$3"
EXPECTED_FINGERPRINT="${TELETOOL_APT_GPG_FINGERPRINT:-}"
PRIVATE_KEY="${TELETOOL_APT_GPG_PRIVATE_KEY:-}"

if ! [[ "$SUITE" =~ ^[a-z0-9][a-z0-9.-]*$ ]]; then
  echo "Invalid APT suite: $SUITE" >&2
  exit 1
fi
if [ -z "$EXPECTED_VERSION" ]; then
  echo "Expected package version is required" >&2
  exit 1
fi
if [ -z "$EXPECTED_FINGERPRINT" ]; then
  echo "TELETOOL_APT_GPG_FINGERPRINT is required" >&2
  exit 1
fi
if [ -z "$PRIVATE_KEY" ]; then
  echo "TELETOOL_APT_GPG_PRIVATE_KEY is required" >&2
  exit 1
fi

for command_name in awk dpkg-deb gpg gpgv grep mktemp sha256sum; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing APT publication command: $command_name" >&2
    exit 1
  }
done

release_dir="$REPO_DIR/dists/$SUITE"
release_file="$release_dir/Release"
packages_file="$release_dir/main/binary-arm64/Packages"
test -f "$release_file"
test -f "$packages_file"
grep -qx "Suite: $SUITE" "$release_file"
grep -qx "Codename: $SUITE" "$release_file"
grep -qx "Architectures: arm64" "$release_file"

package_count="$(grep -c '^Package: teletool$' "$packages_file")"
if [ "$package_count" -ne 1 ]; then
  echo "Expected exactly one TeleTool package, found $package_count" >&2
  exit 1
fi

package_version="$(awk '$1 == "Version:" {print $2; exit}' "$packages_file")"
package_filename="$(awk '$1 == "Filename:" {print $2; exit}' "$packages_file")"
package_sha256="$(awk '$1 == "SHA256:" {print $2; exit}' "$packages_file")"
if [ "$package_version" != "$EXPECTED_VERSION" ]; then
  echo "Unexpected package version: $package_version (expected $EXPECTED_VERSION)" >&2
  exit 1
fi
case "$package_filename" in
  pool/*) ;;
  *) echo "Unsafe package filename: $package_filename" >&2; exit 1 ;;
esac
case "$package_filename" in
  *..*|/*) echo "Unsafe package filename: $package_filename" >&2; exit 1 ;;
esac

package_path="$REPO_DIR/$package_filename"
test -f "$package_path"
if [ "$(dpkg-deb -f "$package_path" Package)" != "teletool" ]; then
  echo "Repository package is not TeleTool" >&2
  exit 1
fi
if [ "$(dpkg-deb -f "$package_path" Version)" != "$EXPECTED_VERSION" ]; then
  echo "Debian package version does not match the repository index" >&2
  exit 1
fi
if [ "$(dpkg-deb -f "$package_path" Architecture)" != "arm64" ]; then
  echo "Debian package is not ARM64" >&2
  exit 1
fi
if [ "$(sha256sum "$package_path" | awk '{print $1}')" != "$package_sha256" ]; then
  echo "Debian package SHA-256 does not match the repository index" >&2
  exit 1
fi

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
chmod 0700 "$GNUPGHOME"
cleanup() {
  rm -rf "$GNUPGHOME"
}
trap cleanup EXIT INT TERM

printf '%s' "$PRIVATE_KEY" | gpg --batch --import
fingerprint="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')"
if [ "$fingerprint" != "$EXPECTED_FINGERPRINT" ]; then
  echo "Unexpected APT signing fingerprint: $fingerprint" >&2
  exit 1
fi

rm -f "$release_dir/InRelease" "$release_dir/Release.gpg" \
  "$REPO_DIR/teletool-archive-keyring.gpg"
gpg --batch --yes --local-user "$fingerprint" --digest-algo SHA256 \
  --clearsign -o "$release_dir/InRelease" "$release_file"
gpg --batch --yes --local-user "$fingerprint" --digest-algo SHA256 \
  --armor --detach-sign -o "$release_dir/Release.gpg" "$release_file"
gpg --batch --yes --export "$fingerprint" > \
  "$REPO_DIR/teletool-archive-keyring.gpg"

gpgv --keyring "$REPO_DIR/teletool-archive-keyring.gpg" \
  "$release_dir/Release.gpg" "$release_file"
gpgv --keyring "$REPO_DIR/teletool-archive-keyring.gpg" \
  "$release_dir/InRelease"

echo "Signed TeleTool $SUITE repository for $EXPECTED_VERSION with $fingerprint"
