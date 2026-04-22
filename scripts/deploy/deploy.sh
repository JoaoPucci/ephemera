#!/usr/bin/env bash
# Ephemera server-side deploy script.
#
# Invoked by /usr/local/sbin/ephemera-deploy-entry (the root-owned
# forced-command target for the `deploy` SSH user) as:
#
#     sudo -u ephemera /opt/ephemera/scripts/deploy/deploy.sh <TAG>
#
# The entry stub has already:
#   - validated TAG against ^v[0-9]+\.[0-9]+\.[0-9]+$
#   - acquired flock /var/lock/ephemera-deploy.lock
#
# This script re-runs the regex defensively (never trust input twice-removed)
# and then walks the full deploy sequence. On any failure it exits nonzero
# and loudly; no auto-rollback. The CI job surfaces the failing step; the
# operator rolls back manually per docs/deployment.md if needed.

set -euo pipefail
set +x   # never echo (even though this script holds no secrets)

TAG="${1:-}"
APP_DIR=/opt/ephemera
VENV_DIR="$APP_DIR/venv"
DB_DIR=/var/lib/ephemera
BACKUP_DIR=/var/backups/ephemera
MIN_FREE_KB=512000   # 500 MB free required on each of DB_DIR and BACKUP_DIR
HEALTHZ_URL=http://127.0.0.1:8000/healthz
HEALTHZ_MAX_RETRIES=20
HEALTHZ_RETRY_INTERVAL=1
ISACTIVE_MAX_RETRIES=30
ISACTIVE_RETRY_INTERVAL=0.5

die() { echo "DEPLOY FAILED: $*" >&2; exit 1; }

# --- 0. Re-validate tag -----------------------------------------------------

if [[ -z "$TAG" ]]; then
  die "no tag argument passed"
fi
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die "tag '$TAG' does not match vMAJOR.MINOR.PATCH"
fi

echo "==> Deploying $TAG"

# --- 1. Pre-flight disk check -----------------------------------------------

for d in "$DB_DIR" "$BACKUP_DIR"; do
  avail=$(df --output=avail "$d" | tail -n1 | tr -d ' ')
  if (( avail < MIN_FREE_KB )); then
    die "less than $((MIN_FREE_KB/1024)) MB free on $d (have ${avail} KB)"
  fi
done

# --- 2. Pre-deploy backup ---------------------------------------------------

echo "==> Running pre-deploy backup"
sudo /etc/cron.daily/ephemera-backup || die "pre-deploy backup invocation failed"

BACKUP_FILE="$BACKUP_DIR/ephemera-$(date +%F).db"
if [[ ! -s "$BACKUP_FILE" ]]; then
  die "backup file $BACKUP_FILE missing or empty"
fi
backup_size=$(stat -c '%s' "$BACKUP_FILE")
if (( backup_size < 8192 )); then
  die "backup file $BACKUP_FILE is suspiciously small (${backup_size} bytes < 8192)"
fi
if [[ "$(sqlite3 "$BACKUP_FILE" 'pragma integrity_check;')" != "ok" ]]; then
  die "backup file $BACKUP_FILE failed pragma integrity_check"
fi
echo "==> Backup OK: $BACKUP_FILE (${backup_size} bytes)"

# --- 3. Git sync ------------------------------------------------------------

echo "==> Fetching tags"
git -C "$APP_DIR" fetch --tags --force --quiet || die "git fetch failed"
if ! git -C "$APP_DIR" rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null; then
  die "tag $TAG not reachable on origin after fetch"
fi

# --- 4. Checkout ------------------------------------------------------------

echo "==> Checking out $TAG"
git -C "$APP_DIR" checkout --quiet "$TAG" || die "git checkout $TAG failed"

# --- 5. Dependencies --------------------------------------------------------

echo "==> Installing dependencies (--require-hashes)"
"$VENV_DIR/bin/pip" install --require-hashes --quiet -r "$APP_DIR/requirements.txt" || die "pip install failed"

# --- 6. Restart service -----------------------------------------------------

echo "==> Restarting ephemera.service"
sudo /bin/systemctl restart ephemera || die "systemctl restart failed"

# --- 7. is-active gate ------------------------------------------------------

echo "==> Waiting for ephemera.service to reach active state"
state=
for _ in $(seq 1 "$ISACTIVE_MAX_RETRIES"); do
  state=$(sudo /bin/systemctl is-active ephemera || true)
  if [[ "$state" == "active" ]]; then
    break
  fi
  if [[ "$state" == "failed" ]]; then
    die "ephemera.service entered failed state"
  fi
  sleep "$ISACTIVE_RETRY_INTERVAL"
done
if [[ "$state" != "active" ]]; then
  die "ephemera.service did not reach active within $(awk "BEGIN{print $ISACTIVE_MAX_RETRIES * $ISACTIVE_RETRY_INTERVAL}")s (last state: $state)"
fi

# --- 8. Healthz poll --------------------------------------------------------

echo "==> Polling $HEALTHZ_URL"
for _ in $(seq 1 "$HEALTHZ_MAX_RETRIES"); do
  body=$(curl -sf --max-time 3 "$HEALTHZ_URL" || true)
  if [[ -n "$body" ]] && echo "$body" | grep -Fq '"ok":true'; then
    echo "==> Healthz OK"
    logger -t ephemera-deploy "deployed $TAG at $(date -Is)"
    echo "==> Deploy of $TAG complete"
    exit 0
  fi
  sleep "$HEALTHZ_RETRY_INTERVAL"
done

die "healthz never returned ok:true after $HEALTHZ_MAX_RETRIES attempts"
