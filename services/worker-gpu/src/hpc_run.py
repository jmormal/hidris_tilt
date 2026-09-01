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
import sys
import traceback

from redis import Redis

import db
import tasks
from events import channel_for, encode

PUBLIC_ID = os.environ["PUBLIC_ID"]
JOB_ID = os.environ["JOB_ID"]
REDIS_URL = os.getenv("REDIS_URL", "redis://hidris-redis:6379")


def publish(event: str, data: dict) -> None:
    """Best-effort: a failed progress publish must never kill the simulation.

    The result goes to Postgres, not down this channel, so losing an event
    costs a UI update and nothing else.
    """
    try:
        Redis.from_url(REDIS_URL).publish(channel_for(JOB_ID), encode(event, data))
    except Exception as exc:  # noqa: BLE001
        print(f"[hpc_run] progress publish failed: {exc}", file=sys.stderr)


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
    os.makedirs(workdir, exist_ok=True)
    link = os.path.join(workdir, "src")
    if not os.path.lexists(link):
        os.symlink("/app/src", link)
    os.chdir(workdir)
    print(f"[hpc_run] cwd={workdir} (src -> /app/src)")
    return workdir


def main() -> int:
    print(f"[hpc_run] instance={PUBLIC_ID} job={JOB_ID} host={os.uname().nodename}")
    prepare_workdir()
    publish("queued", {"job_id": JOB_ID, "public_id": PUBLIC_ID,
                       "detail": f"Started on {os.uname().nodename}"})

    row = db.get_instance_by_public_id(PUBLIC_ID)
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
        publish("error", {"detail": f"Simulation failed: {exc}"})
        return 1

    # Write to shared scratch BEFORE the DB. A three-hour simulation whose
    # upload fails on a dropped tailnet connection must not lose its result —
    # this file is what makes the run replayable instead of wasted.
    spool = os.path.join(os.getenv("HPC_WORKDIR", "/scratch"),
                         f"solution-{PUBLIC_ID}.bin.gz")
    try:
        with open(spool, "wb") as fh:
            fh.write(gz_bytes)
        print(f"[hpc_run] spooled {len(gz_bytes)} bytes to {spool}")
    except OSError as exc:
        print(f"[hpc_run] WARN could not spool result: {exc}", file=sys.stderr)

    try:
        db.save_solution_bytes(PUBLIC_ID, gz_bytes)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        publish("error", {"detail": f"Failed to store solution: {exc}. "
                                    f"Result is spooled at {spool}"})
        return 1

    print(f"[hpc_run] stored {len(gz_bytes)} bytes for {PUBLIC_ID}")
    publish("complete", {"job_id": JOB_ID, "public_id": PUBLIC_ID,
                         "bytes": len(gz_bytes)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
