#!/usr/bin/env bash
# deploy/setup-systemd.sh — Install and enable systemd services.
# Run as root on Linux.
#
# Usage:
#   sudo bash deploy/setup-systemd.sh /opt/hermes-agent

set -euo pipefail

HERMES_DIR="${1:-/opt/hermes-agent}"
HERMES_HOME="/var/lib/hermes"
HERMES_USER="hermes"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR"

# ── Create user ─────────────────────────────────────────────────────────────
if ! id "$HERMES_USER" &>/dev/null; then
    useradd --system --home-dir "$HERMES_HOME" --shell /usr/sbin/nologin "$HERMES_USER"
    echo "Created user: $HERMES_USER"
fi

# ── Create directories ──────────────────────────────────────────────────────
mkdir -p "$HERMES_HOME"/{logs,pids,tasks}
chown -R "$HERMES_USER:$HERMES_USER" "$HERMES_HOME"
chown -R "$HERMES_USER:$HERMES_USER" "$HERMES_DIR"

# ── Install service files ───────────────────────────────────────────────────
cp "$DEPLOY_DIR/hermes-coordinator.service" /etc/systemd/system/
cp "$DEPLOY_DIR/hermes-agent@.service" /etc/systemd/system/

# ── Update WorkingDirectory in service files ────────────────────────────────
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$HERMES_DIR|" \
    /etc/systemd/system/hermes-coordinator.service
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$HERMES_DIR|" \
    /etc/systemd/system/hermes-agent@.service

# ── Enable and start ───────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable hermes-coordinator
systemctl start hermes-coordinator

echo "Waiting for coordinator..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:9100/health &>/dev/null; then
        echo "Coordinator healthy."
        break
    fi
    echo -n "."
    sleep 1
done

# Enable all 4 agent types
for agent in design dev validate deploy; do
    systemctl enable "hermes-agent@${agent}"
    systemctl start "hermes-agent@${agent}"
    echo "Started agent: $agent"
done

echo ""
echo "=== Status ==="
systemctl status hermes-coordinator --no-pager
for agent in design dev validate deploy; do
    systemctl status "hermes-agent@${agent}" --no-pager
done

echo ""
echo "Done. Manage with:"
echo "  systemctl [start|stop|restart|status] hermes-coordinator"
echo "  systemctl [start|stop|restart|status] hermes-agent@design"
echo "  journalctl -u hermes-coordinator -f"
