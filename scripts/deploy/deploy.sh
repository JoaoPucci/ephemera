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
MIN_FREE_KB=512000           # 500 MB free required on each of DB_DIR and BACKUP_DIR
MAX_BACKUP_AGE_SECONDS=129600  # 36h: daily cron cadence is ~24h, so >36h means
                               # at least one run was missed -- abort the deploy
                               # before it can compound on a broken backup posture
HEALTHZ_URL=http://127.0.0.1:8000/healthz
HEALTHZ_MAX_RETRIES=20
HEALTHZ_RETRY_INTERVAL=1
ISACTIVE_MAX_RETRIES=30
ISACTIVE_RETRY_INTERVAL=0.5

# Anchor cwd to a directory ephemera can read. Without this, `find` (and any
# other chdir-and-restore tool) errors on exit when the caller's cwd is
# unreadable to ephemera -- which is the case both from an admin shell (the
# admin's $HOME is 0700) and from the CI forced-command path (/home/deploy/
# is 0700 too). The find stderr is silenced, the pipeline's nonzero exit
# cascades through pipefail, and the script appears to hang silently.
cd "$APP_DIR"

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

# --- 2. Verify the latest daily backup -------------------------------------
# Deploy doesn't take its own backup -- /etc/cron.daily/ephemera-backup runs
# on the daily cadence, writes ephemera-YYYY-MM-DD.db, retains 30 days.
# Instead of duplicating the backup logic (with its own filename scheme and
# retention class), we assert the latest one is fresh and intact before
# touching code. If the cron is broken and the last snapshot is >36h old, we
# refuse to deploy -- a broken backup posture should stop the pipeline, not
# ride along silently.

echo "==> Verifying latest daily backup"
LATEST_BACKUP=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'ephemera-*.db' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-)
if [[ -z "$LATEST_BACKUP" ]]; then
  die "no ephemera-*.db backup found in $BACKUP_DIR (is /etc/cron.daily/ephemera-backup installed?)"
fi
backup_mtime=$(stat -c '%Y' "$LATEST_BACKUP")
backup_age_seconds=$(( $(date +%s) - backup_mtime ))
if (( backup_age_seconds > MAX_BACKUP_AGE_SECONDS )); then
  die "latest backup $LATEST_BACKUP is $((backup_age_seconds / 3600))h old (>$((MAX_BACKUP_AGE_SECONDS / 3600))h); daily cron may be broken"
fi
backup_size=$(stat -c '%s' "$LATEST_BACKUP")
if (( backup_size < 8192 )); then
  die "latest backup $LATEST_BACKUP is suspiciously small (${backup_size} bytes < 8192)"
fi
if [[ "$(sqlite3 "$LATEST_BACKUP" 'pragma integrity_check;')" != "ok" ]]; then
  die "latest backup $LATEST_BACKUP failed pragma integrity_check"
fi
echo "==> Backup OK: $LATEST_BACKUP (${backup_size} bytes, $((backup_age_seconds / 3600))h old)"

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
