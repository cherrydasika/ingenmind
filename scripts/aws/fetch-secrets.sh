#!/usr/bin/env bash
set +x
set -euo pipefail

[[ $(id -u) -eq 0 && $# -eq 1 && -f $1/docker-compose.langfuse.yml ]] || {
  echo "Run as root with a verified release directory" >&2
  exit 1
}

release_dir=$(realpath "$1")
umask 077
temp=$(mktemp "$release_dir/.env.XXXXXXXX")
trap 'rm -f "$temp"' EXIT

aws ssm get-parameter \
  --name /rag-systems/prod/langfuse-env \
  --region eu-west-2 \
  --with-decryption --output json | jq -er '.Parameter.Value' > "$temp"

for name in POSTGRES_PASSWORD DATABASE_URL SALT ENCRYPTION_KEY CLICKHOUSE_PASSWORD REDIS_AUTH NEXTAUTH_SECRET MINIO_ROOT_PASSWORD LANGFUSE_INIT_PROJECT_PUBLIC_KEY LANGFUSE_INIT_PROJECT_SECRET_KEY LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD \
    SESSION_SECRET OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET OIDC_LOGOUT_URL ADMIN_EMAILS; do
  if ! grep -Eq "^${name}=.+" "$temp"; then
    printf 'Missing required secret: %s\n' "$name" >&2
    exit 1
  fi
done

install -m 0600 "$temp" "$release_dir/.env"
echo "Installed private Compose environment in $release_dir"