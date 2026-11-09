#!/bin/sh
# radio-relay setup for Synology Audio Station internet radio.
#
#   sudo sh setup.sh          full install / repair (safe to run again)
#   sudo sh setup.sh --boot   only re-add the /etc/hosts line (for a DSM boot-up task)
#
# What the full install changes (all undone by uninstall.sh):
#  1. builds + starts the "radio-relay" Docker container (restarts on boot)
#  2. /etc/hosts: "<NAS_IP> yp.shoutcast.com" so Audio Station's SHOUTcast list goes via the relay
#     (backup: /etc/hosts.bak-radio-relay)
#  3. DSM Reverse Proxy rule: http://yp.shoutcast.com:80 -> http://127.0.0.1:<RELAY_PORT>
#     (Control Panel > Login Portal > Advanced > Reverse Proxy)
#  4. your User-defined + Favourite radio URLs are pointed at the relay
#     (the original URL stays inside ?u=...; backup: /usr/syno/etc/radio/*.bak-radio-relay)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

[ "$(id -u)" = "0" ] || { echo "Please run with sudo:  sudo sh $DIR/setup.sh"; exit 1; }

# ---------- settings (.env) ----------
detect_ip() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}
detect_dns() {
  awk '$1=="nameserver" && $2 !~ /^127\./ {print $2; exit}' /etc/resolv.conf
}
if [ ! -f .env ]; then
  NAS_IP_DETECTED=$(detect_ip)
  DNS_DETECTED=$(detect_dns)
  sed -e "s/^NAS_IP=.*/NAS_IP=${NAS_IP_DETECTED}/" -e "s/^RELAY_DNS=.*/RELAY_DNS=${DNS_DETECTED}/" .env.example > .env
  echo "created .env (NAS_IP=${NAS_IP_DETECTED}, RELAY_DNS=${DNS_DETECTED})"
fi
# shellcheck disable=SC1091
. ./.env
NAS_IP=${NAS_IP:-$(detect_ip)}
RELAY_PORT=${RELAY_PORT:-18570}
[ -n "$NAS_IP" ] || { echo "Could not detect the NAS IP. Set NAS_IP in $DIR/.env"; exit 1; }
case "$NAS_IP" in 127.*) echo "NAS_IP must be the LAN IP, not $NAS_IP"; exit 1;; esac
RELAY="http://${NAS_IP}:${RELAY_PORT}"
HOSTLINE="${NAS_IP} yp.shoutcast.com  # radio-relay"

ensure_hosts() {
  if grep -q "yp\.shoutcast\.com  # radio-relay" /etc/hosts; then
    if ! grep -qx "$HOSTLINE" /etc/hosts; then
      sed -i "/yp\.shoutcast\.com  # radio-relay/d" /etc/hosts
      echo "$HOSTLINE" >> /etc/hosts
      echo "updated: $HOSTLINE"
    else
      echo "ok: $HOSTLINE"
    fi
  elif grep -q "yp\.shoutcast\.com" /etc/hosts; then
    echo "WARNING: /etc/hosts already has a different yp.shoutcast.com entry:"
    grep "yp\.shoutcast\.com" /etc/hosts
  else
    [ -f /etc/hosts.bak-radio-relay ] || cp /etc/hosts /etc/hosts.bak-radio-relay
    echo "$HOSTLINE" >> /etc/hosts
    echo "added: $HOSTLINE"
  fi
}

if [ "$1" = "--boot" ]; then
  ensure_hosts
  exit 0
fi

echo "== 1/4 building and starting the radio-relay container"
command -v docker >/dev/null || { echo "docker not found - install Container Manager from Package Center first"; exit 1; }
docker compose -f compose.yml up -d --build
i=0
until curl -fsS -m 2 "http://127.0.0.1:${RELAY_PORT}/health" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 40 ] && { echo "relay did not come up:"; docker logs --tail 30 radio-relay; exit 1; }
  sleep 1
done
curl -fsS "http://127.0.0.1:${RELAY_PORT}/health" | head -2

echo "== 2/4 /etc/hosts"
ensure_hosts

echo "== 3/4 DSM reverse proxy: yp.shoutcast.com:80 -> 127.0.0.1:${RELAY_PORT}"
if /usr/syno/bin/synowebapi --exec api=SYNO.Core.AppPortal.ReverseProxy method=list version=1 2>/dev/null | grep -q '"yp.shoutcast.com"'; then
  echo "rule already exists"
else
  /usr/syno/bin/synowebapi --exec api=SYNO.Core.AppPortal.ReverseProxy method=create version=1 \
    entry='{"description":"radio-relay (Audio Station SHOUTcast fix)","proxy_connect_timeout":60,"proxy_read_timeout":60,"proxy_send_timeout":60,"proxy_http_version":1,"proxy_intercept_errors":false,"frontend":{"acl":null,"fqdn":"yp.shoutcast.com","port":80,"protocol":0,"https":{"hsts":false}},"backend":{"fqdn":"127.0.0.1","port":'"${RELAY_PORT}"',"protocol":0},"customize_headers":[]}' >/dev/null || true
  sleep 2
fi

echo "== 4/4 pointing User-defined + Favourite radio at the relay"
python3 -I - "$RELAY" <<'EOF'
import json, os, shutil, sys, urllib.parse
relay = sys.argv[1]
for name in ('userdef.json', 'favorite.json'):
    p = '/usr/syno/etc/radio/' + name
    if not os.path.exists(p):
        print(name, 'not found (no stations yet)')
        continue
    d = json.load(open(p))
    changed = 0
    for st in d.get('stations', []):
        u = st.get('url') or ''
        if '/s?u=' in u or 'yp.shoutcast.com' in u or not u.startswith(('http://', 'https://')):
            continue
        st['url'] = relay + '/s?u=' + urllib.parse.quote(u, safe='')
        changed += 1
    if changed:
        if not os.path.exists(p + '.bak-radio-relay'):
            shutil.copy2(p, p + '.bak-radio-relay')
        st_ = os.stat(p)
        json.dump(d, open(p, 'w'), indent=3, ensure_ascii=False)
        os.chown(p, st_.st_uid, st_.st_gid)
    print(name, 'stations pointed at relay:', changed)
EOF

echo "== check: a SHOUTcast playlist now goes through the relay"
if curl -fsS -m 10 "http://yp.shoutcast.com/sbin/tunein-station.pls?id=99554421" | grep -m1 "^File1=${RELAY}/s?u="; then
  echo "OK"
else
  echo "WARNING: playlist check failed - see Troubleshooting in README.md"
fi
echo "== done. Open Audio Station and play a radio station."
