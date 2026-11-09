#!/usr/bin/env bash
# From this PC (Git Bash): packs the bot with its paper state and installs it on the server.
#   bash deploy/deploy.sh <server IP> [ssh user]
#   ssh user: root (Hetzner and most providers) or ubuntu (Oracle Cloud); it needs sudo
# Uses the SSH key ~/.ssh/deskbot_server (created once with ssh-keygen; only its .pub goes to the provider).
set -euo pipefail
IP="${1:?usage: deploy.sh <server IP> [ssh user]}"
USER_AT="${2:-root}@$IP"
KEY="$HOME/.ssh/deskbot_server"
HOST="${IP//./-}.sslip.io"          # free hostname that points to the IP, so HTTPS works without buying a domain
cd "$(dirname "$0")/.."
BUNDLE="$(mktemp -d)/deskbot.tgz"
tar czf "$BUNDLE" \
  --exclude=.venv --exclude='__pycache__' --exclude=.pytest_cache --exclude=logs --exclude=reports \
  --exclude='*.tmp' --exclude='memetest/pid' --exclude='memetest/stop' \
  deskbot tests deploy requirements.txt config.toml .env README.md PESQUISA.md \
  $(ls -d data journal.db bot_state.json paper_account.json controls.json desks memetest \
        phone_token.txt phone_password.json 2>/dev/null)
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
scp "${SSH_OPTS[@]}" "$BUNDLE" "$USER_AT:/tmp/deskbot.tgz"
ssh "${SSH_OPTS[@]}" "$USER_AT" "sudo bash -s -- $HOST" < deploy/setup_server.sh
rm -f "$BUNDLE"
echo "App: https://$HOST/app"
