#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
container="webcappi-redis-test-$$"
secrets_dir="$(mktemp -d)"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true; rm -rf "$secrets_dir"' EXIT
docker run --detach --name "$container" -p 127.0.0.1::6379 \
  redis:8.2.2-alpine redis-server --save '' --appendonly no \
  --maxmemory 64mb --maxmemory-policy noeviction >/dev/null
for attempt in {1..50}; do
  if docker exec "$container" redis-cli ping >/dev/null 2>&1; then break; fi
  sleep 0.1
done
address="$(docker port "$container" 6379/tcp)"
WEBCAPPI_REDIS_TEST_ADDR="$address" "${GO:-go}" test -race -count=1 \
  ./internal/httpapi -run 'TestRedis' -timeout 60s
# Repeat the same protocol and sharing scenarios with file-based authentication.
printf '%s\n' 'webcappi-disposable-test-password' > "$secrets_dir/password"
docker exec "$container" redis-cli CONFIG SET requirepass webcappi-disposable-test-password >/dev/null
WEBCAPPI_REDIS_TEST_ADDR="$address" WEBCAPPI_REDIS_TEST_PASSWORD_FILE="$secrets_dir/password" \
  "${GO:-go}" test -race -count=1 ./internal/httpapi -run 'TestRedis' -timeout 60s
