#!/bin/sh
# Runs as root on the origin host, from deploy.sh: install the binary and the
# units staged in /tmp/xue-api/, install cloudflared if missing, restart.
set -eu
STAGE=/tmp/xue-api

install -m 755 "$STAGE/xue-api-server" /usr/local/bin/xue-api-server.new
mv /usr/local/bin/xue-api-server.new /usr/local/bin/xue-api-server
install -m 644 "$STAGE/xue-api.service" "$STAGE/xue-api-tunnel.service" /etc/systemd/system/
install -d -m 755 /etc/xue-api
if [ -s "$STAGE/tunnel.env" ]; then
  install -m 600 "$STAGE/tunnel.env" /etc/xue-api/tunnel.env
fi
rm -rf "$STAGE"

if ! command -v cloudflared >/dev/null; then
  install -d -m 755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared >/dev/null
fi

systemctl daemon-reload
systemctl enable xue-api.service >/dev/null 2>&1
systemctl restart xue-api.service
# xue-api pulls the tunnel in (Wants=); restart it explicitly so a new token
# takes effect.
if [ -f /etc/xue-api/tunnel.env ]; then
  systemctl restart xue-api-tunnel.service
fi
curl -fsS --max-time 10 http://127.0.0.1:8787/health
echo
systemctl is-active xue-api.service xue-api-tunnel.service || true
