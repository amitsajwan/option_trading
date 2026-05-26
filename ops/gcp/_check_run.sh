#!/usr/bin/env bash
RUN_ID="${1:-731bad6f-786b-4843-8a4e-e44c899cfc10}"
curl -s "http://127.0.0.1:8008/api/strategy/evaluation/runs/${RUN_ID}" | python3 -m json.tool | head -80
