#!/usr/bin/env bash
# Installs the daily event-calendar refresh (08:02 IST = 02:32 UTC) — same
# pattern as the Dhan token units. Idempotent.
set -euo pipefail

cat > /etc/systemd/system/event-calendar-refresh.service << 'EOF'
[Unit]
Description=Refresh seller event calendar via LLM (librarian only)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/option_trading/ops/gcp/event_calendar_refresh.py
EOF

cat > /etc/systemd/system/event-calendar-refresh.timer << 'EOF'
[Unit]
Description=Daily event-calendar refresh (08:02 IST)
[Timer]
OnCalendar=*-*-* 02:32:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now event-calendar-refresh.timer
systemctl list-timers | grep event-calendar || true
echo "installed."
