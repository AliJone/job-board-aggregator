#!/usr/bin/env bash
# Sets up the daily run on an Ubuntu server: venv, requirements, .env, and the
# systemd timer (18:00 Asia/Karachi). Safe to rerun after a git pull.
# Run it as the normal user; it calls sudo itself for the systemd files.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as the normal user, not root; it calls sudo where needed." >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$REPO"

# pandas 3 in requirements.txt needs Python 3.11 or newer
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ is needed. On Ubuntu 22.04:" >&2
  echo "  sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.12 python3.12-venv" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || { echo "venv failed; install it with: sudo apt install -y ${PY}-venv" >&2; exit 1; }
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Fill in SMTP_* and SUMMARY_TO before the first run."
fi
chmod 600 .env
mkdir -p scripts/output/runs scripts/output/logs

for unit in jba-daily.service jba-daily.timer; do
  sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$RUN_USER|g" "deploy/$unit" |
    sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now jba-daily.timer

systemd-analyze calendar "*-*-* 18:00:00 Asia/Karachi" || true
systemctl list-timers jba-daily.timer --no-pager
echo
echo "Next: fill in .env, then run  .venv/bin/python scripts/daily_run.py --test-email"
