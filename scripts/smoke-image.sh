#!/usr/bin/env bash
# Smoke-test a built Hub image the way the daemon installer runs it (agent-harness #547):
#   1. `--version` prints the expected version;
#   2. as a non-root --user with a bind-mounted state directory, the Hub writes owner-only state there
#      (signin-link) and serves /healthz on the host's loopback (--network host).
# Usage: scripts/smoke-image.sh <image> [expected version]. Linux only (host networking, numeric --user).
set -euo pipefail

image=${1:?usage: smoke-image.sh <image> [expected version]}
expected=${2:-}
port=${HUB_SMOKE_PORT:-18120}

version=$(docker run --rm "$image" --version)
echo "image reports: $version"
if [[ -n "$expected" && "$version" != "harness_hub $expected" ]]; then
  echo "expected harness_hub $expected" >&2
  exit 1
fi

state=$(mktemp -d)
name="hub-smoke-$$"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$state"
}
trap cleanup EXIT
user="$(id -u):$(id -g)"
mount="type=bind,src=$state,dst=/hub-state"

link=$(docker run --rm --user "$user" --mount "$mount" "$image" \
  signin-link --state-dir /hub-state --url "http://127.0.0.1:$port")
[[ "$link" == "http://127.0.0.1:$port/#signin="* ]] || { echo "unexpected sign-in link: $link" >&2; exit 1; }
for path in "$state" "$state/signin-codes.json"; do
  [[ -e "$path" ]] || { echo "missing $path" >&2; ls -la "$state" >&2; exit 1; }
  owner=$(stat -c '%u:%g' "$path")
  [[ "$owner" == "$user" ]] || { echo "$path is owned by $owner, not $user" >&2; exit 1; }
done
[[ $(stat -c '%a' "$state") == 700 ]] || { echo "state directory is not owner-only" >&2; exit 1; }
[[ $(stat -c '%a' "$state/signin-codes.json") == 600 ]] || { echo "sign-in codes are not owner-only" >&2; exit 1; }

docker run -d --name "$name" --network host --user "$user" --mount "$mount" "$image" \
  --daemon-url http://127.0.0.1:9 --state-dir /hub-state --port "$port" >/dev/null
for _ in $(seq 1 30); do
  if health=$(curl -fsS "http://127.0.0.1:$port/healthz" 2>/dev/null); then
    echo "healthz: $health"
    [[ -z "$expected" || "$health" == *"\"version\":\"$expected\""* ]] || { echo "healthz version mismatch" >&2; exit 1; }
    [[ $(docker inspect -f '{{.State.Running}}' "$name") == true ]] || { echo "Hub exited" >&2; exit 1; }
    echo "image smoke test passed"
    exit 0
  fi
  sleep 1
done
echo "Hub did not answer /healthz" >&2
docker logs "$name" >&2 || true
exit 1
