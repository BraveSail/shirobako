#!/usr/bin/env bash
set -euo pipefail

image=${1:?image required}
arch=${2:?amd64 or arm64 required}
platform="linux/$arch"
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = "$platform"

# Keep the network boundary deterministic. The stub accepts the bot's SOCKS5
# greeting on loopback and holds the connection while Docker runs its probe.
stub=$(docker run -d --network none --platform "$platform" "$image" python -uc '
import socket
with socket.create_server(("127.0.0.1", 1080)) as listener:
    waiting = []
    while True:
        conn, _ = listener.accept()
        if conn.recv(1) == bytes([5]):
            print("SOCKS5")
        waiting.append(conn)
')
cid=
cleanup() {
    if [[ -n "$cid" ]]; then docker rm -f "$cid" >/dev/null; fi
    docker rm -f "$stub" >/dev/null
}
trap cleanup EXIT
cid=$(docker run -d --network "container:$stub" --platform "$platform" \
    -e BOT_PROXY=socks5://127.0.0.1:1080 \
    -e API_ID=12345 -e API_HASH=00000000000000000000000000000000 -e BOT_TOKEN=12345:offline \
    --health-cmd "python scripts/ci/image-health.py $arch" \
    --health-interval 2s --health-timeout 60s --health-retries 30 "$image")
for ((i = 0; i < 60; i++)); do
    state=$(docker inspect --format '{{.State.Status}}/{{.State.Health.Status}}' "$cid")
    case "$state" in
        running/healthy)
            if [[ $(docker logs "$stub") == *SOCKS5* ]]; then
                echo "$platform: healthy; bot reached the offline SOCKS5 endpoint"
                exit 0
            fi
            sleep 1 ;;
        running/starting) sleep 2 ;;
        *) break ;;
    esac
done
docker logs --tail 30 "$cid" >&2
docker inspect --format '{{json .State.Health}}' "$cid" >&2
exit 1
