"""
Standalone HPC simulation runner. Slurm is the scheduler, so unlike worker.py
there is no RQ worker loop, no queue and no job object — this process solves
exactly one instance and exits.

What it keeps identical to the RQ path is the *observable* contract:
progress is published to `sim:events:{job_id}` with the same event shapes from
events.py, so the API's existing SSE relay and the frontend's progress UI work
unchanged whether a simulation ran in-cluster or on the HPC.

Reads the instance from Postgres rather than taking it on the command line: the
payload can be megabytes of polygons, and the DB is reachable anyway (that is
what the tailnet is for). Solution goes back the same way.

Env: PUBLIC_ID, JOB_ID, plus the DB_*/REDIS_URL that db.py and tasks.py read.
"""

import argparse
import os
import json
import sys
import traceback

from redis import Redis

import db
import tasks
from events import channel_for, encode

PUBLIC_ID = os.environ["PUBLIC_ID"]
JOB_ID = os.environ["JOB_ID"]
REDIS_URL = os.getenv("REDIS_URL", "redis://hidris-redis:6379")

# Rank identity, resolved once. anuga defines these even without MPI (0 and 1),
# so this file behaves identically whether it was started by mpirun or not.
try:
    from anuga import myid as _MYID, numprocs as _NUMPROCS
except Exception:  # noqa: BLE001
    _MYID, _NUMPROCS = 0, 1
_IS_ROOT = _MYID == 0


