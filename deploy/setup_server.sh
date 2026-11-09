#!/usr/bin/env bash
# Installs (or updates) AI Desk Bot on an Ubuntu 24.04 server. Paper trading only.
#   Run as root on the server:  bash setup_server.sh <hostname>
#   <hostname> is what the phone opens, e.g. 203-0-113-9.sslip.io (a free name that points to the IP)
# Expects the app bundle at /tmp/deskbot.tgz (deploy.sh uploads it).
set -euo pipefail
HOST="${1:?usage: setup_server.sh <hostname>}"
BASE=/opt/deskbot
APP=$BASE/app
export DEBIAN_FRONTEND=noninteractive

echo "== packages (Ubuntu's own repositories only)"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip caddy unattended-upgrades
command -v netfilter-persistent >/dev/null 2>&1 || apt-get install -y ufw
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== user and files"
id deskbot >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin deskbot
mkdir -p "$APP"
tar xzf /tmp/deskbot.tgz -C "$APP"
rm -f /tmp/deskbot.tgz

echo "== python environment"
if [ ! -x "$APP/.venv/bin/python" ]; then python3 -m venv "$APP/.venv"; fi
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "== server settings: paper ledger instead of the PC's AI-Trader, AI off until a key is added, dashboard behind HTTPS"
cd "$APP"
"$APP/.venv/bin/python" - <<'PY'
from deskbot import settings_edit
settings_edit.write({("broker", "kind"): "paper", ("ai", "backend"): "none", ("dashboard", "lan"): False})
PY
mkdir -p "$APP/logs"
chown -R deskbot:deskbot "$BASE"
chmod 600 "$APP/.env" "$APP/phone_token.txt" "$APP"/phone_password.json 2>/dev/null || true

echo "== services"
cat >/etc/systemd/system/deskbot-dashboard.service <<EOF
[Unit]
Description=AI Desk Bot dashboard (127.0.0.1:8899, behind Caddy)
After=network-online.target
Wants=network-online.target

[Service]
User=deskbot
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m deskbot dashboard --local
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/deskbot-cycle.service <<EOF
[Unit]
Description=AI Desk Bot cycle: stops, daily desk, futures and stocks desks
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=deskbot
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m deskbot run
TimeoutStartSec=900
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
EOF

cat >/etc/systemd/system/deskbot-cycle.timer <<EOF
[Unit]
Description=Run the AI Desk Bot cycle every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=20

[Install]
WantedBy=timers.target
EOF

echo "== HTTPS (Caddy gets the certificate by itself)"
cat >/etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8899
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        X-Frame-Options "DENY"
    }
}
EOF

echo "== firewall: only SSH and HTTPS"
if [ -f /etc/iptables/rules.v4 ] && command -v netfilter-persistent >/dev/null 2>&1; then
  # Oracle Cloud images ship their own iptables rules (everything but SSH rejected); don't mix in ufw
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null       || iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  done
  netfilter-persistent save
else
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo "== swap on small machines (1 GB free VMs)"
if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 2000000 ] && [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

systemctl daemon-reload
systemctl enable --now deskbot-dashboard.service deskbot-cycle.timer
systemctl restart deskbot-dashboard.service
systemctl reload caddy || systemctl restart caddy
echo "== done: https://$HOST/app"
