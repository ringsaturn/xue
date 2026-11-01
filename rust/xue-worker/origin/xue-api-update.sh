#!/bin/sh
# Runs as root from xue-api-update.timer: install the binary of the rolling
# `api-server` prerelease when it differs from the one installed, restart,
# and put the old binary back if the new one does not answer /health.
#
# The release is deleted and recreated on every build, so for a moment it
# may have no assets, or a .sha256 of one build and a binary of the next.
# Either case just waits for the next run. A build that failed its health
# check is remembered and not retried until the release moves on.
set -eu

REPO=${XUE_API_REPO:-ringsaturn/xue}
ASSET=xue-api-server-x86_64-linux-gnu
BASE="https://github.com/$REPO/releases/download/api-server"
BIN=/usr/local/bin/xue-api-server
STATE=${STATE_DIRECTORY:-/var/lib/xue-api-update}

listen=$(sed -n 's/^XUE_API_LISTEN=//p' /etc/xue-api/env 2>/dev/null | tail -n 1)
listen=${listen:-127.0.0.1:8787}
HEALTH="http://127.0.0.1:${listen##*:}/health"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

if ! curl -fsSL --max-time 30 -o "$ASSET.sha256" "$BASE/$ASSET.sha256"; then
  echo "no $ASSET.sha256 in the api-server release now; trying next run"
  exit 0
fi
want=$(cut -d ' ' -f 1 "$ASSET.sha256")
case $want in
  *[!0-9a-f]* | '') echo "unreadable $ASSET.sha256"; exit 0 ;;
esac
[ ${#want} -eq 64 ] || { echo "unreadable $ASSET.sha256"; exit 0; }

have=$( [ -f "$BIN" ] && sha256sum "$BIN" | cut -d ' ' -f 1 || true)
[ "$want" != "$have" ] || exit 0
if [ "$want" = "$(cat "$STATE/rejected" 2>/dev/null || true)" ]; then
  echo "build $want failed its health check before; waiting for the next one"
  exit 0
fi

if ! curl -fsSL --max-time 300 -o "$ASSET" "$BASE/$ASSET"; then
  echo "could not download $ASSET; trying next run"
  exit 0
fi
if ! sha256sum -c --quiet "$ASSET.sha256"; then
  echo "$ASSET does not match its .sha256 (release being replaced?); trying next run"
  exit 0
fi

healthy() {
  i=0
  while [ $i -lt 15 ]; do
    curl -fsS --max-time 5 -o /dev/null "$HEALTH" && return 0
    i=$((i + 1))
    sleep 2
  done
  return 1
}

restart() {
  systemctl restart xue-api.service
  # The connector is BindsTo= the API: make sure it came back with it.
  if [ -f /etc/xue-api/tunnel.env ]; then
    systemctl start xue-api-tunnel.service
  fi
}

[ -f "$BIN" ] && cp -p "$BIN" "$BIN.prev"
install -m 755 "$ASSET" "$BIN.new"
mv "$BIN.new" "$BIN"
echo "installed ${have:-nothing} -> $want"

if restart && healthy; then
  rm -f "$STATE/rejected"
  echo "xue-api-server $want is up"
  exit 0
fi

echo "$want" > "$STATE/rejected"
if [ -f "$BIN.prev" ]; then
  mv "$BIN.prev" "$BIN"
  restart || true
  echo "build $want failed its health check; put $have back"
else
  echo "build $want failed its health check and there is nothing to put back"
fi
exit 1
