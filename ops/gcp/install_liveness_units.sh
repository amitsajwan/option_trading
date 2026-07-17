#!/usr/bin/env bash
# Installs the strategy-liveness probe as a systemd timer (every 5 min).
# Run on the VM: sudo bash ops/gcp/install_liveness_units.sh
set -euo pipefail
REPO=/opt/option_trading

cat > /etc/systemd/system/strategy-liveness.service <<EOF
[Unit]
Description=Strategy consumer liveness probe (market hours)
[Service]
Type=oneshot
ExecStart=/usr/bin/bash ${REPO}/ops/gcp/check_strategy_liveness.sh
EOF

cat > /etc/systemd/system/strategy-liveness.timer <<EOF
[Unit]
Description=Run strategy liveness probe every 5 min
[Timer]
OnCalendar=*:0/5
Persistent=false
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now strategy-liveness.timer
systemctl list-timers strategy-liveness.timer --no-pager | head -3
echo "installed. Test with: sudo bash ${REPO}/ops/gcp/check_strategy_liveness.sh --force"
