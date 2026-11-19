#!/usr/bin/env bash
# Install or update the Mastersportal scraper app on Ubuntu 22.04/24.04.
#   sudo bash deploy/install.sh scraper.yourdomain.com   # with HTTPS (recommended)
#   sudo bash deploy/install.sh                          # no domain: http://SERVER_IP (less secure)
# Safe to re-run for updates: keeps /etc/mpscraper.env and the database.
set -euo pipefail

DOMAIN="${1:-}"
APP=/opt/mpscraper
DATA=/var/lib/mpscraper
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx >/dev/null

echo "==> Creating service user and folders"
id -u mpscraper >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin mpscraper
mkdir -p "$APP" "$DATA"
cp -r "$SRC"/app.py "$SRC"/db.py "$SRC"/scraper.py "$SRC"/exports.py "$SRC"/manage.py "$SRC"/wsgi.py \
      "$SRC"/requirements.txt "$SRC"/templates "$APP"/
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

# Bring in an existing mastersportal.sqlite (from the CLI script) if it sits next to the app folder.
if [ -f "$SRC/mastersportal.sqlite" ] && [ ! -f "$DATA/mastersportal.sqlite" ]; then
  echo "==> Importing existing mastersportal.sqlite"
  cp "$SRC/mastersportal.sqlite" "$DATA/mastersportal.sqlite"
fi
chown -R mpscraper:mpscraper "$DATA"
chmod 750 "$DATA"

if [ ! -f /etc/mpscraper.env ]; then
  echo "==> Writing /etc/mpscraper.env"
  SECURE=1; [ -z "$DOMAIN" ] && SECURE=0
  cat > /etc/mpscraper.env <<EOF
MP_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
MP_DB=$DATA/mastersportal.sqlite
MP_COOKIE_SECURE=$SECURE
MP_USER_AGENT="Mozilla/5.0 (research crawler; contact: you@example.com)"
EOF
  chmod 600 /etc/mpscraper.env
fi

cat > /usr/local/bin/mpscraper-manage <<EOF
#!/bin/sh
exec runuser -u mpscraper -- $APP/venv/bin/python $APP/manage.py --db $DATA/mastersportal.sqlite "\$@"
EOF
chmod 755 /usr/local/bin/mpscraper-manage

echo "==> Starting the app service"
cp "$SRC/deploy/mpscraper.service" /etc/systemd/system/mpscraper.service
systemctl daemon-reload
systemctl enable -q mpscraper
systemctl restart mpscraper

echo "==> Configuring nginx"
cat > /etc/nginx/sites-available/mpscraper <<EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
EOF
ln -sf /etc/nginx/sites-available/mpscraper /etc/nginx/sites-enabled/mpscraper
[ -z "$DOMAIN" ] && rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null
fi

if [ -n "$DOMAIN" ]; then
  echo "==> Getting an HTTPS certificate for $DOMAIN (the domain's DNS must already point here)"
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  certbot --nginx -d "$DOMAIN" --redirect || echo "!! certbot failed: fix DNS, then run: sudo certbot --nginx -d $DOMAIN --redirect"
fi

sleep 2
systemctl is-active -q mpscraper && echo "==> App is running." || { echo "!! App failed to start:"; journalctl -u mpscraper -n 30 --no-pager; exit 1; }
if [ -z "$(mpscraper-manage list-users)" ]; then
  echo
  echo "Create your admin login next:"
  echo "    sudo mpscraper-manage create-user you@example.com --role admin"
fi
if [ -n "$DOMAIN" ]; then echo "Then open: https://$DOMAIN"; else echo "Then open: http://YOUR_SERVER_IP"; fi
