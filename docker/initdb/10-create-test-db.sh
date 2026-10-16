#!/bin/bash
# Creates the test database next to the app database (runs once, on an empty data volume).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v dbname="${POSTGRES_TEST_DB:-median_test}" -v owner="$POSTGRES_USER" <<'SQL'
CREATE DATABASE :"dbname" OWNER :"owner";
SQL
