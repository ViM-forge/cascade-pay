#!/bin/sh
# Host-only D6; use a dedicated demo project: its Redis is cleared.
set -eu
: "${API_URL:=http://127.0.0.1:8000}"
export API_URL
exec poetry run python scripts/demo.py restart-recovery
