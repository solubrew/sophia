#!/bin/sh
# Bootstrap script: create the `sophia` database on the shared
# ncho-postgres if it doesn't already exist. Invoked by
# docker-compose.cluster.yml's `bootstrap` service.
set -e
PGPASSWORD="$PGPASSWORD" psql -h ncho-postgres -U app -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='sophia'" | grep -q 1 \
    || PGPASSWORD="$PGPASSWORD" psql -h ncho-postgres -U app -d postgres \
    -c "CREATE DATABASE sophia"
echo "sophia database ready"
