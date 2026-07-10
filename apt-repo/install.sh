#!/bin/sh

TT_UI_ENABLED=0
TT_UI_DEFAULT=""
TT_UI_BACKGROUND=""
TT_UI_TEXT=""
TT_UI_RESET=""
TT_UI_BOLD=""
TT_UI_BLUE=""
TT_UI_GREEN=""
TT_UI_YELLOW=""
TT_UI_RED=""
TT_UI_INSTALLER_VERSION="${TELETOOL_INSTALLER_VERSION:-1.0}"
TT_UI_LAST_PERCENT=0

tt_ui_init() {
  case "${TELETOOL_TERMINAL_UI:-auto}" in
    1|true|yes|on) TT_UI_ENABLED=1 ;;
    0|false|no|off) TT_UI_ENABLED=0 ;;
    *)
      if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
        TT_UI_ENABLED=1
      fi
      ;;
  esac

  if [ "$TT_UI_ENABLED" = "1" ] && [ -z "${NO_COLOR:-}" ]; then
    # Palette derived from the TeleTool logo: yellow #F8D818, blue #0888E8,
    # with a deeper blue backdrop to retain strong terminal contrast.
    TT_UI_DEFAULT="$(printf '\033[0m')"
    TT_UI_BACKGROUND="$(printf '\033[48;2;2;40;72m')"
    TT_UI_TEXT="$(printf '\033[38;2;248;216;24m')"
    TT_UI_RESET="${TT_UI_BACKGROUND}${TT_UI_TEXT}"
    TT_UI_BOLD="$(printf '\033[1m')"
    TT_UI_BLUE="$(printf '\033[38;2;8;136;232m')"
    TT_UI_GREEN="$TT_UI_TEXT"
    TT_UI_YELLOW="$TT_UI_TEXT"
    TT_UI_RED="$(printf '\033[38;2;255;104;104m')"
  fi

  if [ "$TT_UI_ENABLED" = "1" ]; then
    printf '%s%s\033[2J\033[H\033[?25l' "$TT_UI_DEFAULT" "$TT_UI_RESET"
  fi
}

tt_ui_clear() {
  if [ "$TT_UI_ENABLED" = "1" ]; then
    printf '%s\033[2J\033[H' "$TT_UI_RESET"
  fi
}

tt_ui_title() {
  if [ "$TT_UI_ENABLED" = "1" ]; then
    printf '\033]0;TeleTool Setup - %s\007' "$1"
  fi
}

tt_ui_reset() {
  if [ "$TT_UI_ENABLED" = "1" ]; then
    printf '%s\033[?25h' "$TT_UI_DEFAULT"
  fi
}

tt_ui_progress_bar() {
  percent="$1"
  width=52
  filled=$((percent * width / 100))
  empty=$((width - filled))
  filled_bar="$(printf '%*s' "$filled" '')"
  empty_bar="$(printf '%*s' "$empty" '')"
  filled_bar="$(printf '%s' "$filled_bar" | tr ' ' '#')"
  empty_bar="$(printf '%s' "$empty_bar" | tr ' ' '-')"
  printf '[%s%s%s%s%s]' "$TT_UI_YELLOW" "$filled_bar" "$TT_UI_BLUE" "$empty_bar" "$TT_UI_RESET"
}

tt_ui_brand() {
  printf '%s%s' "$TT_UI_YELLOW" "$TT_UI_BOLD"
  printf '  TTTTT  EEEEE  L      EEEEE  TTTTT   OOO    OOO   L\n'
  printf '    T    E      L      E        T    O   O  O   O  L\n'
  printf '    T    EEEE   L      EEEE     T    O   O  O   O  L\n'
  printf '    T    E      L      E        T    O   O  O   O  L\n'
  printf '    T    EEEEE  LLLLL  EEEEE    T     OOO    OOO   LLLLL\n'
  printf '%s' "$TT_UI_RESET"
}

tt_ui_progress() {
  percent="$1"
  label="$2"
  detail="${3:-}"
  if [ "$percent" -lt 0 ]; then percent=0; fi
  if [ "$percent" -gt 100 ]; then percent=100; fi
  if [ "$percent" -lt "$TT_UI_LAST_PERCENT" ]; then
    percent="$TT_UI_LAST_PERCENT"
  else
    TT_UI_LAST_PERCENT="$percent"
  fi

  if [ "$TT_UI_ENABLED" != "1" ]; then
    printf '\n==> [%s%%] %s\n' "$percent" "$label"
    if [ -n "$detail" ]; then
      printf '    %s\n' "$detail"
    fi
    return
  fi

  printf '\033[H'
  tt_ui_title "$label"
  tt_ui_brand
  printf '\n  %sRASPBERRY PI SETUP%s  |  Installer v%s\n' "$TT_UI_BOLD" "$TT_UI_RESET" "$TT_UI_INSTALLER_VERSION"
  printf '  ==============================================================================\n'
  printf '\n  %3s%%  ' "$percent"
  tt_ui_progress_bar "$percent"
  printf '\n\n  %s%s%s\n' "$TT_UI_BOLD" "$label" "$TT_UI_RESET"
  if [ -n "$detail" ]; then
    printf '  %s\n' "$detail"
  fi
  printf '\033[J'
}

tt_ui_stage() {
  current="$1"
  total="$2"
  label="$3"
  percent=$((current * 100 / total))
  tt_ui_progress "$percent" "$label" "Stage $current of $total"
}

tt_ui_completion_header() {
  tt_ui_clear
  tt_ui_title "Complete"
  tt_ui_brand
  printf '\n'
  printf '%s%s  TELETOOL READY%s\n' "$TT_UI_GREEN" "$TT_UI_BOLD" "$TT_UI_RESET"
  printf '  Installer v%s\n' "$TT_UI_INSTALLER_VERSION"
  printf '  ==============================================================================\n'
}

tt_ui_web_link() {
  url="$1"
  printf '\n  %s%sOPEN THIS ADDRESS IN A WEB BROWSER%s\n\n' "$TT_UI_YELLOW" "$TT_UI_BOLD" "$TT_UI_RESET"
  printf '  ==============================================================================\n'
  printf '\n      %s%s%s\n\n' "$TT_UI_BOLD" "$url" "$TT_UI_RESET"
  printf '  ==============================================================================\n'
}

tt_ui_failure() {
  message="$1"
  log_file="${2:-}"
  tt_ui_clear
  tt_ui_title "Installation stopped"
  tt_ui_brand
  printf '\n'
  printf '%s%s  TELETOOL INSTALLATION STOPPED%s\n' "$TT_UI_RED" "$TT_UI_BOLD" "$TT_UI_RESET"
  printf '  ==============================================================================\n\n'
  printf '  %s%s%s\n' "$TT_UI_RED" "$message" "$TT_UI_RESET"
  if [ -n "$log_file" ] && [ -f "$log_file" ]; then
    printf '\n  Last installer messages:\n\n'
    tail -n 12 "$log_file" | sed 's/^/    /'
    printf '\n  Full log: %s\n' "$log_file"
  fi
}

tt_ui_status() {
  state="$1"
  message="$2"
  colour="$TT_UI_BLUE"
  case "$state" in
    OK|READY) colour="$TT_UI_GREEN" ;;
    ACTION|WARN) colour="$TT_UI_YELLOW" ;;
    ERROR) colour="$TT_UI_RED" ;;
  esac
  printf "  $colour[%s]$TT_UI_RESET %s\n" "$state" "$message"
}
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
  tt_ui_failure "TeleTool could not be installed." "$LOG_FILE"
  exit 1
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
