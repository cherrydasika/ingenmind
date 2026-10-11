#!/bin/sh
# Install IngenMind on this machine: everything runs locally in Docker.
#
#   curl -fsSL https://raw.githubusercontent.com/cherrydasika/ingenmind/main/install.sh | sh
#
# Re-running it upgrades in place and keeps your settings and data.
#   sh install.sh --uninstall          stop and remove the containers (data kept)
#   sh install.sh --uninstall --purge  ... and delete the data and ~/.ingenmind
#
# Settings (environment variables, all optional):
#   INGENMIND_VERSION  a release tag such as v0.1.0 (default: the latest release)
#   INGENMIND_DIR      where the compose file and settings go (default: ~/.ingenmind)
#   INGENMIND_PORT     the port on localhost (default: 18000)
#   ANTHROPIC_API_KEY  the model key; asked for when not set
#   TAVILY_API_KEY     web search, needed to set up your own domain; asked for
#                      (Enter skips it) when not set
#   LLM_PROVIDER       anthropic (default, tested), openai (also set OPENAI_API_KEY
#                      and LLM_MODEL) or ollama
#   INGENMIND_DEMO     0 to start without the fictional demo documents
set -eu

REPO=cherrydasika/ingenmind
DIR=${INGENMIND_DIR:-$HOME/.ingenmind}
PORT=${INGENMIND_PORT:-18000}

say() { printf '%s\n' "$*"; }
has_tty() { (exec </dev/tty) 2>/dev/null; }     # a terminal to ask on (not in CI or a pipe without one)
fail() { printf 'x %s\n' "$*" >&2; exit 1; }
# .env is the source of truth: Compose would prefer these from the shell (and
# INGENMIND_VERSION there is the release tag, v0.1.0, not the image tag 0.1.0).
compose() {
  env -u INGENMIND_VERSION -u INGENMIND_IMAGE -u INGENMIND_PORT \
    docker compose --project-directory "$DIR" -f "$DIR/compose.yml" "$@"
}

uninstall() {
  [ -f "$DIR/compose.yml" ] || fail "IngenMind is not installed in $DIR"
  if [ "${1:-}" = "--purge" ]; then
    compose down --volumes
    rm -rf "$DIR"
    say "Removed IngenMind, its data and $DIR."
  else
    compose down
    say "Stopped and removed the containers; the data and $DIR are kept."
  fi
  exit 0
}
[ "${1:-}" = "--uninstall" ] && { shift; uninstall "$@"; }

# --- Docker -------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "Docker is needed: install Docker Desktop, Rancher Desktop or Docker Engine."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running: start it and run this again."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is needed (the 'docker compose' command)."

