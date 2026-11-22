#!/usr/bin/env bash
# Gateway: serve the exam hub at https://exam.glasslab.org/exam/ (campus only).
# Run with sudo ON THE GATEWAY, after Squarespace has
#   exam.glasslab.org                A      134.74.146.39
#   _acme-challenge.exam.glasslab.org CNAME (same acme-dns target as
#                                     _acme-challenge.glasslab.org)
# Issues a separate one-name certificate (acme-dns keeps only two TXT values,
# both used by the glasslab.org + www certificate), then enables the site.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if [ ! -s /etc/letsencrypt/live/exam.glasslab.org/fullchain.pem ]; then
  certbot certonly -n --manual --preferred-challenges dns \
    --manual-auth-hook /etc/letsencrypt/acme-dns-auth.py \
    --deploy-hook 'systemctl reload apache2' \
    --cert-name exam.glasslab.org -d exam.glasslab.org
fi

STAMP=$(date +%Y%m%d-%H%M%S)
tar -C /etc -czf "/root/apache2-bak-$STAMP-exam.tgz" apache2
install -m 644 "$HERE/exam-glasslab.conf" /etc/apache2/sites-available/exam-glasslab.conf
a2ensite -q exam-glasslab
if ! apache2ctl configtest; then
  echo "configtest failed; disabling exam-glasslab again." >&2
  a2dissite -q exam-glasslab
  apache2ctl configtest && systemctl reload apache2
  exit 1
fi
systemctl reload apache2
echo "exam.glasslab.org enabled. Apache backup: /root/apache2-bak-$STAMP-exam.tgz"
