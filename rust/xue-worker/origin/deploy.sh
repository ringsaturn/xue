#!/bin/sh
# Build xue-api-server for linux x86_64 and install it on an origin host.
#
#   rust/xue-worker/origin/deploy.sh <ssh-target>
#
# Needs cargo-zigbuild and zig on PATH (`nix shell nixpkgs#zig
# nixpkgs#cargo-zigbuild -c …` works without installing either), and sudo on
# the host. XUE_API_TUNNEL_TOKEN, when set, is written to the host's
# /etc/xue-api/tunnel.env; otherwise the token already there is kept.
set -eu
TARGET=${1:?usage: deploy.sh <ssh-target>}
HERE=$(cd "$(dirname "$0")" && pwd)
RUST=$(cd "$HERE/../.." && pwd)
TRIPLE=x86_64-unknown-linux-gnu

# glibc 2.35 (Ubuntu 22.04) is the oldest host the binary has to run on.
(cd "$RUST" && cargo zigbuild -p xue-worker --features server --bin xue-api-server \
  --release --target "$TRIPLE.2.35")

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp "$RUST/target/$TRIPLE/release/xue-api-server" "$HERE/xue-api.service" \
  "$HERE/xue-api-tunnel.service" "$STAGE/"
if [ -n "${XUE_API_TUNNEL_TOKEN:-}" ]; then
  (umask 077 && printf 'TUNNEL_TOKEN=%s\n' "$XUE_API_TUNNEL_TOKEN" > "$STAGE/tunnel.env")
fi
ssh "$TARGET" 'rm -rf /tmp/xue-api && mkdir -m 700 /tmp/xue-api'
scp -q "$STAGE"/* "$TARGET:/tmp/xue-api/"
ssh "$TARGET" 'sudo sh -s' < "$HERE/install.sh"
