#!/usr/bin/env bash
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root on the EC2 instance" >&2; exit 1; }
source /etc/os-release
[[ $ID == amzn && $VERSION_ID == 2023 ]] || { echo "Expected Amazon Linux 2023" >&2; exit 1; }

dnf install -y docker git

# Rotate container logs so they cannot fill the root volume. Docker reads this
# at start-up and applies it to containers created afterwards.
if [[ ! -f /etc/docker/daemon.json ]]; then
  install -d -m 0755 /etc/docker
  printf '%s\n' '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}' \
    > /etc/docker/daemon.json
  # Restarting stops running containers, so only do it when the config is new.
  if systemctl is-active -q docker; then systemctl restart docker; fi
fi
systemctl enable --now docker

plugin_dir=/usr/local/lib/docker/cli-plugins  # searched before the distro's plugins
mkdir -p "$plugin_dir"
download=$(mktemp)
trap 'rm -f "$download"' EXIT

install_plugin() {  # install_plugin <name> <url> <sha256>
  curl -fsSL --retry 3 "$2" -o "$download"
  printf '%s  %s\n' "$3" "$download" | sha256sum -c -
  install -m 0755 "$download" "$plugin_dir/$1"
}

install_plugin docker-compose \
  https://github.com/docker/compose/releases/download/v5.3.1/docker-compose-linux-aarch64 \
  aa611e811d0ea25897839c404bfb5bf93ce706dc51c500a4457890f5d0606a86
# Compose v5 builds through Buildx >= 0.17; the distro package ships 0.12.
install_plugin docker-buildx \
  https://github.com/docker/buildx/releases/download/v0.36.1/buildx-v0.36.1.linux-arm64 \
  5d0cafd9d16afe1a0f0d9529885344ace2cc99efdd531b6c783c5455a6001569
docker compose version
docker buildx version