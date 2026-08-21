# syntax=docker/dockerfile:1.7
#
# Caddy reverse proxy image for Sophia. The entrypoint renders the
# Caddyfile template from environment variables at container startup,
# then runs `caddy run` in the foreground.
#
# Build:
#   docker build -f Caddy.dockerfile -t sophia-caddy:local .

FROM caddy:2-builder AS builder
RUN caddy add-package github.com/caddy-dns/cloudflare

FROM caddy:2

COPY --from=builder /usr/bin/caddy /usr/bin/caddy

COPY Caddyfile /etc/caddy/Caddyfile.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
