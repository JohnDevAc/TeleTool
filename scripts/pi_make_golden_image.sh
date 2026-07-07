#!/usr/bin/env bash
set -Eeuo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [ "${EUID}" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

OUT_DIR="${1:-/home/admin/golden-master}"
NAME="${2:-teletool-golden-$(date +%Y%m%d-%H%M%S)}"
COMPRESS="${COMPRESS:-xz}"
ROOT_MIN_MIB="${ROOT_MIN_MIB:-12288}"
ROOT_MARGIN_MIB="${ROOT_MARGIN_MIB:-4096}"
BOOT_SRC="${BOOT_SRC:-/boot/firmware}"
SERVICES_TO_QUIESCE="${SERVICES_TO_QUIESCE:-tvh_ndi_bridge tvheadend}"

work_dir=""
mount_dir=""
loop_dev=""
stopped_services=()

log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

cleanup() {
  set +e
  sync
  if [ -n "${mount_dir}" ]; then
    mountpoint -q "${mount_dir}/boot/firmware" && umount "${mount_dir}/boot/firmware"
    mountpoint -q "${mount_dir}" && umount "${mount_dir}"
  fi
  if [ -n "${loop_dev}" ]; then
    losetup -d "${loop_dev}" >/dev/null 2>&1
  fi
  if [ -n "${work_dir}" ] && [ -d "${work_dir}" ]; then
    rm -rf "${work_dir}"
  fi
  for ((idx=${#stopped_services[@]}-1; idx>=0; idx--)); do
    systemctl start "${stopped_services[$idx]}" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

need awk
need blkid
need blockdev
need df
need e2fsck
need gzip
need losetup
need mkfs.ext4
need mount
need parted
need partprobe
need rsync
need sed
need sha256sum
need sfdisk
need sync
need truncate
need xz

mkfs_vfat="$(command -v mkfs.vfat || command -v mkfs.fat || true)"
if [ -z "${mkfs_vfat}" ]; then
  echo "Missing required command: mkfs.vfat or mkfs.fat" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd "${OUT_DIR}" && pwd)"
img="${OUT_DIR}/${NAME}.img"

if [ -e "${img}" ] || [ -e "${img}.xz" ] || [ -e "${img}.gz" ]; then
  echo "Refusing to overwrite existing image artifact for ${NAME}" >&2
  exit 1
fi

root_used_kib="$(df -Pk / | awk 'NR == 2 {print $3}')"
root_used_mib="$(( (root_used_kib + 1023) / 1024 ))"
root_target_mib="$(( root_used_mib + ROOT_MARGIN_MIB ))"
if [ "${root_target_mib}" -lt "${ROOT_MIN_MIB}" ]; then
  root_target_mib="${ROOT_MIN_MIB}"
fi

boot_dev="$(findmnt -n -o SOURCE "${BOOT_SRC}")"
boot_bytes="$(blockdev --getsize64 "${boot_dev}")"
boot_mib="$(( (boot_bytes + 1048575) / 1048576 ))"
if [ "${boot_mib}" -lt 512 ]; then
  boot_mib=512
fi

start_mib=4
boot_end_mib="$(( start_mib + boot_mib ))"
root_start_mib="${boot_end_mib}"
total_mib="$(( root_start_mib + root_target_mib + 64 ))"

log "Creating sparse image: ${img}"
log "Boot partition: ${boot_mib} MiB; root partition: ${root_target_mib} MiB; image: ${total_mib} MiB"
truncate -s "${total_mib}M" "${img}"

parted -s "${img}" mklabel msdos
parted -s "${img}" unit MiB mkpart primary fat32 "${start_mib}" "${boot_end_mib}"
parted -s "${img}" unit MiB mkpart primary ext4 "${root_start_mib}" 100%
parted -s "${img}" set 1 boot on
parted -s "${img}" set 1 lba on

loop_dev="$(losetup --find --partscan --show "${img}")"
partprobe "${loop_dev}" || true
sleep 1

boot_part="${loop_dev}p1"
root_part="${loop_dev}p2"
for _ in $(seq 1 20); do
  [ -b "${boot_part}" ] && [ -b "${root_part}" ] && break
  sleep 0.25
done
[ -b "${boot_part}" ] || { echo "Loop boot partition did not appear: ${boot_part}" >&2; exit 1; }
[ -b "${root_part}" ] || { echo "Loop root partition did not appear: ${root_part}" >&2; exit 1; }

log "Formatting image partitions"
"${mkfs_vfat}" -F 32 -n BOOTFS "${boot_part}" >/dev/null
mkfs.ext4 -F -L rootfs "${root_part}" >/dev/null

work_dir="$(mktemp -d /tmp/teletool-golden.XXXXXX)"
mount_dir="${work_dir}/root"
mkdir -p "${mount_dir}"
mount "${root_part}" "${mount_dir}"

log "Stopping services for a consistent snapshot"
for svc in ${SERVICES_TO_QUIESCE}; do
  if systemctl list-unit-files "${svc}.service" >/dev/null 2>&1 && systemctl is-active --quiet "${svc}"; then
    systemctl stop "${svc}" || true
    stopped_services+=("${svc}")
    log "Stopped ${svc}"
  fi
done

log "Copying root filesystem"
rsync -aHAXx --numeric-ids --delete \
  --exclude=/dev/*** \
  --exclude=/proc/*** \
  --exclude=/sys/*** \
  --exclude=/tmp/*** \
  --exclude=/run/*** \
  --exclude=/mnt/*** \
  --exclude=/media/*** \
  --exclude=/lost+found \
  --exclude=/swapfile \
  --exclude=/var/swap \
  --exclude=/var/tmp/*** \
  --exclude="${OUT_DIR}/***" \
  / "${mount_dir}/"

mkdir -p "${mount_dir}/boot/firmware"
mount "${boot_part}" "${mount_dir}/boot/firmware"

log "Copying boot filesystem"
rsync -aHAX --numeric-ids --delete "${BOOT_SRC}/" "${mount_dir}/boot/firmware/"

boot_partuuid="$(blkid -s PARTUUID -o value "${boot_part}")"
root_partuuid="$(blkid -s PARTUUID -o value "${root_part}")"

log "Updating image PARTUUID references"
awk -v boot="${boot_partuuid}" -v root="${root_partuuid}" '
  $2 == "/boot/firmware" { $1 = "PARTUUID=" boot }
  $2 == "/" { $1 = "PARTUUID=" root }
  { print }
' "${mount_dir}/etc/fstab" > "${mount_dir}/etc/fstab.new"
mv "${mount_dir}/etc/fstab.new" "${mount_dir}/etc/fstab"

if [ -f "${mount_dir}/boot/firmware/cmdline.txt" ]; then
  sed -i -E "s#root=[^ ]+#root=PARTUUID=${root_partuuid}#" "${mount_dir}/boot/firmware/cmdline.txt"
fi

log "Adding first-boot root expansion service"
mkdir -p "${mount_dir}/usr/local/sbin" "${mount_dir}/etc/systemd/system/multi-user.target.wants"
cat > "${mount_dir}/usr/local/sbin/teletool-firstboot-expand.sh" <<'EXPAND'
#!/bin/sh
set -eu
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

root_dev="$(findmnt -n -o SOURCE /)"
root_base="$(basename "${root_dev}")"
disk_name="$(lsblk -no PKNAME "${root_dev}" | head -n 1)"
part_num="$(cat "/sys/class/block/${root_base}/partition" 2>/dev/null || true)"

if [ -n "${disk_name}" ] && [ -n "${part_num}" ]; then
  disk="/dev/${disk_name}"
  parted -s "${disk}" resizepart "${part_num}" 100% || true
  partprobe "${disk}" || true
fi

resize2fs "${root_dev}" || true
systemctl disable teletool-firstboot-expand.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/multi-user.target.wants/teletool-firstboot-expand.service
rm -f /etc/systemd/system/teletool-firstboot-expand.service
rm -f /usr/local/sbin/teletool-firstboot-expand.sh
EXPAND
chmod 0755 "${mount_dir}/usr/local/sbin/teletool-firstboot-expand.sh"

cat > "${mount_dir}/etc/systemd/system/teletool-firstboot-expand.service" <<'UNIT'
[Unit]
Description=Expand TeleTool root filesystem on first boot
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/teletool-firstboot-expand.sh

[Install]
WantedBy=multi-user.target
UNIT
ln -sf ../teletool-firstboot-expand.service \
  "${mount_dir}/etc/systemd/system/multi-user.target.wants/teletool-firstboot-expand.service"

log "Preparing image as a cloneable master"
: > "${mount_dir}/etc/machine-id"
rm -f "${mount_dir}/var/lib/dbus/machine-id"
ln -sf /etc/machine-id "${mount_dir}/var/lib/dbus/machine-id" 2>/dev/null || true
rm -f "${mount_dir}/var/lib/systemd/random-seed" 2>/dev/null || true
find "${mount_dir}/var/log" -type f -exec truncate -s 0 {} + 2>/dev/null || true
rm -rf "${mount_dir}/tmp/"* "${mount_dir}/var/tmp/"* 2>/dev/null || true

sync
umount "${mount_dir}/boot/firmware"
umount "${mount_dir}"

log "Checking root filesystem"
e2fsck -fy "${root_part}" >/dev/null

losetup -d "${loop_dev}"
loop_dev=""
sync

case "${COMPRESS}" in
  xz)
    artifact="${img}.xz"
    log "Compressing image with xz"
    xz -T0 -1 --keep --force "${img}"
    ;;
  gzip|gz)
    artifact="${img}.gz"
    log "Compressing image with gzip"
    gzip -1 --keep --force "${img}"
    ;;
  none)
    artifact="${img}"
    ;;
  *)
    echo "Unsupported COMPRESS=${COMPRESS}; use xz, gzip, or none" >&2
    exit 1
    ;;
esac

log "Writing checksums and metadata"
sha256sum "${artifact}" > "${artifact}.sha256"
{
  echo "name=${NAME}"
  echo "created_at=$(date -Is)"
  echo "source_host=$(hostname)"
  echo "source_root=$(findmnt -n -o SOURCE /)"
  echo "source_boot=${boot_dev}"
  echo "image_mib=${total_mib}"
  echo "boot_mib=${boot_mib}"
  echo "root_mib=${root_target_mib}"
  echo "boot_partuuid=${boot_partuuid}"
  echo "root_partuuid=${root_partuuid}"
  echo "artifact=${artifact}"
  echo "sha256=$(cut -d ' ' -f 1 "${artifact}.sha256")"
} > "${artifact}.manifest"

du -h "${img}" "${artifact}" "${artifact}.sha256" "${artifact}.manifest" 2>/dev/null || true
log "Golden image complete: ${artifact}"
