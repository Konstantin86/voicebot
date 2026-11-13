#!/usr/bin/env bash
# One-time setup on the Pi. Run from /opt/voicebot:  sudo ./deploy/install.sh
# Safe to run again after code updates.
set -euo pipefail

APP_DIR=/opt/voicebot
ETC_DIR=/etc/voicebot
OWNER=${SUDO_USER:?run with sudo}

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
cd "$APP_DIR"

if ! id voicebot &>/dev/null; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin voicebot
fi
usermod -aG audio voicebot
usermod -aG voicebot "$OWNER"

install -m 0644 deploy/99-voicebot-jabra.rules /etc/udev/rules.d/99-voicebot-jabra.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw --subsystem-match=input

install -d -m 0750 -o voicebot -g voicebot "$ETC_DIR"
if [[ ! -f $ETC_DIR/voicebot.env ]]; then
  install -m 0600 -o voicebot -g voicebot .env.example "$ETC_DIR/voicebot.env"
  echo "Created $ETC_DIR/voicebot.env - fill it in with: sudoedit $ETC_DIR/voicebot.env"
fi
if [[ ! -f $ETC_DIR/context.md ]]; then
  install -m 0640 -o "$OWNER" -g voicebot context.example.md "$ETC_DIR/context.md"
fi

sudo -u "$OWNER" python3 -m venv "$APP_DIR/venv"
sudo -u "$OWNER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$OWNER" "$APP_DIR/venv/bin/pip" install -q -r requirements.txt

install -m 0644 deploy/voicebot.service /etc/systemd/system/voicebot.service
systemctl daemon-reload
systemctl enable voicebot.service
echo "Installed. Start with: sudo systemctl restart voicebot && journalctl -u voicebot -f"
