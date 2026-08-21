#!/usr/bin/env bash
# Render Caddyfile from template env vars, then run caddy.
set -euo pipefail

: "${DOMAIN:?DOMAIN must be set}"
: "${CADDY_CHALLENGE:=http}"

# Cloudflare DNS-01 challenge plugin registration (if configured).
if [ "${CADDY_CHALLENGE}" = "cloudflare" ]; then
    if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] || [ -z "${CLOUDFLARE_ZONE:-}" ]; then
        echo "ERROR: cloudflare challenge requires CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE"
        exit 1
    fi
fi

# envsubst substitutes ${DOMAIN} from the template.
envsubst '${DOMAIN} ${CADDY_CHALLENGE}' < /etc/caddy/Caddyfile.template > /etc/caddy/Caddyfile

exec caddy run --config /etc/caddy/Caddyfile
