#!/usr/bin/env bash
# Put the web app's sign-in settings into the EC2 env parameter
# (/rag-systems/prod/langfuse-env, a SecureString), from the Cognito
# resources in infra/terraform:
#   OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_LOGOUT_URL,
#   OIDC_PROVIDER_NAME, SESSION_SECRET (kept if already set, else generated)
#   and ADMIN_EMAILS (the argument).
# Every other key in the parameter is left as it is. Nothing is printed but
# key names. Run from an up-to-date main checkout with Terraform initialised,
# on a trusted administrator machine:
#   AWS_PROFILE=personal scripts/aws/set-auth-secrets.sh you@example.com
set +x
set -euo pipefail

[[ $# -eq 1 && $1 == *@* ]] || { echo "Usage: $0 ADMIN_EMAILS (comma-separated)" >&2; exit 1; }
admin_emails=$1
parameter=/rag-systems/prod/langfuse-env
region=eu-west-2
tf="terraform -chdir=$(dirname "$0")/../../infra/terraform"

work=$(mktemp -d)
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

$tf output -json cognito > "$work/cognito.json"
issuer=$(jq -er .issuer "$work/cognito.json")
client_id=$(jq -er .client_id "$work/cognito.json")
logout_url=$(jq -er .logout_url "$work/cognito.json")
client_secret=$($tf output -raw cognito_client_secret)

aws ssm get-parameter --name "$parameter" --region "$region" --with-decryption --output json \
  | jq -er '.Parameter.Value' > "$work/env"
session_secret=$(grep -E '^SESSION_SECRET=.+' "$work/env" | head -1 | cut -d= -f2- || true)
[[ -n $session_secret ]] || session_secret=$(openssl rand -base64 32 | tr -d '\n')

# Drop the keys being set, then append them.
grep -Ev '^(SESSION_SECRET|OIDC_ISSUER|OIDC_CLIENT_ID|OIDC_CLIENT_SECRET|OIDC_LOGOUT_URL|OIDC_PROVIDER_NAME|ADMIN_EMAILS)=' \
  "$work/env" > "$work/new" || true
{
  printf 'SESSION_SECRET=%s\n' "$session_secret"
  printf 'OIDC_ISSUER=%s\n' "$issuer"
  printf 'OIDC_CLIENT_ID=%s\n' "$client_id"
  printf 'OIDC_CLIENT_SECRET=%s\n' "$client_secret"
  printf 'OIDC_LOGOUT_URL=%s\n' "$logout_url"
  printf 'OIDC_PROVIDER_NAME=%s\n' "Amazon Cognito"
  printf 'ADMIN_EMAILS=%s\n' "$admin_emails"
} >> "$work/new"

size=$(wc -c < "$work/new" | tr -d ' ')
(( size <= 4096 )) || { echo "The parameter would be $size bytes, over the 4 KiB Standard limit" >&2; exit 1; }

aws ssm put-parameter --name "$parameter" --region "$region" --type SecureString --overwrite \
  --value "file://$work/new" --output text > /dev/null
echo "Updated $parameter ($size bytes): SESSION_SECRET OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET OIDC_LOGOUT_URL OIDC_PROVIDER_NAME ADMIN_EMAILS"
