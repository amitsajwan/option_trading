#!/bin/bash
sudo docker inspect option_trading-strategy_app-1 --format='{{range .Config.Env}}{{.}}{{"\n"}}{{end}}' | grep -E 'REDIS|ENTRY_ML|ENTRY_VOL|STRATEGY_PROFILE|MARKET' | sort
