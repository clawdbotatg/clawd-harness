#!/usr/bin/env bash
# Usage: sudo bash setup_tls.sh fleet.example.com [email@example.com]
#
# Points an nginx vhost at the local relay (127.0.0.1:8788) and gets a Let's
# Encrypt cert via certbot. The domain's A record must already resolve to this
# box. Safe to run on a host already serving other nginx vhosts — it only adds
# one site and does a graceful reload.
set -euo pipefail
DOMAIN="${1:?need a domain, e.g. fleet.zkllmapi.com}"
EMAIL="${2:-admin@${DOMAIN#*.}}"

SITE=/etc/nginx/sites-available/$DOMAIN
tee "$SITE" > /dev/null <<NGINX
server {
  listen 80;
  listen [::]:80;
  server_name $DOMAIN;
  location / {
    proxy_pass http://127.0.0.1:8788;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
  }
}
NGINX
ln -sf "$SITE" /etc/nginx/sites-enabled/$DOMAIN
nginx -t
systemctl reload nginx
echo "[ok] http vhost live; requesting cert (certbot will add the 443 block + redirect)…"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
nginx -t && systemctl reload nginx
echo "[done] relay reachable at: wss://$DOMAIN/ws?role=mobile&t=<TOKEN>"
