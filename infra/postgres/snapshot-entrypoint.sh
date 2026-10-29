#!/bin/sh
# PID 1 of the container: runs the official entrypoint, and when ECS stops the task (SIGTERM, after
# the mock has already stopped) saves a snapshot before shutting Postgres down.
set -u

docker-entrypoint.sh "$@" &
pid=$!

save() {
  if [ -n "${SNAPSHOT_URL:-}" ] && pg_isready -q -h 127.0.0.1 -U "$POSTGRES_USER"; then
    if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      --no-owner --no-privileges -f /tmp/snapshot.sql \
      && gzip -f /tmp/snapshot.sql \
      && aws s3 cp --only-show-errors /tmp/snapshot.sql.gz "$SNAPSHOT_URL"; then
      echo "snapshot: saved $SNAPSHOT_URL ($(wc -c < /tmp/snapshot.sql.gz) bytes)"
    else
      echo "snapshot: save failed, the previous one stays" >&2
    fi
  fi
  kill -INT "$pid" # Postgres' fast shutdown
}
trap save TERM INT

wait "$pid" # returns early when the trap fires
wait "$pid"
