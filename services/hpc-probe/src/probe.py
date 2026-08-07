"""
Connectivity probe: can an HPC compute node reach the k3d cluster's Redis and
Postgres over Tailscale, and by which transport?

Writes a JSON report to stdout (the API parses this) and a human-readable
summary to stderr (this lands in the Slurm .out file).

The interesting question is not "does it work" but "which transport works",
because the compute nodes have no CAP_NET_ADMIN, so tailscaled runs with
--tun=userspace-networking and there is no kernel route to 100.x.y.z. Four
transports are checked independently:

  direct       plain sockets. Only works if a real TUN interface exists.
  socks        PySocks monkeypatch -> tailscaled's SOCKS5 on :1055. Works for
               pure-Python clients (redis-py, pg8000) and NOTHING else.
  proxychains  LD_PRELOAD, intercepts at libc. The only transport that catches
               libpq, i.e. the only one where psycopg2 works unmodified.
               Selected by PROBE_MODE=proxychains (the runner re-execs us).
  netns        unshare -Ur + slirp4netns, giving a real TUN inside a user
               namespace. Detected here, exercised by the runner.

Exit status is 0 if at least one transport reached both services, 1 otherwise.
"""

import errno
import fcntl
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import traceback
from urllib.parse import urlparse

# Report shape: a flat list of checks, each {name, ok, detail, ...}.
REPORT: dict = {
    "schema": 1,
    "mode": os.getenv("PROBE_MODE", "direct"),
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "host": socket.gethostname(),
    "slurm_job_id": os.getenv("SLURM_JOB_ID"),
    "checks": [],
}

