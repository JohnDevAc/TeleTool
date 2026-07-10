#!/bin/sh
set -eu

# build_apt_repo.sh prepends TeleTool's terminal UI helpers. Keep plain-text
# fallbacks so this source file is also safe to run directly during development.
if ! command -v tt_ui_init >/dev/null 2>&1; then
  TT_UI_YELLOW=""
  TT_UI_RESET=""
  TT_UI_LAST_PERCENT=0
  tt_ui_init() { :; }
  tt_ui_reset() { :; }
  tt_ui_stage() { printf '\n==> [%s/%s] %s\n' "$1" "$2" "$3"; }
  tt_ui_progress() { printf '\n==> [%s%%] %s\n' "$1" "$2"; }
  tt_ui_completion_header() { printf '\nTELETOOL READY\n\n'; }
  tt_ui_web_link() { printf '\nOPEN THIS ADDRESS IN A WEB BROWSER\n\n  %s\n\n' "$1"; }
  tt_ui_failure() { printf '\nINSTALLATION STOPPED: %s\n' "$1" >&2; }
  tt_ui_status() { printf '  [%s] %s\n' "$1" "$2"; }
fi

REPOSITORY_URL="${TELETOOL_REPOSITORY_URL:-https://johndevac.github.io/teletwat/apt-repo}"
KEYRING="/usr/share/keyrings/teletool-archive-keyring.gpg"
SOURCE_FILE="/etc/apt/sources.list.d/teletool.sources"
LOG_FILE="/var/log/teletool-installer.log"

run_apt_with_progress() {
  download_start="$1"
  download_span="$2"
  install_start="$3"
  install_span="$4"
  phase_label="$5"
  display_message="$6"
  shift 6

  progress_dir="$(mktemp -d /tmp/teletool-apt-progress.XXXXXX)"
  progress_pipe="$progress_dir/status"
  progress_result="$progress_dir/result"
  mkfifo "$progress_pipe"

  (
    set +e
    "$@" 2>"$progress_pipe" >>"$LOG_FILE"
    printf '%s\n' "$?" >"$progress_result"
    exit 0
  ) &
  progress_pid=$!

  while IFS=: read -r progress_kind progress_item progress_percent progress_description; do
    progress_whole="${progress_percent%%.*}"
    case "$progress_whole" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$progress_whole" -gt 100 ]; then progress_whole=100; fi

    case "$progress_kind" in
      dlstatus)
        overall_percent=$((download_start + progress_whole * download_span / 100))
        progress_label="$phase_label"
        ;;
      pmstatus)
        overall_percent=$((install_start + progress_whole * install_span / 100))
        progress_label="$phase_label"
        ;;
      pmerror|error)
        overall_percent="$TT_UI_LAST_PERCENT"
        progress_label="Package operation reported an error"
        ;;
      *)
        printf '%s' "$progress_kind" >>"$LOG_FILE"
        if [ -n "$progress_item" ]; then
          printf ':%s' "$progress_item" >>"$LOG_FILE"
        fi
        if [ -n "$progress_percent" ]; then
          printf ':%s' "$progress_percent" >>"$LOG_FILE"
        fi
        if [ -n "$progress_description" ]; then
          printf ':%s' "$progress_description" >>"$LOG_FILE"
        fi
        printf '\n' >>"$LOG_FILE"
        continue
        ;;
    esac

    if [ -z "$progress_description" ]; then
      progress_description="$phase_label"
    fi
    printf 'progress=%s phase=%s package=%s detail=%s\n' \
      "$overall_percent" "$progress_kind" "$progress_item" \
      "$progress_description" >>"$LOG_FILE"
    tt_ui_progress "$overall_percent" "$progress_label" "$display_message"
  done < "$progress_pipe"

  wait "$progress_pid" || true
  if [ -f "$progress_result" ]; then
    progress_exit="$(cat "$progress_result")"
  else
    progress_exit=1
  fi
  rm -f "$progress_pipe" "$progress_result"
  rmdir "$progress_dir" 2>/dev/null || true
  return "$progress_exit"
}

tt_ui_init
trap tt_ui_reset EXIT
tt_ui_progress 2 "Checking Raspberry Pi OS" "Confirming this unit can run TeleTool"

if [ "$(id -u)" -ne 0 ]; then
  tt_ui_failure "Run this installer as root, for example: curl ... | sudo sh"
  exit 1
