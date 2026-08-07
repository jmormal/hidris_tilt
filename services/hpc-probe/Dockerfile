# hidris-probe — a small SIF that answers one question: can a compute node
# reach the cluster's Redis and Postgres over Tailscale, and by which transport?
#
# Deliberately unrelated to worker-gpu's image. That one is multi-GB (NVHPC +
# ANUGA compiled from source) and takes an hour to build; this is ~250MB and
# builds in under a minute, so the network question can be iterated on without
# paying the compute image's build cost.
#
# Build + ship:  ./build-sif.sh
#
# Everything needed to run tailscaled is inside the image, so the compute node
# needs nothing installed and no admin involvement — tailscaled in
# --tun=userspace-networking mode is an ordinary unprivileged process.
FROM python:3.11-slim-bookworm

# Pin: the tailnet is on a known coordination-server API version, and silently
# floating this is how a probe starts failing for reasons unrelated to the
# thing being probed. Check https://pkgs.tailscale.com/stable/ before bumping.
ARG TS_VERSION=1.86.2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 \
      # libpq for psycopg2 (the client the real worker uses)
      libpq5 \
      # LD_PRELOAD SOCKS interception at the libc layer — the ONLY transport
      # that catches libpq's own C sockets, hence the only one where the
      # existing db.py works unmodified.
      proxychains4 \
      # userspace network stack for the unshare -Ur + netns path, in case the
      # compute node allows user namespaces
      slirp4netns uidmap \
 && rm -rf /var/lib/apt/lists/*

# Static binaries, not the apt package: no systemd in a container, and the
# tarball is self-contained.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) ts_arch=amd64 ;; arm64) ts_arch=arm64 ;; \
      *) echo "unsupported arch $arch" >&2; exit 1 ;; esac; \
    curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${ts_arch}.tgz" \
      -o /tmp/ts.tgz; \
    tar -xzf /tmp/ts.tgz -C /tmp; \
    mv "/tmp/tailscale_${TS_VERSION}_${ts_arch}/tailscale" \
       "/tmp/tailscale_${TS_VERSION}_${ts_arch}/tailscaled" /usr/local/bin/; \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TS_VERSION}_${ts_arch}"; \
    tailscaled --version

# psycopg2-binary matches what worker-gpu/requirements.txt uses, so a psycopg2
# result here transfers directly to the real worker. pg8000 + PySocks are the
# pure-Python fallback pair, proving reachability even when libpq cannot get out.
RUN pip install --no-cache-dir \
      "psycopg2-binary==2.9.10" \
      "pg8000==1.31.2" \
      "PySocks==1.7.1" \
      "redis==5.2.1"

# proxychains4 needs a config naming the SOCKS5 endpoint tailscaled opens.
# proxy_dns is essential: MagicDNS names must be resolved by tailscaled on the
# far side of the proxy, since this node's resolver knows nothing about them.
RUN printf '%s\n' \
      'strict_chain' \
      'proxy_dns' \
      'remote_dns_subnet 224' \
      'tcp_read_time_out 15000' \
      'tcp_connect_time_out 10000' \
      '[ProxyList]' \
      'socks5 127.0.0.1 1055' \
      > /etc/proxychains4.conf

# /opt, not /tmp: Singularity bind-mounts the host /tmp over the container's.
COPY src/probe.py /opt/probe/probe.py
COPY entrypoint.sh /opt/probe/entrypoint.sh
COPY netns-run.sh /opt/probe/netns-run.sh
RUN chmod +x /opt/probe/entrypoint.sh /opt/probe/netns-run.sh

ENTRYPOINT ["/opt/probe/entrypoint.sh"]
