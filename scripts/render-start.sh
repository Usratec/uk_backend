#!/usr/bin/env bash
# Container start command on Render's free tier.
#
# This is a prototype arrangement: one Render service runs gunicorn, the Celery worker and the
# beat scheduler together, because Render's Background Worker service type is not on the free
# plan. The trade-off is deliberate, but two things are *not* traded away:
#
#   - no task is silently lost: the broker uses noeviction and CELERY_TASK_ACKS_LATE is on, so a
#     task interrupted mid-flight returns to the queue;
#   - no process dies silently: if gunicorn, the worker or beat exits, this script tears the
#     whole container down (see the supervisor at the bottom) so Render restarts it and the
#     failure is visible in the dashboard rather than leaving a half-dead service answering
#     health checks while background work quietly stops.
#
# On a paid plan, set RUN_WORKER_IN_WEB=false and run `celery --app config worker` and
# `celery --app config beat` as their own Render services, as compose.yaml already does locally.
set -euo pipefail

log() { echo "[start] $*"; }

# --- Preflight -----------------------------------------------------------------------------------
# Checked explicitly, so the logs say which dependency is wrong instead of failing later inside a
# request or a task. Connection URIs are logged with their credentials masked by Kombu.

log "running migrations"
python manage.py migrate --noinput

# Staff role groups are derived from code, so they are re-synced on every deploy.
log "syncing staff roles"
python manage.py sync_roles

log "checking the cache and the Celery broker"
python manage.py shell --command "
from django.core.cache import cache
from config.celery import app

cache.set('render-start-probe', 'ok', 30)
assert cache.get('render-start-probe') == 'ok', 'cache did not return what was written'
print('[start] cache: reachable')

connection = app.connection()
connection.ensure_connection(max_retries=3)
print(f'[start] broker: reachable at {connection.as_uri()}')
connection.release()
"

# --- Processes -----------------------------------------------------------------------------------

pids=()

if [ "${RUN_WORKER_IN_WEB:-false}" = "true" ]; then
    # --beat embeds the scheduler in the worker process. Celery documents that as development-
    # only; it is used here because the alternative on a free plan is no scheduled jobs at all.
    # --pool=solo keeps the worker to one process: a prefork pool would fork a second copy of
    # Django, and three copies do not fit in the free plan's 512 MB.
    # The schedule file goes in /tmp because that is writable whatever the platform does to /app.
    queues="${CELERY_QUEUES:-celery,${VIDEO_TASK_QUEUE:-video}}"
    log "starting the Celery worker with embedded beat on queues: $queues"
    celery --app config worker \
        --beat \
        --pool=solo \
        --queues "$queues" \
        --schedule=/tmp/celerybeat-schedule \
        --loglevel "${CELERY_LOG_LEVEL:-info}" &
    pids+=($!)
else
    log "RUN_WORKER_IN_WEB is not true: no worker in this container"
fi

log "starting gunicorn on port ${PORT:-8000}"
# Threads rather than more workers: 512 MB does not fit a second copy of Django, but threads
# share one. Requests here wait on the database and on Stripe far more than they use the CPU, so
# one worker that can only hold one request at a time serialises everything — the storefront
# polling after a payment was enough to put Stripe's own webhook in a queue behind it, which
# delayed the very access it was polling for.
gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --worker-class gthread \
    --threads "${WEB_THREADS:-8}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile - &
pids+=($!)

# --- Supervisor ----------------------------------------------------------------------------------
# `wait -n` returns as soon as *any* child exits. Whichever it was, the rest are stopped and the
# container exits non-zero, so a dead worker cannot go unnoticed behind a healthy web process.

shutdown() {
    log "received a stop signal, shutting down"
    kill -TERM "${pids[@]}" 2>/dev/null || true
    wait || true
    exit 0
}
trap shutdown TERM INT

log "all processes started; supervising ${#pids[@]}"

wait -n || true
log "a supervised process exited; stopping the container so Render restarts it"
kill -TERM "${pids[@]}" 2>/dev/null || true
wait || true
exit 1
