#!/usr/bin/env bash
# Scale Hub installer for Raspberry Pi OS (Bookworm or newer, 64-bit recommended).
# Run from the unpacked scale-hub folder:  ./install.sh
# Safe to run again: it keeps an existing config.yaml, .env and database.
set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
BSS_DIR="$RUN_HOME/ble-scale-sync"
PORT="${SCALE_HUB_PORT:-8080}"

if [[ "$RUN_USER" == "root" ]]; then
  echo "Run this as your normal user (it uses sudo where needed), not as root." >&2
  exit 1
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Installing system packages (Bluetooth, Python, curl)"
sudo apt-get update -qq
sudo apt-get install -y -qq bluetooth bluez python3 python3-venv curl ca-certificates >/dev/null
sudo systemctl enable --now bluetooth
sudo usermod -aG bluetooth "$RUN_USER" || true
# Lets ble-scale-sync power-cycle the adapter after each session, which keeps
# BlueZ from getting stuck after days of continuous scanning.
sudo apt-get install -y -qq libcap2-bin >/dev/null
sudo setcap cap_net_admin+ep "$(command -v btmgmt)" || echo "warning: could not setcap btmgmt (non-fatal)"
# Stop bluetoothd's GAP plugin from loading the scale's "preferred connection
# parameters" (10-12.5 ms) into the kernel. With them loaded, Linux refuses the
# R-MSC04's own request for 30-45 ms, and the scale hangs up 5.5 s into every
# weigh-in, before it sends body composition. Takes full effect after a reboot.
BTD=""
for p in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd; do
  [[ -x "$p" ]] && BTD="$p" && break
done
if [[ -n "$BTD" ]]; then
  sudo mkdir -p /etc/systemd/system/bluetooth.service.d
  printf '[Service]\nExecStart=\nExecStart=%s -P gap\n' "$BTD" |
    sudo tee /etc/systemd/system/bluetooth.service.d/scale-hub.conf >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
else
  echo "warning: bluetoothd not found; skipping the -P gap override"
fi

say "Checking Node.js (ble-scale-sync needs 22.x or newer)"
node_major() { node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0; }
if [[ "$(node_major)" -lt 22 ]]; then
  echo "Installing Node.js 22 from NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs >/dev/null
fi
echo "Node $(node -v)"

say "Installing ble-scale-sync from GitHub (Bluetooth reader + Garmin uploader)"
# ble-scale-sync is not on the npm registry (npm install returns 404), so build it from source.
sudo apt-get install -y -qq git build-essential >/dev/null
mkdir -p "$BSS_DIR"
SRC_DIR="$BSS_DIR/src"
if [[ -d "$SRC_DIR/.git" ]]; then
  git -C "$SRC_DIR" checkout -q -- .   # drop last install's patches before updating
  git -C "$SRC_DIR" pull --ff-only -q
else
  git clone -q --depth 1 https://github.com/KristianP26/ble-scale-sync.git "$SRC_DIR"
fi
# Local patches (ble-scale-sync/patches/): the household patch lets the scale compute for
# whoever stepped on. A patch that no longer applies to a newer ble-scale-sync is skipped.
for patch in "$HUB_DIR"/ble-scale-sync/patches/*.patch; do
  [[ -e "$patch" ]] || continue
  if git -C "$SRC_DIR" apply --check "$patch" 2>/dev/null; then
    git -C "$SRC_DIR" apply "$patch" && echo "Applied $(basename "$patch")"
  else
    echo "warning: $(basename "$patch") does not apply to this ble-scale-sync version; skipped"
  fi
done
(cd "$SRC_DIR" && npm ci --no-fund --no-audit)
if ! (cd "$SRC_DIR" && npm run build); then
  # A local patch must never cost the working listener: rebuild without the patches.
  echo "warning: ble-scale-sync failed to build with the local patches; building it without them"
  git -C "$SRC_DIR" checkout -q -- .
  (cd "$SRC_DIR" && npm run build)
fi
sudo npm install -g "$SRC_DIR" --no-fund --no-audit
BSS_BIN="$(command -v ble-scale-sync)"
if [[ ! -d "$BSS_DIR/venv" ]]; then
  say "Creating Python venv for the Garmin uploader"
  python3 -m venv "$BSS_DIR/venv"
fi
"$BSS_DIR/venv/bin/pip" install -q --upgrade pip
"$BSS_DIR/venv/bin/pip" install -q -r "$SRC_DIR/requirements.txt"

say "Setting up the shared ingest key"
ENV_FILE="$BSS_DIR/.env"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -q '^SCALE_HUB_TOKEN=' "$ENV_FILE"; then
  echo "SCALE_HUB_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$ENV_FILE"
fi
grep -q '^# GARMIN_EMAIL' "$ENV_FILE" || grep -q '^GARMIN_EMAIL' "$ENV_FILE" || cat >> "$ENV_FILE" <<'EOF'
# Garmin Connect (optional): uncomment, fill in, and enable the garmin exporter in config.yaml
# GARMIN_EMAIL=you@example.com
# GARMIN_PASSWORD=your-password
EOF

if [[ ! -f "$BSS_DIR/config.yaml" ]]; then
  cp "$HUB_DIR/ble-scale-sync/config.yaml.template" "$BSS_DIR/config.yaml"
  CONFIG_IS_NEW=1
else
  CONFIG_IS_NEW=0
fi

say "Installing systemd services"
mkdir -p "$HUB_DIR/data"
render() {
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@HUB_DIR@|$HUB_DIR|g" -e "s|@BSS_DIR@|$BSS_DIR|g" \
      -e "s|@BSS_BIN@|$BSS_BIN|g" -e "s|@PORT@|$PORT|g" "$1" | sudo tee "/etc/systemd/system/$(basename "$1")" >/dev/null
}
render "$HUB_DIR/systemd/scale-hub.service"
render "$HUB_DIR/systemd/ble-scale.service"
sudo systemctl daemon-reload
sudo systemctl enable --now scale-hub.service
sudo systemctl enable ble-scale.service

IP="$(hostname -I | awk '{print $1}')"
say "Done"
cat <<EOF

Dashboard:  http://$IP:$PORT   (or http://$(hostname).local:$PORT)

Next steps:
  1. Edit your profile (name, height, birth date, gender, weight range):
       nano $BSS_DIR/config.yaml
  2. Check it:
       cd $BSS_DIR && ble-scale-sync validate
  3. Start the Bluetooth listener:
       sudo systemctl restart ble-scale
  4. Step on the scale and stay on until it shows the results. Watch the log with:
       journalctl -u ble-scale -f

  Garmin Connect (optional): put GARMIN_EMAIL/GARMIN_PASSWORD in $ENV_FILE,
  uncomment the garmin block in config.yaml, then run:
       cd $BSS_DIR && ble-scale-sync setup-garmin --all-users
       sudo systemctl restart ble-scale

  Reboot once after the first install (sudo reboot) so the Bluetooth fix takes effect.

  Close the Renpho app on your phone (or turn off phone Bluetooth) while testing:
  the scale talks to only one device at a time.
EOF
if [[ "$CONFIG_IS_NEW" == 1 ]]; then
  echo "  (A fresh config.yaml was created from the template. Edit it before step 3.)"
fi
