#!/bin/bash
cd /home/savitasajwan03/option_trading
export PYTHONPATH=/home/savitasajwan03/option_trading
/home/savitasajwan03/option_trading/.venv/bin/python -u -c 'import ml_pipeline_2; print("import_ok")' > /tmp/test_py.log 2>&1
