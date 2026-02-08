# =============================================================================
# VPN Sidecar - SOCKS5 proxy via university FortiClient VPN
# =============================================================================
#
# Provides a SOCKS5 proxy (microsocks) over an openfortivpn tunnel to the
# university network, allowing agents to access paywalled research papers.
#
# Build:
#   podman build -t graphrag-vpn ./docker/vpn
#
# Run:
#   podman run --cap-add=NET_ADMIN --device=/dev/ppp \
#     -v ./docker/vpn/config:/etc/openfortivpn/config:ro \
#     -p 1080:1080 graphrag-vpn
#
# Requires:
#   - VPN config at docker/vpn/config (copy config.example and fill in)
#   - NET_ADMIN capability for tunnel interface
#   - /dev/ppp device for pppd
#
# =============================================================================

# ---------------------------------------------------------------------------
# Builder: compile microsocks from source (~100 lines of C, no dependencies)
# ---------------------------------------------------------------------------
FROM docker.io/library/alpine:3.21 AS builder

RUN apk add --no-cache gcc musl-dev make git

RUN git clone --depth 1 https://github.com/rofl0r/microsocks.git /build/microsocks \
    && cd /build/microsocks \
    && make

# ---------------------------------------------------------------------------
# Final: openfortivpn + ppp + microsocks
# ---------------------------------------------------------------------------
FROM docker.io/library/alpine:3.21

LABEL maintainer="Graph-RAG Team"
LABEL description="VPN sidecar providing SOCKS5 proxy via university FortiClient VPN"

# openfortivpn is in Alpine edge/testing
RUN echo "@testing https://dl-cdn.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories \
    && apk add --no-cache \
    openfortivpn@testing \
    ppp \
    socat

COPY --from=builder /build/microsocks/microsocks /usr/local/bin/microsocks
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /usr/local/bin/microsocks

EXPOSE 1080

ENTRYPOINT ["/entrypoint.sh"]
