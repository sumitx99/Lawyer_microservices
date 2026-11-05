#!/bin/sh
# Start the API: bring the database schema up to date, then serve on $PORT.
set -e
python -m pymicroservices.app.migrate upgrade
exec uvicorn pymicroservices.app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-1}"