download() {  # download <url> <file>
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"; else wget -qO "$2" "$1"; fi
}
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# --- The release ----------------------------------------------------------------
VERSION=${INGENMIND_VERSION:-}
if [ -z "$VERSION" ] && [ -z "${INGENMIND_COMPOSE_FILE:-}" ]; then
  VERSION=$(download "https://api.github.com/repos/$REPO/releases/latest" - 2>/dev/null \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$VERSION" ] || fail "Could not find the latest release; set INGENMIND_VERSION (e.g. v0.1.0)."
fi
say "Installing IngenMind ${VERSION:-(local build)} into $DIR"
mkdir -p "$DIR"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if [ -n "${INGENMIND_COMPOSE_FILE:-}" ]; then      # testing: a local compose file and image
  cp "$INGENMIND_COMPOSE_FILE" "$tmp/compose.yml"
else
  base="https://github.com/$REPO/releases/download/$VERSION"
  download "$base/compose.yml" "$tmp/compose.yml" || fail "Could not download $base/compose.yml"
  download "$base/SHA256SUMS" "$tmp/SHA256SUMS" || fail "Could not download $base/SHA256SUMS"
  expected=$(sed -n 's/^\([0-9a-f]*\)  compose.yml$/\1/p' "$tmp/SHA256SUMS")
  [ "$(sha256 "$tmp/compose.yml")" = "$expected" ] || fail "compose.yml does not match its checksum; not installing."
fi
mv "$tmp/compose.yml" "$DIR/compose.yml"

# --- Settings (.env): written once, kept on upgrade -------------------------------
TAG=${INGENMIND_IMAGE_TAG:-${VERSION#v}}
if [ -f "$DIR/.env" ]; then
  say "Keeping your settings in $DIR/.env"
  sed "s/^INGENMIND_VERSION=.*/INGENMIND_VERSION=$TAG/" "$DIR/.env" > "$tmp/env" && cat "$tmp/env" > "$DIR/.env"
else
  PROVIDER=${LLM_PROVIDER:-anthropic}
  KEY=${ANTHROPIC_API_KEY:-}
  if [ "$PROVIDER" = anthropic ] && [ -z "$KEY" ]; then
    has_tty || fail "Set ANTHROPIC_API_KEY to install without a terminal."
    printf 'Anthropic API key (from console.anthropic.com): ' >/dev/tty
    stty -echo </dev/tty 2>/dev/null || true
    read -r KEY </dev/tty
    stty echo </dev/tty 2>/dev/null || true
    printf '\n' >/dev/tty
    [ -n "$KEY" ] || fail "An Anthropic API key is needed (or set LLM_PROVIDER)."
  fi
  TAVILY=${TAVILY_API_KEY:-}
  if [ -z "$TAVILY" ] && has_tty && [ -z "${INGENMIND_NO_PROMPT:-}" ]; then
    say "IngenMind starts with a demo. To set it up for your own domain later (Admin -> Knowledge"
    say "system), it researches the web with Tavily (free tier at tavily.com)."
    printf 'Tavily API key (press Enter to skip; you can add it to .env later): ' >/dev/tty
    stty -echo </dev/tty 2>/dev/null || true
    read -r TAVILY </dev/tty || TAVILY=
    stty echo </dev/tty 2>/dev/null || true
    printf '\n' >/dev/tty
  fi
  secret() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
  umask 077
  {
    say "# IngenMind settings. Re-running install.sh keeps this file."
    say "INGENMIND_VERSION=$TAG"
    [ -n "${INGENMIND_IMAGE:-}" ] && say "INGENMIND_IMAGE=$INGENMIND_IMAGE"
    say "INGENMIND_PORT=$PORT"
    say "# The demo documents; empty this before setting up your own domain."
    if [ "${INGENMIND_DEMO:-1}" = 0 ]; then say "COMPOSE_PROFILES="; else say "COMPOSE_PROFILES=demo"; fi
    say "POSTGRES_PASSWORD=$(secret 16)"
    say "SESSION_SECRET=$(secret 32)"
    say "LLM_PROVIDER=$PROVIDER"
    say "ANTHROPIC_API_KEY=$KEY"
    say "OPENAI_API_KEY=${OPENAI_API_KEY:-}"
    say "LLM_MODEL=${LLM_MODEL:-}"
    say "TAVILY_API_KEY=$TAVILY"
  } > "$DIR/.env"
  umask 022
fi
PORT=$(sed -n 's/^INGENMIND_PORT=//p' "$DIR/.env")

# --- Start ------------------------------------------------------------------------
[ -n "${INGENMIND_COMPOSE_FILE:-}" ] || compose pull --quiet
compose up -d --remove-orphans
say "Waiting for IngenMind to answer (the first start downloads a 130 MB embedding model)..."
i=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/config" 2>/dev/null)" != 000 ]; do
  i=$((i + 1)); [ $i -lt 100 ] || fail "IngenMind did not answer within 5 minutes: docker compose -f $DIR/compose.yml logs webapp"
  sleep 3
done

say ""
say "IngenMind is running: http://localhost:$PORT"
say "  Sign in with your name and email (the first person becomes the admin), then ask"
say "  the demo a question, e.g. \"Is there step-free access at Kestrel Bay station?\""
say "  To use your own domain instead: Admin -> Knowledge system -> Set up my own domain."
say ""
say "  Stop:     docker compose -f $DIR/compose.yml stop"
say "  Start:    docker compose -f $DIR/compose.yml start"
say "  Upgrade:  run the install command again"
say "  Remove:   curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | sh -s -- --uninstall [--purge]"
