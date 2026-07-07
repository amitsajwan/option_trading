#!/usr/bin/env bash
# Install/refresh the Dhan token systemd units. Idempotent — run after any
# redeploy that touches ops/gcp/. Usage: sudo bash install_dhan_token_units.sh
#
# Units installed:
#   dhan-token-refresh.{service,timer} — daily mint at 02:30 UTC (08:00 IST)
#   dhan-token-guard.{service,timer}   — validity probe every 15 min during
#                                        market hours; auto-refreshes on 401
#
# Hardening notes (2026-07-07 incident: script lost its exec bit -> 203/EXEC
# -> stack ran token-less until 10:52 IST):
#   - ExecStart uses `/bin/bash <script>` — immune to a lost executable bit
#   - Persistent=true on both timers — a missed window fires at boot
#   - the guard is the retry path: a failed 08:00 refresh is healed within
#     15 minutes of the first market-hours probe
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)"; exit 1; }

REPO=/opt/option_trading

cat > /etc/systemd/system/dhan-token-refresh.service <<EOF
[Unit]
Description=Dhan access token daily renewal via TOTP (mint + verify + recreate containers)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $REPO/ops/gcp/dhan_token_refresh.sh
EOF

cat > /etc/systemd/system/dhan-token-refresh.timer <<EOF
[Unit]
Description=Run Dhan token renewal daily pre-market (08:00 IST)

[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/dhan-token-guard.service <<EOF
[Unit]
Description=Dhan token validity probe (auto-refresh on 401)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $REPO/ops/gcp/dhan_token_guard.sh
EOF

# 03:00-10:00 UTC = 08:30-15:30 IST, Mon-Fri: covers pre-open check through close.
cat > /etc/systemd/system/dhan-token-guard.timer <<EOF
[Unit]
Description=Probe Dhan token every 15 min during market hours

[Timer]
OnCalendar=Mon..Fri *-*-* 03..09:00,15,30,45:00 UTC
OnCalendar=Mon..Fri *-*-* 10:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now dhan-token-refresh.timer dhan-token-guard.timer
echo "installed. next runs:"
systemctl list-timers dhan-token-refresh.timer dhan-token-guard.timer --no-pager
