#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "[ERROR] .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [[ -f ".env.local" ]]; then
  # shellcheck disable=SC1091
  source ".env.local"
fi

export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:1087}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-5318777224}"

for arg in "$@"; do
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    exec .venv/bin/python astock_alert.py "$@"
  fi
done

if [[ -z "${TELEGRAM_BOT_TOKEN}" ]]; then
  echo "[ERROR] TELEGRAM_BOT_TOKEN is empty. Set it in .env.local or export it before running."
  exit 1
fi

exec .venv/bin/python astock_alert.py "$@"
