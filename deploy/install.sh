#!/usr/bin/env bash
# Install the collector as a systemd service on a Debian/Ubuntu VPS. Run as root from a clone of this repo:
#   sudo bash deploy/install.sh
set -euo pipefail

APP=/opt/beltounsi
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync

id beltounsi >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin beltounsi
mkdir -p "$APP"
rsync -a --delete --exclude .git --exclude data --exclude .venv --exclude config.toml --exclude .env "$SRC/" "$APP/"

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "$APP[hf]"

[ -f "$APP/config.toml" ] || cp "$APP/config.example.toml" "$APP/config.toml"
if [ ! -f "$APP/.env" ]; then
  echo "HF_TOKEN=" > "$APP/.env"
  chmod 600 "$APP/.env"
fi
chown -R beltounsi:beltounsi "$APP"

cp "$APP/deploy/beltounsi-collect.service" /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Installed in $APP. Before starting:
  1. edit $APP/config.toml   (set hf_repo_id, e.g. "you/BelTounsi")
  2. put your HF write token in $APP/.env   (HF_TOKEN=hf_...)
  3. optional, avoids re-collecting posts already on HF:
       sudo -u beltounsi bash -c 'set -a; . $APP/.env; $APP/.venv/bin/beltounsi-collect --config $APP/config.toml import-seen --hf'
  4. quick check: sudo -u beltounsi $APP/.venv/bin/beltounsi-collect --config $APP/config.toml crawl -n 3
  5. start:  systemctl enable --now beltounsi-collect
     logs:   journalctl -u beltounsi-collect -f
EOF
