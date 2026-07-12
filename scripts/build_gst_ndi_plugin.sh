#!/usr/bin/env bash
set -euo pipefail

GST_NDI_VERSION="${TELETOOL_GST_NDI_VERSION:-0.13.5}"
GST_NDI_SHA256="${TELETOOL_GST_NDI_SHA256:-ec8417e75002857f4c8e8fd2f2f1a7521937eaac3de264f7bb6904a0d22cba23}"
OUTPUT_PATH="${1:-dist/libgstndi.so}"
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
NOTICE_DIR="${TELETOOL_GST_NDI_NOTICES_DIR:-$OUTPUT_DIR/gst-plugin-ndi-licenses}"
SOURCE_OUTPUT="${TELETOOL_GST_NDI_SOURCE_ARCHIVE:-$OUTPUT_DIR/gst-plugin-ndi-$GST_NDI_VERSION.crate}"

for command_name in cargo curl python3 sha256sum tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing build command: $command_name" >&2
    exit 1
  }
done

build_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

archive="$build_dir/gst-plugin-ndi-$GST_NDI_VERSION.crate"
source_dir="$build_dir/gst-plugin-ndi-$GST_NDI_VERSION"

curl -A "TeleTool package builder" -fL \
  "https://static.crates.io/crates/gst-plugin-ndi/gst-plugin-ndi-$GST_NDI_VERSION.crate" \
  -o "$archive"
printf '%s  %s\n' "$GST_NDI_SHA256" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$build_dir"

[ -f "$source_dir/Cargo.toml" ] || {
  echo "GStreamer NDI source archive did not contain Cargo.toml" >&2
  exit 1
}

cargo build --locked --release --manifest-path "$source_dir/Cargo.toml"
plugin="$source_dir/target/release/libgstndi.so"
[ -f "$plugin" ] || {
  echo "GStreamer NDI build did not produce libgstndi.so" >&2
  exit 1
}

install -d "$(dirname "$OUTPUT_PATH")" "$(dirname "$SOURCE_OUTPUT")"
install -m 0644 "$plugin" "$OUTPUT_PATH"
install -m 0644 "$archive" "$SOURCE_OUTPUT"

metadata="$build_dir/cargo-metadata.json"
cargo metadata --locked --format-version 1 --manifest-path "$source_dir/Cargo.toml" \
  > "$metadata"

rm -rf -- "$NOTICE_DIR"
install -d "$NOTICE_DIR"
python3 - "$metadata" "$NOTICE_DIR" <<'PY'
import json
import re
import shutil
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

manifest = ["package\tversion\tdeclared licence\tsource"]
missing_notices = []
for package in sorted(metadata["packages"], key=lambda p: (p["name"].lower(), p["version"])):
    name = package["name"]
    version = package["version"]
    licence = package.get("license") or "Not declared in Cargo metadata"
    source = package.get("repository") or package.get("homepage") or package.get("source") or ""
    manifest.append(f"{name}\t{version}\t{licence}\t{source}")

    package_root = Path(package["manifest_path"]).parent
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", f"{name}-{version}")
    package_output = output_dir / safe_name
    notice_files = []
    declared_license_file = package.get("license_file")
    if declared_license_file:
        declared_path = Path(declared_license_file)
        if not declared_path.is_absolute():
            declared_path = package_root / declared_path
        if declared_path.is_file():
            notice_files.append(declared_path)
    for candidate in package_root.iterdir():
        upper_name = candidate.name.upper()
        if candidate.is_file() and upper_name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            notice_files.append(candidate)
    if notice_files:
        package_output.mkdir(parents=True, exist_ok=True)
        for notice_file in sorted(set(notice_files)):
            shutil.copy2(notice_file, package_output / notice_file.name)
    else:
        missing_notices.append(f"{name} {version} ({licence})")

(output_dir / "DEPENDENCIES.tsv").write_text("\n".join(manifest) + "\n", encoding="utf-8")
if missing_notices:
    raise SystemExit("Missing licence text for: " + ", ".join(missing_notices))
PY

echo "Built $OUTPUT_PATH"
echo "Saved corresponding source to $SOURCE_OUTPUT"
echo "Collected dependency notices in $NOTICE_DIR"
