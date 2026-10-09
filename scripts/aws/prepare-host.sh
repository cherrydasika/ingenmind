#!/usr/bin/env bash
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root on the EC2 instance" >&2; exit 1; }
volume_id=${1:?Pass the Terraform data_volume_id (vol-...)}
[[ $volume_id =~ ^vol-[0-9a-f]+$ ]] || { echo "Invalid EBS volume ID" >&2; exit 1; }
command -v docker >/dev/null || { echo "Install Docker first" >&2; exit 1; }
docker compose version >/dev/null || { echo "Install the Docker Compose plugin first" >&2; exit 1; }

serial=${volume_id//-/}
device=$(lsblk -dnro NAME,SERIAL,TYPE | awk -v serial="$serial" '$2 == serial && $3 == "disk" {print "/dev/" $1}')
[[ -n $device && $device != *$'\n'* ]] || { echo "Expected exactly one NVMe disk with serial $serial" >&2; exit 1; }
[[ $(lsblk -nr -o NAME "$device" | wc -l) -eq 1 ]] || { echo "Disk has partitions; inspect it manually" >&2; exit 1; }

mount_dir=/srv/rag-systems
if mountpoint -q "$mount_dir"; then
  [[ $(findmnt -no SOURCE "$mount_dir") == "$device" ]] || {
    echo "$mount_dir is mounted from another device; refusing to proceed" >&2; exit 1;
  }
else
  filesystem=$(blkid -s TYPE -o value "$device" || true)
  if [[ -z $filesystem ]]; then
    mkfs.ext4 "$device"
    filesystem=ext4
  fi
  [[ $filesystem == ext4 || $filesystem == xfs ]] || { echo "Unsupported filesystem: $filesystem" >&2; exit 1; }
  filesystem_uuid=$(blkid -s UUID -o value "$device")
  mkdir -p "$mount_dir"
  if ! grep -q "^UUID=$filesystem_uuid " /etc/fstab; then
    printf 'UUID=%s %s %s defaults,nofail,x-systemd.device-timeout=30s 0 2\n' \
      "$filesystem_uuid" "$mount_dir" "$filesystem" >> /etc/fstab
  fi
  mount "$mount_dir"
fi
mountpoint -q "$mount_dir" || { echo "EBS is not mounted; refusing to create data directories" >&2; exit 1; }

install -d -m 0700 -o 999 -g 999 "$mount_dir/pgvector" "$mount_dir/langfuse/postgres" "$mount_dir/langfuse/redis"
install -d -m 0750 -o 101 -g 101 "$mount_dir/langfuse/clickhouse" "$mount_dir/langfuse/clickhouse-logs"
install -d -m 0750 -o 65532 -g 65532 "$mount_dir/langfuse/minio"
echo "Prepared $mount_dir on $volume_id; Docker data directories are on EBS"