fi
if [ "$(dpkg --print-architecture)" != "arm64" ]; then
  tt_ui_failure "TeleTool currently supports only 64-bit ARM Raspberry Pi OS."
  exit 1
fi

install -d -m 0755 /var/log
: > "$LOG_FILE"
chmod 0644 "$LOG_FILE"

tt_ui_progress 8 "Preparing the TeleTool package source" "Package output is being recorded in $LOG_FILE"
install -d -m 0755 /usr/share/keyrings
tmp_key="$(mktemp)"
if ! curl -fsSL "$REPOSITORY_URL/teletool-archive-keyring.gpg" -o "$tmp_key" >>"$LOG_FILE" 2>&1; then
  rm -f "$tmp_key"
  tt_ui_failure "Could not download the TeleTool repository signing key." "$LOG_FILE"
  exit 1
fi
install -m 0644 "$tmp_key" "$KEYRING"
rm -f "$tmp_key"

cat > "$SOURCE_FILE" <<EOF
Types: deb
URIs: $REPOSITORY_URL
Suites: stable
Components: main
Architectures: arm64
Signed-By: $KEYRING
EOF

tt_ui_progress 15 "Preparing TeleTool" "Please be patient, TeleTool is preparing the installation..."
if ! run_apt_with_progress 15 5 20 0 "Preparing TeleTool" \
  "Please be patient, TeleTool is preparing the installation..." \
  apt-get -qq -o Dpkg::Use-Pty=0 -o APT::Status-Fd=2 update; then
  tt_ui_failure "Package information could not be refreshed." "$LOG_FILE"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
tt_ui_progress 20 "Installing TeleTool" "Please be patient, TeleTool is installing..."
export TELETOOL_DEFER_COMPLETION=1
if ! run_apt_with_progress 20 30 50 45 "Installing TeleTool" \
  "Please be patient, TeleTool is installing..." \
  apt-get -qq -o Dpkg::Use-Pty=0 -o APT::Status-Fd=2 install -y teletool; then
  printf 'retry=apt-fix-broken after initial package configuration failure\n' >>"$LOG_FILE"
  tt_ui_progress 94 "Finalising TeleTool" "Please be patient, TeleTool is completing the installation..."
  if ! run_apt_with_progress 90 0 90 5 "Finalising TeleTool" \
    "Please be patient, TeleTool is completing the installation..." \
    apt-get -qq -o Dpkg::Use-Pty=0 -o APT::Status-Fd=2 --fix-broken install -y; then
    tt_ui_failure "TeleTool could not be installed." "$LOG_FILE"
    exit 1
  fi
fi
unset TELETOOL_DEFER_COMPLETION

tt_ui_progress 95 "Verifying the installation" "Starting TeleTool and Tvheadend"

ip="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\./ && $i !~ /^127\./) {print $i; exit}}')"
if [ -z "$ip" ]; then
  ip="$(hostname -s 2>/dev/null || printf teletool).local"
fi

teletool_ready=1
tvheadend_ready=1
systemctl is-active --quiet teletool.service 2>/dev/null || teletool_ready=0
systemctl is-active --quiet tvheadend.service 2>/dev/null || tvheadend_ready=0

tt_ui_progress 100 "Installation complete" "TeleTool is ready"
tt_ui_completion_header
tt_ui_web_link "http://$ip:8000/"
if [ "$teletool_ready" -ne 1 ]; then
  tt_ui_status ERROR "TeleTool service did not start; run systemctl status teletool"
fi
if [ "$tvheadend_ready" -ne 1 ]; then
  tt_ui_status WARN "Tvheadend service is not running"
fi

runtime_ready=0
if ldconfig -p 2>/dev/null | grep -q 'libndi\.so\.6' && \
   [ -f /var/lib/teletool/ndi-runtime-verified ]; then
  runtime_ready=1
fi

if [ "$runtime_ready" -ne 1 ]; then
  printf '\n  %sNDI SDK REQUIRED%s\n' "${TT_UI_YELLOW:-}" "${TT_UI_RESET:-}"
  printf '  Open the address above and upload the ARM64 libndi.so.6 file.\n'
  printf '  SDK: https://ndi.video/for-developers/ndi-sdk/\n'
fi
printf '\n'
