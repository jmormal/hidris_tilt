# hpc-probe

A ~220MB SIF that answers one question: **can a compute node reach this
cluster's Redis and Postgres over Tailscale, and by which transport?**

Kept separate from `worker-gpu` on purpose. That image is multi-GB and takes
an hour to build, so iterating on the network question there is painful. This
one builds in under a minute.

## Why "which transport" is the question

Compute nodes have no `CAP_NET_ADMIN`, so `tailscaled` cannot create a TUN
device and must run with `--tun=userspace-networking`. That gives a real
WireGuard connection, but it lives entirely inside the tailscaled process —
the kernel has no route to `100.x.y.z`, so nothing else on the node can use it
except through the SOCKS5 proxy tailscaled opens on `:1055`.

That matters because a SOCKS proxy is opt-in. The probe tests four transports
and reports which ones reached both services:

| transport | how | reaches libpq? |
|---|---|---|
| `direct` | plain sockets | only with a real TUN |
| `socks` | PySocks monkeypatch → `:1055` | **no** — Python sockets only |
| `proxychains` | `LD_PRELOAD`, intercepts at libc | **yes** |
| `netns` | `unshare -Ur -n` + slirp4netns, real TUN | **yes**, with no proxy at all |

**`netns` is the one to want, and it has been confirmed available on this
cluster.** Measured on `vrhpcadm1` inside the SIF: `TUNSETIFF` fails with
`EPERM` normally (`CapEff=0000000000000000`), but inside `unshare -Ur -n` it
succeeds with `CapEff=0000003fffffffff`. tailscaled then starts with a genuine
`--tun=tailscale0`, routes out through slirp4netns, and reaches
`controlplane.tailscale.com`. Kernel routing to `100.x.y.z` is real in that
namespace, so `psycopg2` needs no proxy and `db.py` needs no changes.

Capabilities are namespaced: inside a user namespace you are UID 0 with a full
capability set over *your own* network namespace only. That is not privilege
escalation — the only thing you can break is your own view of the network. A
fresh netns has just loopback, so slirp4netns (running outside, as you)
translates its packets into ordinary `socket()` calls you were already allowed
to make.

`psycopg2` wraps libpq, which opens its own sockets in C. Python-level
monkeypatching never sees them and libpq has no SOCKS support, so the `socks`
row cannot carry the existing `db.py`. `proxychains` intercepts at the libc
layer and does. The report's `worker_ready_via` field is exactly this: which
transports the unmodified worker `db.py` could run over.

`pg8000` (pure Python) is also tested as the fallback — it proves the database
is reachable even when psycopg2 cannot get there. It is not a drop-in
replacement: it has no `conn.lobject`, so the large-object reads in `db.py`
would need rewriting as `SELECT lo_get(%s)`.

`proxychains` remains the fallback if `netns` turns out to be unavailable on
compute nodes specifically (the capability test above was run on the login
node; the Slurm job re-runs it where it counts).

## Targets

Tailnet names, not in-cluster ones. The operator assigns these from the
annotations already in the manifests:

- `k8s/redis.yaml:30` → `hidris-redis`
- `k8s/postgres.yaml:24` → `hidris-db`

`services/worker-gpu/anuga.env` currently ships `DB_HOST=hidris-db-rw` and
`REDIS_URL=redis://redis:6379`, which resolve **only inside Kubernetes** and
cannot work from HPC. The probe flags any such name it finds in the
environment (`config.no_in_cluster_names`) — this is the most likely reason a
cluster job fails, and it fails in a way that looks like a network problem.

## Use

```bash
./build-sif.sh                 # build, smoke-test, rsync to the cluster
SKIP_PUSH=1 ./build-sif.sh     # build and test only
```

First run only, on the cluster — the env file is deliberately *not* rsynced,
so a rebuild can never overwrite working credentials with a stale local copy:

```bash
ssh jmormal@upvnet.upv.es@vrhpcadm1
mkdir -p ~/containers && chmod 700 ~/containers
vi ~/containers/probe.env       # fill in from probe.env.example
chmod 600 ~/containers/probe.env
```

Then either `sbatch ~/containers/run-probe.slurm`, or drive it from the API:

```bash
curl -sk -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.127.0.0.1.nip.io/api/hpc/probe
# -> {"job_id": "12345", "state": "PENDING"}

curl -sk -H "Authorization: Bearer $TOKEN" \
  https://api.127.0.0.1.nip.io/api/hpc/probe/12345
```

## Reading the result

```json
"verdict": {
  "reachable_via":    ["socks", "proxychains"],
  "worker_ready_via": ["proxychains"]
}
```

- `reachable_via` empty → nothing works. Check `tailscale.peer.*` first: a
  missing peer means the operator proxy never came up, which is a different
  problem from a timeout.
- `worker_ready_via` non-empty → run the real worker under that transport and
  `db.py` needs no changes.
- `reachable_via` non-empty but `worker_ready_via` empty → the database is
  reachable but only by pure-Python clients; either get proxychains working or
  port the large-object reads to `pg8000`.

## What this does not test

Four things survive whichever transport wins, and three were never about
Tailscale:

1. **Connection ceiling.** 200 array tasks × a pool `minconn=1` is 200
   connections against a default `max_connections` of 100. CNPG does not raise
   that by default.
2. **Import-time pool.** `worker-gpu/src/db.py:12` opens a connection at import,
   before the job has done any work — and before tailscaled is necessarily up.
3. **Idle connections across a multi-hour job.** Connect after the compute
   finishes, and set keepalives regardless.
4. **The non-atomic Redis + Postgres write.**

`proxychains` also has a real cost: it is a userspace TCP/IP path, so
throughput caps in the hundreds of Mbit. Fine for a gzipped result, not for
bulk data.
