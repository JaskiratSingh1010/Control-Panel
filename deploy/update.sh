#!/usr/bin/env bash
# One command to put new code live on the server.
#
# NOT RUN YET. Copy to the server, `chmod +x update.sh`, then: ./update.sh
#
# Replaces the current by-hand routine of: pull, stop, start, hope.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/control-panel}"
BRANCH="${BRANCH:-production}"

cd "$APP_DIR"

echo "==> 1/6  saving a copy of the database first"
if [ -f db.sqlite3 ]; then
  cp db.sqlite3 "db.sqlite3.backup-$(date +%Y%m%d-%H%M%S)"
  ls -1t db.sqlite3.backup-* | tail -n +11 | xargs -r rm --   # keep the last 10
fi

echo "==> 2/6  fetching $BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> 3/6  installing any new packages"
.venv/bin/pip install -q -r requirements.txt

echo "==> 4/6  applying database changes"
.venv/bin/python manage.py migrate --noinput

echo "==> 5/6  rebuilding static files (compressed + cache-friendly names)"
.venv/bin/python manage.py collectstatic --noinput

echo "==> 6/6  restarting the site"
sudo systemctl restart control-panel
sleep 3
systemctl is-active --quiet control-panel && echo "OK - site is running" || {
  echo "FAILED - check: journalctl -u control-panel -n 50"; exit 1; }

# Fill the caches so the first real visitor does not wait on SAP.
.venv/bin/python manage.py warm_kpi_cache || true
echo "==> done"