def publish(event: str, data: dict) -> None:
    """Best-effort: a failed progress publish must never kill the simulation.

    The result goes to Postgres, not down this channel, so losing an event
    costs a UI update and nothing else.
    """
    if not _IS_ROOT:
        return
    try:
        if _DB_PROXY_CONF:
            import base64

            _proxied("publish", REDIS_URL, channel_for(JOB_ID),
                     base64.b64encode(encode(event, data)).decode())
        else:
            Redis.from_url(REDIS_URL).publish(
                channel_for(JOB_ID), encode(event, data)
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[hpc_run] progress publish failed: {exc}", file=sys.stderr)


# Set by mn-entrypoint.sh on rank 0 under multi-node: the path to a proxychains
# config that reaches the tailnet. Its presence means "this rank has DB access,
# but only through a subprocess" — the solver itself must never be proxied,
# because LD_PRELOAD breaks MPI (MPI_ERR_INTERN inside bcast).
_DB_PROXY_CONF = os.getenv("HPC_DB_PROXY_CONF", "")


def _proxied(*args: str) -> dict:
    """Run one db_proxy.py command under proxychains and return its JSON."""
    import subprocess

    cmd = ["python", "/app/src/db_proxy.py", *args]
    if _DB_PROXY_CONF:
        cmd = ["proxychains4", "-f", _DB_PROXY_CONF, "-q"] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(
            f"db_proxy {args[0]} failed (rc={out.returncode}): "
            f"{(out.stderr or out.stdout)[-400:]}"
        )
    res = json.loads(out.stdout.strip().splitlines()[-1])
    if "error" in res:
        raise RuntimeError(f"db_proxy {args[0]}: {res['error']}")
    return res


def _abort_peers() -> None:
    """Tear the whole MPI job down after a failure on this rank.

    Returning normally is not enough. mpi4py calls MPI_Finalize at interpreter
    exit and that is COLLECTIVE, so a failed rank blocks there while its peers
    sit in whatever collective they had reached — typically a bcast or Gatherv
    that will never be satisfied. No process exits, mpirun sees nothing wrong,
    and Slurm runs the job to its wall clock: one NameError cost a 4-hour
    allocation and held its GPUs the whole time.

    Abort() bypasses the collective and kills every rank, so the job ends in
    seconds. The `error` event has already been published by the caller, so the
    frontend still learns what happened.
    """
    if _NUMPROCS <= 1:
        return
    try:
        from mpi4py import MPI

        print(f"[hpc_run] rank {_MYID}: aborting all {_NUMPROCS} ranks",
              file=sys.stderr, flush=True)
        MPI.COMM_WORLD.Abort(1)
    except Exception:  # noqa: BLE001
        os._exit(1)  # last resort: never leave peers hanging in a collective


def prepare_workdir() -> str:
    """Make a writable CWD that still looks like /app to tasks.py.

    tasks.py resolves the DEM as a relative path ("./src/MDT_malla_5m...ers",
    tasks.py:597) and writes the clipped result to a relative "region.asc". So
    the working directory must BOTH contain a `src/` and be writable. /app is
    only the former: under Singularity the image root is read-only squashfs, so
    running there fails at the region.asc write.

    Running from bind-mounted scratch with src symlinked in satisfies both, and
    keeps every intermediate on the node's scratch rather than in the image.
    """
    workdir = os.getenv("HPC_WORKDIR", "/scratch")
    if _NUMPROCS > 1:
        # A directory per rank. Two things go wrong when ranks share one:
        # they race to create the src symlink (FileExistsError, because
        # lexists-then-symlink is not atomic), and every rank writes the DEM
        # clip to the same relative "region.asc" at the same time, which is a
        # silently corrupted raster rather than an error.
        workdir = os.path.join(workdir, f"rank{_MYID}")
    os.makedirs(workdir, exist_ok=True)
    link = os.path.join(workdir, "src")
    try:
        os.symlink("/app/src", link)
    except FileExistsError:
        pass
    os.chdir(workdir)
    print(f"[hpc_run] rank {_MYID}: cwd={workdir} (src -> /app/src)")
    return workdir


def main() -> int:
    print(
        f"[hpc_run] instance={PUBLIC_ID} job={JOB_ID} host={os.uname().nodename} "
        f"rank={_MYID}/{_NUMPROCS}"
    )
    prepare_workdir()
    publish("queued", {"job_id": JOB_ID, "public_id": PUBLIC_ID,
                       "detail": f"Started on {os.uname().nodename}"})

    # Rank 0 reads it and hands it to the others. Under multi-node only rank 0
    # has a route to Postgres — the rest deliberately have no tailnet at all —
    # so every rank calling this would fail on all but one. The payload is a
    # setup document (polygons and config), small enough for a pickled bcast;
    # the storm cube, which is not, has its own broadcast in tasks.py.
    if _IS_ROOT:
        row = (_proxied("get-instance", PUBLIC_ID)["row"] if _DB_PROXY_CONF
               else db.get_instance_by_public_id(PUBLIC_ID))
    else:
        row = None
    if _NUMPROCS > 1:
        from mpi4py import MPI

        row = MPI.COMM_WORLD.bcast(row, root=0)

    if row is None:
        msg = f"No instance {PUBLIC_ID} in the database"
        print(f"[hpc_run] {msg}", file=sys.stderr)
        publish("error", {"detail": msg})
        return 2
    payload = row.get("instance")
    if not payload:
        msg = f"Instance {PUBLIC_ID} has no setup payload to solve"
        print(f"[hpc_run] {msg}", file=sys.stderr)
        publish("error", {"detail": msg})
        return 2
    print(f"[hpc_run] loaded setup for {row.get('instance_name') or PUBLIC_ID}")

    # Only used when the setup has no "region" feature: with one, tasks.py
    # overwrites this with the DEM it clips to that region (tasks.py:595).
    elevation = os.getenv("ANUGA_ELEVATION", "/app/src/MDT_malla_5m_etrs89h30.ers")
    if not os.path.isabs(elevation):
        elevation = os.path.abspath(elevation)

    args = argparse.Namespace(
        payload=None,
        result=None,
        job_id=JOB_ID,
        output_name=f"simulation_{JOB_ID}",
        yieldstep=float(os.getenv("ANUGA_YIELDSTEP", "20")),
        src_epsg=int(os.getenv("ANUGA_SRC_EPSG", "4326")),
        dst_epsg=int(os.getenv("ANUGA_DST_EPSG", "25830")),
        elevation_file=elevation,
        gpu_mode=int(os.getenv("ANUGA_GPU_MODE", "2")),
    )

    try:
        # Same core the RQ path calls, so the two cannot diverge numerically.
        # _run_gpu_worker publishes its own progress via tasks._publish_progress,
        # which targets the same channel because it is keyed on args.job_id.
        gz_bytes = tasks._run_gpu_worker(args, payload=payload)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        if _IS_ROOT:
            publish("error", {"detail": f"Simulation failed: {exc}"})
        _abort_peers()
        return 1

    # Under mpirun every rank runs this file. Only rank 0 comes back with the
    # result; the rest have done their share of the solve and must not write it
    # — N ranks storing one solution would race on the same row, and N ranks
    # publishing would give the frontend N copies of every event.
    if gz_bytes is None:
        print(f"[hpc_run] rank {_MYID}: solve done, rank 0 stores the result")
        return 0

    # Write to shared scratch BEFORE the DB. A three-hour simulation whose
    # upload fails on a dropped tailnet connection must not lose its result —
    # this file is what makes the run replayable instead of wasted.
    # Deliberately the shared workdir, not the per-rank cwd: this is the
    # replayable artifact run-simulation.slurm lists after the job.
    spool = os.path.join(os.getenv("HPC_WORKDIR", "/scratch"),
                         f"solution-{PUBLIC_ID}.bin.gz")
    try:
        with open(spool, "wb") as fh:
            fh.write(gz_bytes)
        print(f"[hpc_run] spooled {len(gz_bytes)} bytes to {spool}")
    except OSError as exc:
        print(f"[hpc_run] WARN could not spool result: {exc}", file=sys.stderr)

    try:
        if _DB_PROXY_CONF:
            # Already spooled above; hand the proxy the file rather than piping
            # hundreds of MB through an argv-sized channel.
            _proxied("save-solution", PUBLIC_ID, spool)
        else:
            db.save_solution_bytes(PUBLIC_ID, gz_bytes)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        publish("error", {"detail": f"Failed to store solution: {exc}. "
                                    f"Result is spooled at {spool}"})
        _abort_peers()
        return 1

    print(f"[hpc_run] stored {len(gz_bytes)} bytes for {PUBLIC_ID}")
    publish("complete", {"job_id": JOB_ID, "public_id": PUBLIC_ID,
                         "bytes": len(gz_bytes)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