# Targets. Defaults are the MagicDNS names the tailscale operator assigns from
# the annotations in k8s/redis.yaml and k8s/postgres.yaml — NOT the in-cluster
# service names, which do not exist outside the cluster.
REDIS_HOST = os.getenv("PROBE_REDIS_HOST", "hidris-redis")
REDIS_PORT = int(os.getenv("PROBE_REDIS_PORT", "6379"))
DB_HOST = os.getenv("PROBE_DB_HOST", "hidris-db")
DB_PORT = int(os.getenv("PROBE_DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "hidris")
DB_USER = os.getenv("PG_USER", "hidris")
DB_PASSWORD = os.getenv("PG_PASSWORD", "")

SOCKS_HOST = os.getenv("TS_SOCKS_HOST", "127.0.0.1")
SOCKS_PORT = int(os.getenv("TS_SOCKS_PORT", "1055"))

# Names that only resolve inside Kubernetes. If the environment hands us one of
# these we flag it: it is the single most likely reason a job fails out here.
IN_CLUSTER_NAMES = {
    "redis", "hidris-db-rw", "hidris-db-ro", "hidris-db-r",
    "minio", "mlflow", "keycloak",
}


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    """Append one check to the report and echo it to stderr."""
    entry = {"name": name, "ok": ok, "detail": detail, **extra}
    REPORT["checks"].append(entry)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", file=sys.stderr)
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===", file=sys.stderr)


def guarded(name: str, fn, **extra):
    """Run fn(), turning any exception into a failed check rather than a crash.

    A probe that dies on its third check tells you much less than one that
    completes and reports eight results, so nothing here is allowed to escape.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        record(name, False, f"{type(exc).__name__}: {exc}",
               traceback=traceback.format_exc(limit=3), **extra)
        return None


# ---------------------------------------------------------------------------
# 1. Kernel capabilities — decides which transports are even possible
# ---------------------------------------------------------------------------

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


def check_capabilities() -> None:
    section("Kernel capabilities")

    caps = ""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    caps = line.split()[1]
                    break
    except OSError:
        pass
    # All-zero CapEff is the normal unprivileged case and means TUNSETIFF below
    # will return EPERM. Non-zero is worth knowing about.
    record("caps.effective", True, f"CapEff={caps or 'unknown'}",
           privileged=bool(caps and caps.strip("0")))

    # Opening /dev/net/tun proves nothing — it is usually mode 666. TUNSETIFF is
    # the operation that actually needs CAP_NET_ADMIN.
    def tun():
        fd = os.open("/dev/net/tun", os.O_RDWR)
        try:
            fcntl.ioctl(fd, TUNSETIFF,
                        struct.pack("16sH", b"tsprobe%d", IFF_TUN | IFF_NO_PI))
            return record("caps.tun_create", True,
                          "TUNSETIFF succeeded — real interface mode is possible",
                          enables="direct")
        except OSError as exc:
            return record("caps.tun_create", False,
                          f"TUNSETIFF failed ({errno.errorcode.get(exc.errno, exc.errno)})"
                          " — userspace-networking required",
                          enables="direct")
        finally:
            os.close(fd)

    guarded("caps.tun_create", tun)

    # A user namespace grants CAP_NET_ADMIN over its own netns, which is the
    # slirp4netns path to a real TUN without any host privilege.
    def userns():
        proc = subprocess.run(["unshare", "-Ur", "-n", "true"],
                              capture_output=True, timeout=15)
        ok = proc.returncode == 0
        return record("caps.userns", ok,
                      "unshare -Urn OK" if ok else
                      f"unshare -Urn denied: {proc.stderr.decode().strip()}",
                      enables="netns")

    if guarded("caps.userns", userns):
        # Creating the namespace is not the interesting part. TUNSETIFF *inside*
        # it is: that is what lets tailscaled bring up a real interface, which
        # in turn is what makes libpq (psycopg2) work with no proxy at all.
        def userns_tun():
            snippet = (
                "import fcntl,struct,os\n"
                "fd=os.open('/dev/net/tun',os.O_RDWR)\n"
                "fcntl.ioctl(fd,0x400454ca,"
                "struct.pack('16sH',b'tsprobe%d',0x0001|0x1000))\n"
            )
            proc = subprocess.run(
                ["unshare", "-Ur", "-n", sys.executable, "-c", snippet],
                capture_output=True, timeout=30)
            ok = proc.returncode == 0
            return record("caps.userns_tun", ok,
                          "TUNSETIFF succeeds inside the namespace — a real TUN "
                          "is possible, so psycopg2 needs no proxy"
                          if ok else
                          f"TUNSETIFF still fails inside the namespace: "
                          f"{proc.stderr.decode().strip()[:200]}",
                          enables="netns")

        guarded("caps.userns_tun", userns_tun)

    slirp = shutil.which("slirp4netns")
    record("caps.slirp4netns", slirp is not None,
           f"found at {slirp}" if slirp else "not on PATH", enables="netns")


# ---------------------------------------------------------------------------
# 2. Tailscale daemon state
# ---------------------------------------------------------------------------


def check_tailscale() -> dict | None:
    section("Tailscale")

    sock = os.getenv("TS_SOCKET", "")
    cmd = ["tailscale"] + (["--socket", sock] if sock else []) + ["status", "--json"]

    def run_status():
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            record("tailscale.status", False,
                   f"exit {proc.returncode}: {proc.stderr.decode().strip()[:300]}")
            return None
        return json.loads(proc.stdout)

    status = guarded("tailscale.status", run_status)
    if not status:
        return None

    state = status.get("BackendState", "unknown")
    record("tailscale.backend_state", state == "Running",
           f"BackendState={state}")

    self_node = status.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    record("tailscale.self_ip", bool(ips),
           f"{self_node.get('HostName', '?')} = {', '.join(ips) or 'no IP assigned'}",
           tailscale_ips=ips)

    health = status.get("Health") or []
    record("tailscale.health", not health,
           "no warnings" if not health else "; ".join(str(h) for h in health[:4]))

    # Are the two services we care about actually visible as peers? A peer that
    # is missing here means the operator never created its proxy, or an ACL is
    # hiding it — a different problem from "the connection timed out".
    peers = status.get("Peer") or {}
    peer_names = sorted(
        (p.get("DNSName") or p.get("HostName") or "").rstrip(".")
        for p in peers.values()
    )
    record("tailscale.peers", bool(peer_names),
           f"{len(peer_names)} peer(s): {', '.join(n for n in peer_names if n)[:400]}",
           peers=peer_names)

    for want in (REDIS_HOST, DB_HOST):
        short = want.split(".")[0]
        seen = any(short == n.split(".")[0] for n in peer_names if n)
        record(f"tailscale.peer.{short}", seen,
               "visible in tailnet" if seen else
               f"NOT a visible peer — check the tailscale.com/expose annotation "
               f"and that the operator proxy pod is running")

    magic = status.get("MagicDNSSuffix", "")
    record("tailscale.magicdns_suffix", bool(magic), magic or "not reported",
           suffix=magic)
    return status


# ---------------------------------------------------------------------------
# 3. Transports
# ---------------------------------------------------------------------------


def check_internet() -> None:
    """Can this node reach Tailscale's coordination and relay infrastructure?

    The prerequisite for everything else, and the one check that needs no
    credential. A compute node with no route off-site cannot join the tailnet
    no matter how the auth is configured, and that failure otherwise shows up
    much later as a confusing `tailscale up` timeout.

    443/TCP to controlplane is required. DERP (also 443) is the relay path used
    when no direct UDP path can be established, which on a firewalled compute
    node is the likely case.
    """
    section("Outbound reachability (no credential needed)")

    for label, host, port in (
        ("controlplane", "controlplane.tailscale.com", 443),
        ("derp", "derp1.tailscale.com", 443),
    ):
        def go(h=host, p=port, l=label):
            addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(h, p)})
            t0 = time.monotonic()
            with socket.create_connection((h, p), timeout=10):
                ms = (time.monotonic() - t0) * 1000
            return record(f"internet.{l}", True,
                          f"{h}:{p} reachable in {ms:.0f}ms via {addrs[0]}",
                          addresses=addrs, connect_ms=round(ms))

        guarded(f"internet.{label}", go)


def check_socks_listener() -> bool:
    section("SOCKS5 listener")

    def probe():
        with socket.create_connection((SOCKS_HOST, SOCKS_PORT), timeout=5):
            return record("socks.listener", True,
                          f"tailscaled SOCKS5 accepting on {SOCKS_HOST}:{SOCKS_PORT}")

    return bool(guarded("socks.listener", probe))


def check_dns() -> None:
    """Direct name resolution.

    Expected to FAIL under userspace-networking: tailscaled cannot rewrite
    /etc/resolv.conf without root, so MagicDNS names have no system resolver.
    This is not fatal — SOCKS5 passes the hostname to tailscaled, which resolves
    it internally, so the socks transport works even when this fails.
    """
    section("DNS (direct resolution)")
    for host in (REDIS_HOST, DB_HOST):
        def resolve(h=host):
            addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(h, None)})
            return record(f"dns.{h}", True, f"resolves to {', '.join(addrs)}",
                          addresses=addrs)
        guarded(f"dns.{host}", resolve)


def _install_socks_monkeypatch() -> None:
    """Point every subsequent pure-Python socket at tailscaled's SOCKS5.

    Only affects sockets created in Python. libpq (psycopg2) opens its own in C
    and is completely unaffected — that is the whole reason proxychains exists
    as a separate mode below.
    """
    import socks  # PySocks

    socks.set_default_proxy(socks.SOCKS5, SOCKS_HOST, SOCKS_PORT, rdns=True)
    socket.socket = socks.socksocket

    # Patching socket.socket alone is not enough. redis-py and pg8000 both call
    # socket.getaddrinfo() *first* and connect to the resulting address, so a
    # MagicDNS name fails at resolution before the proxy is ever reached — the
    # node has no resolver that knows the tailnet. Hand the name back unresolved
    # so socksocket forwards it to tailscaled, which does the lookup itself
    # (rdns=True above). This is the SOCKS equivalent of proxychains' proxy_dns.
    def _passthrough_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    socket.getaddrinfo = _passthrough_getaddrinfo


def check_redis(transport: str) -> bool:
    section(f"Redis via {transport}")
    import redis

    def go():
        client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                             socket_connect_timeout=10, socket_timeout=10)
        pong = client.ping()
        info = client.info("server")
        # Round-trip a real key: PING alone can succeed against something that
        # is not actually usable for the queue.
        key = f"hidris:probe:{os.getenv('SLURM_JOB_ID', 'local')}"
        client.setex(key, 60, "ok")
        value = client.get(key)
        client.delete(key)
        depths = {q: client.llen(q) for q in
                  ("jobs:gpu", "jobs:cpu", "jobs:cluster:gpu", "jobs:cluster:cpu")}
        return record(f"redis.{transport}", bool(pong) and value == b"ok",
                      f"PING+SETEX/GET ok, server {info.get('redis_version')}, "
                      f"queue depths {depths}",
                      redis_version=info.get("redis_version"), queues=depths)

    return bool(guarded(f"redis.{transport}", go))


def check_postgres_psycopg2(transport: str) -> bool:
    """psycopg2 — what the real worker uses. Only succeeds on direct/proxychains."""
    section(f"Postgres via {transport} (psycopg2 / libpq)")
    import psycopg2

    def go():
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            connect_timeout=15,
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version(), current_database(), current_user;")
                version, database, who = cur.fetchone()
                # Confirm the schema the worker writes to is actually present —
                # reaching a Postgres that is not ours is a real failure mode.
                cur.execute("""
                    SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = current_schema()
                      AND table_name IN ('simulations', 'storms');
                """)
                tables = cur.fetchone()[0]
            conn.rollback()
        finally:
            conn.close()
        return record(f"postgres.psycopg2.{transport}", True,
                      f"connected as {who} to {database}; {tables}/2 expected "
                      f"tables present; {version.split(',')[0]}",
                      server_version=version.split(",")[0], tables_found=tables)

    return bool(guarded(f"postgres.psycopg2.{transport}", go))


def check_postgres_pg8000(transport: str) -> bool:
    """pg8000 — pure Python, so it works over the SOCKS monkeypatch.

    This is the fallback if proxychains is unavailable: it proves the database
    is reachable even when psycopg2 cannot get there. Note it cannot replace
    psycopg2 in db.py without also replacing conn.lobject (large objects are
    unsupported; SELECT lo_get(%s) is the equivalent).
    """
    section(f"Postgres via {transport} (pg8000 / pure Python)")
    import pg8000.dbapi

    def go():
        conn = pg8000.dbapi.connect(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASSWORD, timeout=15,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT version(), current_database(), current_user;")
            version, database, who = cur.fetchone()
            cur.close()
            conn.rollback()
        finally:
            conn.close()
        return record(f"postgres.pg8000.{transport}", True,
                      f"connected as {who} to {database}; {version.split(',')[0]}",
                      server_version=version.split(",")[0])

    return bool(guarded(f"postgres.pg8000.{transport}", go))


# ---------------------------------------------------------------------------
# 4. Config sanity — catches the mistake that actually happens
# ---------------------------------------------------------------------------


def check_config() -> None:
    section("Config sanity")

    # anuga.env currently ships DB_HOST=hidris-db-rw and REDIS_URL=redis://redis
    # — both in-cluster names. They work in k8s and cannot work here.
    suspect = []
    for var in ("DB_HOST", "PROBE_DB_HOST", "PROBE_REDIS_HOST"):
        val = os.getenv(var, "")
        if val.split(".")[0] in IN_CLUSTER_NAMES:
            suspect.append(f"{var}={val}")
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        parsed = urlparse(redis_url)
        if (parsed.hostname or "").split(".")[0] in IN_CLUSTER_NAMES:
            suspect.append(f"REDIS_URL={redis_url}")

    record("config.no_in_cluster_names", not suspect,
           "no in-cluster DNS names in the environment" if not suspect else
           f"in-cluster names that cannot resolve from HPC: {', '.join(suspect)}"
           " — use the tailnet names (hidris-db, hidris-redis) instead",
           offenders=suspect)

    record("config.db_password_set", bool(DB_PASSWORD),
           "PG_PASSWORD is set" if DB_PASSWORD else
           "PG_PASSWORD is empty — auth will fail even if the socket connects")


# ---------------------------------------------------------------------------


def main() -> int:
    mode = REPORT["mode"]
    print(f"hidris HPC connectivity probe — mode={mode} host={REPORT['host']}",
          file=sys.stderr)

    check_config()

    # Capabilities and daemon state are properties of the node, not of the
    # transport, so only gather them once (on the plain pass).
    if mode == "direct":
        check_capabilities()
        check_internet()
        check_tailscale()
        check_socks_listener()
        check_dns()

    if mode == "socks":
        _install_socks_monkeypatch()

    redis_ok = check_redis(mode)
    # psycopg2 over the SOCKS monkeypatch is expected to fail; run it anyway so
    # the report contains the evidence rather than an assumption.
    pg_ok = check_postgres_psycopg2(mode)
    pg8000_ok = check_postgres_pg8000(mode)

    REPORT["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    REPORT["summary"] = {
        "redis": redis_ok,
        "postgres_psycopg2": pg_ok,
        "postgres_pg8000": pg8000_ok,
        "usable": bool(redis_ok and (pg_ok or pg8000_ok)),
        "worker_ready": bool(redis_ok and pg_ok),  # unmodified db.py works
        "failed_checks": [c["name"] for c in REPORT["checks"] if not c["ok"]],
    }

    section("Summary")
    s = REPORT["summary"]
    print(f"  transport={mode} redis={s['redis']} "
          f"psycopg2={s['postgres_psycopg2']} pg8000={s['postgres_pg8000']}",
          file=sys.stderr)
    print(f"  worker_ready (unmodified db.py works over this transport): "
          f"{s['worker_ready']}", file=sys.stderr)

    # stdout is JSON only — the runner tees it to a file the API reads back.
    json.dump(REPORT, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if s["usable"] else 1


if __name__ == "__main__":
    sys.exit(main())
