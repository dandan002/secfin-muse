#!/bin/sh
# Runs once, on the fresh data directory, while Postgres listens only on its socket: the mock (which
# connects over TCP) can't start until the snapshot is in. No snapshot yet means the first session.
set -eu
[ -n "${SNAPSHOT_URL:-}" ] || exit 0

if aws s3 cp --only-show-errors "$SNAPSHOT_URL" /tmp/restore.sql.gz 2> /tmp/restore.err; then
  gunzip -c /tmp/restore.sql.gz | psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" > /dev/null
  rm -f /tmp/restore.sql.gz
  echo "snapshot: restored $SNAPSHOT_URL"
elif grep -q '(404)' /tmp/restore.err; then
  echo "snapshot: none at $SNAPSHOT_URL yet, starting empty"
else
  # Starting empty here would overwrite the snapshot at the next stop, so fail the task instead.
  cat /tmp/restore.err >&2
  exit 1
fi
