#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "${1:-}" != "--yes" ]; then
  echo "This clears AirScout's saved trip, scan history, and alert deduplication."
  echo "Back up data/listings.sqlite3 first if you need the existing records."
  echo "Run: sh scripts/reset.sh --yes"
  exit 0
fi
cd "$ROOT"
docker compose stop airscout
rm -f data/listings.sqlite3 data/listings.sqlite3-shm data/listings.sqlite3-wal
docker compose up -d --build airscout
echo "AirScout database reset. Create a new trip from http://localhost:8080."
