#!/bin/sh
# Undo everything setup.sh did:   sudo sh uninstall.sh
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"
[ "$(id -u)" = "0" ] || { echo "Please run with sudo:  sudo sh $DIR/uninstall.sh"; exit 1; }

echo "== stopping and removing the container"
docker compose -f compose.yml down --rmi local 2>/dev/null || docker rm -f radio-relay 2>/dev/null

echo "== /etc/hosts"
sed -i '/yp\.shoutcast\.com  # radio-relay/d' /etc/hosts && echo "removed yp.shoutcast.com line"

echo "== DSM reverse proxy rule"
UUIDS=$(/usr/syno/bin/synowebapi --exec api=SYNO.Core.AppPortal.ReverseProxy method=list version=1 2>/dev/null \
  | python3 -I -c "import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
data=d.get('data',d)
print(' '.join(e['UUID'] for e in data.get('entries',[]) if e.get('frontend',{}).get('fqdn')=='yp.shoutcast.com'))")
for u in $UUIDS; do
  /usr/syno/bin/synowebapi --exec api=SYNO.Core.AppPortal.ReverseProxy method=delete version=1 uuids="[\"$u\"]" >/dev/null && echo "deleted rule $u"
done
[ -n "$UUIDS" ] || echo "no rule found (or delete it by hand: Control Panel > Login Portal > Advanced > Reverse Proxy)"

echo "== DSM boot task"
if /usr/syno/bin/synowebapi --exec api=SYNO.Core.EventScheduler method=get version=1 task_name='"radio-relay hosts"' 2>/dev/null | grep -q '"success" *: *true'; then
  /usr/syno/bin/synowebapi --exec api=SYNO.Core.EventScheduler method=delete version=1 task_name='"radio-relay hosts"' >/dev/null && echo 'deleted task "radio-relay hosts"'
else
  echo "no boot task found"
fi

echo "== radio station URLs"
for f in userdef.json favorite.json; do
  if [ -f /usr/syno/etc/radio/$f.bak-radio-relay ]; then
    cp -p /usr/syno/etc/radio/$f.bak-radio-relay /usr/syno/etc/radio/$f && echo "restored $f from backup"
  fi
done
echo "radio-relay removed."
