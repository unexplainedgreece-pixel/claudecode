#!/usr/bin/env bash
# Run the whole test suite against PostgreSQL instead of SQLite.
#   TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/social_test tests/run_postgres.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to an empty PostgreSQL database}"
export DATABASE_URL="$TEST_DATABASE_URL"
python3 -m unittest discover -s tests "$@"
