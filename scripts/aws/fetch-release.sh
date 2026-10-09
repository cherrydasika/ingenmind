#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 || $# -ne 2 || ! $1 =~ ^[a-z0-9.-]+$ || ! $2 =~ ^[a-f0-9]{40}$ ]]; then
  echo "Usage (as root): fetch-release.sh ARTIFACT_BUCKET COMMIT_SHA" >&2
  exit 1
fi

bucket=$1
revision=$2
release_dir="/opt/rag-systems/releases/$revision"
[[ ! -e $release_dir ]] || { echo "Release already exists: $revision" >&2; exit 1; }

umask 077
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
aws s3 cp "s3://$bucket/releases/$revision/source.tar.gz" "$temp/source.tar.gz" --region eu-west-2 --only-show-errors
aws s3 cp "s3://$bucket/releases/$revision/source.tar.gz.sha256" "$temp/source.tar.gz.sha256" --region eu-west-2 --only-show-errors
(cd "$temp" && sha256sum -c source.tar.gz.sha256)

mkdir -p "$release_dir"
tar --no-same-owner -xzf "$temp/source.tar.gz" -C "$release_dir"
echo "Verified source release: $release_dir"