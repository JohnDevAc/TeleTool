#!/usr/bin/env bash
set -euo pipefail

GST_NDI_VERSION="${TELETOOL_GST_NDI_VERSION:-0.13.5}"
GST_NDI_SHA256="${TELETOOL_GST_NDI_SHA256:-ec8417e75002857f4c8e8fd2f2f1a7521937eaac3de264f7bb6904a0d22cba23}"
OUTPUT_PATH="${1:-dist/libgstndi.so}"

for command_name in cargo curl sha256sum tar; do
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

install -d "$(dirname "$OUTPUT_PATH")"
install -m 0644 "$plugin" "$OUTPUT_PATH"
echo "Built $OUTPUT_PATH"
