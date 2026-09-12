"""
tasks.py — RQ task + GPU-accelerated ANUGA simulation in ONE file.

Job envelope (new):
  The RQ job arrives as {"public_id": "<uuid>", "payload": {config, features}}.
  run_anuga unwraps it; a bare {config, features} payload still works as a
  fallback. On completion the gzipped binary result container is written to
  Postgres (keyed by public_id, is_solved -> TRUE) and also returned as bytes.

  PERFORMANCE
  - Per-yieldstep saving is an O(1)-Python snapshot of 3 float32 arrays
    (stage, xmom, ymom). Depth/speed/velocities are derived ONCE at the end,
    vectorized.
  - Elevation/friction are static: captured once, not per timestep.
  - domain.set_store(False): skips the per-yieldstep .sww write + D2H sync.
  - Inlet discharge uses Rate_operator.inflow (GPU-resident).

Env vars:
  ANUGA_GPU_MODE        multiprocessor mode int  (default: 2)
  ANUGA_ELEVATION       path to the DEM          (default: ./src/mi_terreno_whole.tif)
  TETIS_REDIS_URL       redis url for progress   (default: redis://redis:6379)
  ANUGA_GPU_SUBPROCESS  "1" -> run simulation out-of-process (recommended in
                        production: a native crash can't kill the RQ worker,
                        and GPU state is fully released between jobs).
  DB_HOST / DB_NAME / PG_USER / PG_PASSWORD / DB_PORT
                        Postgres connection (db.py reads these); the worker
                        needs them to persist the solution.
"""

import pyproj
from shapely.ops import transform
from shapely.geometry import polygon, shape
from rasterio.mask import mask
import rasterio
import argparse
import gzip
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon

from anuga.geometry.polygon_function import Polygon_function

# ===========================================================================
# Result wire format
# ===========================================================================
# The result is a binary container, not JSON — see _encode_result_binary for
# why. There is no decimal-rounding constant any more: values ship as float32,
# which is finer than any depth a solver meaningfully resolves and removes the
# old trap where coarse rounding could zero sub-centimetre depths before the
# wet/dry filter ever saw them.


def clip_dem_to_asc(
    coordinates,
    ers_path="./src/MDT_malla_5m_etrs89h30.ers",
    src_epsg=4326,
    out_path="mi_terreno.asc",
):
    # `coordinates` is the GeoJSON Polygon ring: [[[lon,lat], ...]]
    poly = Polygon(coordinates[0])

    with rasterio.open(ers_path) as src:
        # reproject the polygon into the DEM's own CRS (don't hardcode)
        project = pyproj.Transformer.from_crs(
            f"EPSG:{src_epsg}", src.crs, always_xy=True
        ).transform
        poly_proj = transform(project, poly)

        nodata = src.nodata if src.nodata is not None else -9999
        img, tr = mask(src, [poly_proj.__geo_interface__], crop=True, nodata=nodata)

        meta = src.meta.copy()
        meta.update(
            driver="AAIGrid",  # <- the -of AAIGrid part
            height=img.shape[1],
            width=img.shape[2],
            count=1,  # AAIGrid is single-band only
            transform=tr,
            nodata=nodata,
            crs=src.crs,
            dtype=img.dtype,
        )

        with rasterio.open(out_path, "w", **meta) as dst:
            dst.write(img[0], 1)  # write first band only

    return out_path


# The old Vertex / Triangle / SimulationResult pydantic models are gone: the
# result is emitted as typed binary blocks (see _encode_result_binary), so
# nothing is ever validated or serialised per triangle. Blocks written:
#
#   vertexLonLat  f8[nVertices * 2]        lon, lat interleaved
#   triIndices    i4[nTriangles * 3]       vertex ids into vertexLonLat
#   elevation     f4[nTriangles]           ground height (m) under each triangle
#   depth         f4[nTriangles * nFrames] indexed [tri * nFrames + frame]
#   speed         f4[nTriangles * nFrames] same layout
#
# `elevation` is the DEM the solve actually ran on, which is what lets the 3D
# view sit the water surface on the same ground ANUGA used
# (water z = elevation + depth[frame]).


# ===========================================================================
# PART 1 — RQ launcher (runs in the single RQ worker process)
# ===========================================================================


def run_anuga(
    job_payload,
    src_epsg=4326,
    dst_epsg=25830,
    output_name="simulation",
    yieldstep=20,
):
    """RQ entrypoint. Runs the GPU simulation, either in-process (default) or
    out-of-process via `tasks.py --gpu-worker` when ANUGA_GPU_SUBPROCESS=1.

    The job is enqueued as {"public_id": ..., "payload": {config, features}}.
    The gzipped binary result container is persisted to Postgres on completion;
    the return value is only a small summary, since RQ pickles it into Redis
    for RESULT_TTL and a real result runs to hundreds of MB. Clients read the
    solution back through GET /api/instances/{id}/result.
    """
    from rq import get_current_job
    from events import channel_for, encode
    import db  # flat import to match the worker's import root (see `events`)

    print("WORKER: Received job. Starting GPU simulation...")
    job = get_current_job()
    job_id = job.id if job is not None else "local"

    # ---- Unwrap the enqueue envelope -------------------------------------
    # New shape: {"public_id": "<uuid>", "payload": {config, features}}
    # Fallback: a bare {config, features} payload (no envelope).
    if isinstance(job_payload, dict) and "payload" in job_payload:
        public_id = job_payload.get("public_id")
        payload = job_payload["payload"]
    else:
        public_id = None
        payload = job_payload

    if public_id is None:
        print(
            "WORKER: WARNING — no public_id in job; solution will NOT be "
            "persisted to the DB (returning bytes only)."
        )

    use_subprocess = os.getenv("ANUGA_GPU_SUBPROCESS", "0") == "1"
    elevation_file = os.getenv("ANUGA_ELEVATION", "./src/mi_terreno_whole.tif")
    if not os.path.isabs(elevation_file):
        elevation_file = os.path.abspath(elevation_file)

    print(elevation_file)

    def _enqueue_kpi_job(public_id: str, job):
        """Enqueue a KPI computation for this instance on jobs:kpi (drained
        by worker-kpi, not this worker). Never raises — the simulation job
        already succeeded by the time this runs, and a flaky KPI-queue push
        shouldn't retroactively fail it. A missed KPI job just means the
        instance's KPIs stay stale until POST /kpis/recompute is called."""
        try:
            from rq import Queue

            queue_name = os.getenv("QUEUE_KPI", "jobs:kpi")
            if job is not None:
                conn = job.connection
            else:
                from redis import Redis

                conn = Redis.from_url(os.getenv("TETIS_REDIS_URL", "redis://redis:6379"))
            # RQ's default job timeout (180s) is too short for a first-time
            # OSM ingestion pass over a basin — see main.py's KPI_JOB_TIMEOUT.
            kpi_job_timeout = int(os.getenv("KPI_JOB_TIMEOUT", "1200"))
            Queue(queue_name, connection=conn).enqueue(
                "tasks.compute_kpis",
                {"public_id": public_id},
                job_timeout=kpi_job_timeout,
            )
            print(f"WORKER: enqueued KPI job for {public_id} on {queue_name}")
        except Exception as e:
            print(f"WORKER: failed to enqueue KPI job for {public_id}: {e}")

    def _summary(gz_bytes):
        """What goes back to RQ (and therefore into Redis) — never the blob."""
        return {
            "public_id": public_id,
            "persisted": public_id is not None,
            "bytes": len(gz_bytes),
        }

    def _persist_and_notify(gz_bytes):
        """Write the gzipped solution to the DB (if we know the instance) and
        publish the terminal 'complete' event."""
        if public_id is not None:
            try:
                db.save_solution_bytes(public_id, gz_bytes)
                print(f"WORKER: solution stored for instance {public_id}")
            except Exception as e:
                print(f"WORKER: ERROR storing solution: {e}")
                if job is not None:
                    job.connection.publish(
                        channel_for(job.id),
                        encode(
                            "error",
                            {"detail": f"Failed to store solution: {e}"},
                        ),
                    )
                raise

            # KPI computation runs on its own queue/worker (worker-kpi),
            # entirely decoupled from the solve — see docs/architecture.md.
            # _enqueue_kpi_job swallows its own failures: a KPI-enqueue
            # problem must never fail the simulation job that just succeeded.
            _enqueue_kpi_job(public_id, job)

        if job is not None:
            job.meta["progress"] = 1.0
            job.meta["status_message"] = "Simulation complete"
            job.save_meta()
            job.connection.publish(
                channel_for(job.id),
                encode("complete", {"job_id": job.id, "public_id": public_id}),
            )

    if not use_subprocess:
        # ---- In-process path ----
        args = argparse.Namespace(
            payload=None,
            result=None,
            job_id=job_id,
            output_name=f"{output_name}_{job_id}",
            yieldstep=yieldstep,
            src_epsg=src_epsg,
            dst_epsg=dst_epsg,
            elevation_file=elevation_file,
            gpu_mode=int(os.getenv("ANUGA_GPU_MODE", "2")),
        )
        result = _run_gpu_worker(args, payload=payload)  # gzipped bytes
        _persist_and_notify(result)
        return _summary(result)

    # ---- Out-of-process path ----
    workdir = tempfile.mkdtemp(prefix=f"anuga_{job_id}_")
    payload_path = os.path.join(workdir, "payload.json")
    result_path = os.path.join(workdir, "result.json.gz")
    this_file = os.path.abspath(__file__)

    proc = None
    try:
        # Write the UNWRAPPED payload — the subprocess expects {config, features}.
        with open(payload_path, "w") as f:
            json.dump(payload, f)

        cmd = [
            sys.executable,
            "-u",
            this_file,
            "--gpu-worker",
            "--payload",
            payload_path,
            "--result",
            result_path,
            "--job-id",
            job_id,
            "--output-name",
            f"{output_name}_{job_id}",
            "--yieldstep",
            str(yieldstep),
            "--src-epsg",
            str(src_epsg),
            "--dst-epsg",
            str(dst_epsg),
            "--elevation-file",
            elevation_file,
            "--gpu-mode",
            os.getenv("ANUGA_GPU_MODE", "2"),
        ]
        print(f"WORKER: running: {' '.join(cmd)}")

        env = os.environ.copy()
        env.setdefault("OMP_NUM_THREADS", "1")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=workdir,
            env=env,
        )

        if job is not None:
            job.meta["gpu_pid"] = proc.pid
            job.save_meta()

        captured = []
        for line in proc.stdout:
            print(line, end="")
            captured.append(line)
        proc.wait()

        if proc.returncode != 0:
            tail = "".join(captured[-60:]) or "(no output captured)"
            raise RuntimeError(
                f"GPU simulation failed (exit code {proc.returncode}).\n"
                f"--- subprocess output (last 60 lines) ---\n{tail}"
            )

        with open(result_path, "rb") as f:
            result = f.read()  # gzipped JSON bytes, same as in-process

        # Parent worker (which has DB access) persists; the subprocess does not.
        _persist_and_notify(result)
        return _summary(result)

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# PART 2 — GPU worker (single process; imports anuga lazily)
# ===========================================================================


def _reproject_polygon(coordinates, src_epsg=4326, dst_epsg=25830):
    raw_coords = coordinates[0]
    # Frontend (DeckGL/GeoJSON) already sends [Lon, Lat] — no swap needed.
    poly = Polygon(raw_coords)
    gdf = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{src_epsg}")
    gdf = gdf.to_crs(f"EPSG:{dst_epsg}")
    return list(gdf.geometry[0].exterior.coords)


def _close_ring_removed(coords):
    """Drop the duplicated closing vertex of a polygon ring, if present."""
    if coords and coords[0] == coords[-1]:
        return coords[:-1]
    return coords


# Matches utils/stormPlacement.ts's METRES_PER_DEG_LAT — halfW/halfH were
# built on the frontend with this same approximation, so converting them
# back to metres here (rather than re-deriving from the storm's own
# cell_size_m) reproduces exactly the footprint the user saw/dragged.
_METRES_PER_DEG_LAT = 111_320


def _fetch_storm_cube(storm_ref, par):
    """The storm cube, fetched ONCE per job rather than once per rank.

    The cube is ~666MB and lives in a Postgres large object reached over the
    tailnet. Measured on a DERP-relayed link that is ~3 MB/s, i.e. ~4 minutes
    per copy — and every rank was paying it, so an N-GPU run spent N x 4 min
    parked at 0% before meshing even started. Two layers:

      1. A local cache keyed by storm_ref. A stored cube never changes, so a
         re-run costs a disk read instead of a download.
      2. Rank 0 fetches and MPI-broadcasts. Intra-node shared memory moves
         666MB in well under a second, against minutes over the tailnet.

    Cache lives beside the job's scratch, not in the image, and a failure to
    write it is not fatal — it is an optimisation, not state.
    """
    # `db` is imported function-locally everywhere else in this module (see
    # run_anuga and the storm loop) rather than at module scope, so it is NOT a
    # global here. Omitting this raised NameError on the first real run and,
    # because a rank-0 exception under MPI deadlocks rather than exits (see
    # hpc_run.py), the job then held its GPUs until the wall clock expired.
    import db

    cache_dir = os.getenv("HPC_STORM_CACHE", "")
    cache = os.path.join(cache_dir, f"storm-{storm_ref}.npz") if cache_dir else ""

    proxy_conf = os.getenv("HPC_DB_PROXY_CONF", "")

    def _fetch_via_proxy():
        """Multi-node: the solver is never proxied (LD_PRELOAD breaks MPI), so
        the cube is fetched by a short-lived subprocess that writes it to the
        cache path, and we then load it like any cache hit."""
        import subprocess

        target = cache or os.path.join(
            os.getenv("HPC_WORKDIR", "/scratch"), f"storm-{storm_ref}.npz"
        )
        cmd = ["proxychains4", "-f", proxy_conf, "-q",
               "python", "/app/src/db_proxy.py", "get-storm", storm_ref, target]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if out.returncode != 0:
            raise RuntimeError(
                f"storm fetch via proxy failed: {(out.stderr or out.stdout)[-400:]}"
            )
        with np.load(target, allow_pickle=True) as z:
            return z["cube"], z["meta"].item()

    def _load_local():
        if cache and os.path.exists(cache):
            try:
                with np.load(cache, allow_pickle=True) as z:
                    print(f"[tasks:gpu] storm {storm_ref}: cache hit {cache}")
                    return z["cube"], z["meta"].item()
            except Exception as exc:  # noqa: BLE001
                print(f"[tasks:gpu] storm cache unreadable ({exc}); refetching")
        return None, None

    if not par["parallel"]:
        cube, meta = _load_local()
        if cube is None:
            if proxy_conf:
                cube, meta = _fetch_via_proxy()
            else:
                cube, meta = db.get_storm_cube(storm_ref)
                if cube is not None and cache:
                    try:
                        np.savez(cache, cube=cube,
                                 meta=np.array(meta, dtype=object))
                    except OSError as exc:
                        print(f"[tasks:gpu] could not cache storm: {exc}")
        return cube, meta

    comm, myid = par["comm"], par["myid"]
    cube = meta = None
    if myid == 0:
        cube, meta = _load_local()
        if cube is None:
            if proxy_conf:
                cube, meta = _fetch_via_proxy()
            else:
                cube, meta = db.get_storm_cube(storm_ref)
                if cube is not None and cache:
                    try:
                        np.savez(cache, cube=cube,
                                 meta=np.array(meta, dtype=object))
                    except OSError as exc:
                        print(f"[tasks:gpu] could not cache storm: {exc}")

    # Shape/dtype first so the others can preallocate; None means "not found",
    # and every rank must agree on that or they deadlock in the Bcast below.
    header = comm.bcast(
        None if cube is None else (cube.shape, str(cube.dtype), meta), root=0
    )
    if header is None:
        return None, None
    shape, dtype, meta = header
    if myid != 0:
        cube = np.empty(shape, dtype=np.dtype(dtype))
    comm.Bcast(np.ascontiguousarray(cube) if myid == 0 else cube, root=0)
    if myid == 0:
        print(f"[tasks:gpu] storm {storm_ref}: broadcast {cube.nbytes/1e6:.0f}MB "
              f"to {par['numprocs']} ranks")
    return cube, meta


def _storm_placement_to_domain(
    placement, xllcorner, yllcorner, src_epsg=4326, dst_epsg=25830
):
    """
    Reproject a storm's placement — {centerLng, centerLat, halfW, halfH
    (degrees), rotationDeg} as saved by the frontend — into the domain's
    LOCAL frame (metres, relative to xllcorner/yllcorner) that
    storm_sampler.build_storm_driver expects: {centerX, centerY, halfW, halfH
    (metres), rotationDeg}. Subtracting the corner here — rather than handing
    back absolute EPSG coordinates — matches domain.centroid_coordinates,
    the frame anuga.Rate_operator itself samples a spatial rate(x,y,t) in.
    """
    center_lng = float(placement["centerLng"])
    center_lat = float(placement["centerLat"])

    transformer = pyproj.Transformer.from_crs(
        f"EPSG:{src_epsg}", f"EPSG:{dst_epsg}", always_xy=True
    )
    abs_x, abs_y = transformer.transform(center_lng, center_lat)

    m_per_deg_lng = _METRES_PER_DEG_LAT * math.cos(math.radians(center_lat))

    return {
        "centerX": abs_x - xllcorner,
        "centerY": abs_y - yllcorner,
        "halfW": float(placement["halfW"]) * m_per_deg_lng,
        "halfH": float(placement["halfH"]) * _METRES_PER_DEG_LAT,
        "rotationDeg": float(placement.get("rotationDeg", 0.0)),
    }


def _build_vertices(domain, dst_epsg, src_epsg):
    """Reproject mesh nodes back to lat/lon once."""
    nodes = domain.mesh.nodes
    abs_nodes = domain.geo_reference.get_absolute(nodes)
    gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(abs_nodes[:, 0], abs_nodes[:, 1]),
        crs=f"EPSG:{dst_epsg}",
    ).to_crs(f"EPSG:{src_epsg}")
    # (N, 2) float64 [lon, lat] — kept as an array, not objects, so the wet
    # subset can be fancy-indexed and written straight into a binary block.
    return np.column_stack(
        [gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()]
    ).astype(np.float64)


def _parallel_setup(domain):
    """Partition `domain` across MPI ranks, returning what the gather needs.

    A no-op with one rank: anuga.distribute() returns the domain untouched, so
    the in-cluster (KEDA) path and a hand-run single-GPU job take exactly the
    same code path they always did.

    Imported lazily. anuga.parallel pulls in mpi4py, and the k8s worker has no
    reason to initialise MPI just to import this module.
    """
    from anuga import distribute, myid, numprocs

    if numprocs == 1:
        return {"domain": domain, "parallel": False, "myid": 0, "numprocs": 1}

    domain = distribute(domain)

    from mpi4py import MPI

    comm = MPI.COMM_WORLD
    # Ghost cells are duplicated on a neighbour; only OWNED cells are gathered,
    # or the overlap would be written twice and the counts would not add up to
    # the global triangle count.
    owned = np.asarray(domain.tri_full_flag) == 1
    gids = np.asarray(domain.tri_l2g, dtype=np.int64)[owned]

    counts = np.array(comm.allgather(gids.size), dtype=np.int64)
    displs = np.concatenate(([0], np.cumsum(counts)[:-1]))
    all_gids = np.empty(int(counts.sum()), dtype=np.int64) if myid == 0 else None
    comm.Gatherv(
        gids, (all_gids, counts, displs, MPI.INT64_T) if myid == 0 else None, root=0
    )

    return {
        "domain": domain,
        "parallel": True,
        "myid": myid,
        "numprocs": numprocs,
        "comm": comm,
        "MPI": MPI,
        "owned": owned,
        "counts": counts,
        "displs": displs,
        "all_gids": all_gids,
        "n_global": int(counts.sum()),
        "recv": np.empty(int(counts.sum()), dtype=np.float32) if myid == 0 else None,
    }


def _snapshot_parallel(snapshots, domain, t, par):
    """One frame, reassembled on rank 0 from every rank's owned cells.

    Gatherv rather than comm.gather: the latter pickles, and at ~7MB per array
    per frame across a thousand frames that overhead is the difference between
    a gather that disappears into the solve and one that dominates it.

    Only rank 0 accumulates, so peak memory is unchanged from the serial path —
    it is the same global arrays, just assembled from pieces.
    """
    MPI = par["MPI"]
    comm, owned, myid = par["comm"], par["owned"], par["myid"]
    q = domain.quantities
    frame = []
    for name in ("stage", "xmomentum", "ymomentum"):
        send = np.ascontiguousarray(
            np.asarray(q[name].centroid_values, dtype=np.float32)[owned]
        )
        comm.Gatherv(
            send,
            (par["recv"], par["counts"], par["displs"], MPI.FLOAT)
            if myid == 0
            else None,
            root=0,
        )
        if myid == 0:
            glob = np.empty(par["n_global"], dtype=np.float32)
            glob[par["all_gids"]] = par["recv"]
            frame.append(glob)
    if myid == 0:
        snapshots.append((float(t), frame[0], frame[1], frame[2]))


def _snapshot(snapshots, domain, t):
    """Per-yieldstep capture: 3 float32 copies, no Python per-triangle work.

    np.array(..., dtype=float32) COPIES — required, because centroid_values
    is ANUGA's live buffer and is overwritten on the next step.
    """
    q = domain.quantities
    snapshots.append(
        (
            float(t),
            np.array(q["stage"].centroid_values, dtype=np.float32),
            np.array(q["xmomentum"].centroid_values, dtype=np.float32),
            np.array(q["ymomentum"].centroid_values, dtype=np.float32),
        )
    )


def _finalize_result(
    snapshots,
    tri_indices,
    elev,
    friction,
    vertices,
    src_epsg,
    dst_epsg,
    depth_threshold=0.01,
):
    """Derive all quantities vectorized, filter dry triangles, and pack the
    result as one binary blob (see _encode_result_binary). Nothing here ever
    becomes a per-triangle Python object."""
    times = [s[0] for s in snapshots]
    stage = np.stack([s[1] for s in snapshots])  # (T, N) float32
    xmom = np.stack([s[2] for s in snapshots])
    ymom = np.stack([s[3] for s in snapshots])

    depth = stage - elev[None, :]
    speed = np.sqrt(xmom**2 + ymom**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        xvel = np.where(depth > 1e-6, xmom / depth, 0.0)
        yvel = np.where(depth > 1e-6, ymom / depth, 0.0)

    # Wet filter on the raw depth — no rounding happens anywhere now, so the
    # subtlety that used to live here (coarse rounding silently zeroing
    # sub-centimetre storm depths before the threshold could see them) is gone:
    # values go to the wire as float32, which resolves far below any depth a
    # solver produces.
    # Cells that never hold more than this are dropped from the result. The old
    # default was 1e-5 m — 0.01mm — which was fine for inlet-driven runs where
    # most of the domain stays dry, and useless for storm-driven ones: rain
    # falls everywhere, so essentially every cell passed. A 4.6M-triangle storm
    # run kept 4,575,988 of them and produced a 1.8GB decoded buffer. 1cm is
    # the smallest depth anyone would call a flood.
    wet = depth.max(axis=0) > depth_threshold  # (N,)
    wet_idx = np.flatnonzero(wet)

    tri_wet = tri_indices[wet_idx]  # (W, 3)
    used = np.unique(tri_wet)  # sorted old vertex ids
    remapped = np.searchsorted(used, tri_wet)  # (W, 3) new ids

    # vertices is (N, 2) float64 [lon, lat] — fancy-index straight to the
    # subset the wet triangles actually reference.
    new_vertices = vertices[used]

    # Transpose to (W, T) so each triangle's series is one contiguous row,
    # which is the layout the frontend indexes as [tri * nFrames + frame].
    depth_w = depth[:, wet_idx].T
    speed_w = speed[:, wet_idx].T

    return _encode_result_binary(
        times=times,
        vertex_lonlat=new_vertices,
        tri_indices=remapped,
        elevation=elev[wet_idx],
        depth=depth_w,
        speed=speed_w,
    )


# Magic + version for the binary result container. Bump the suffix if the
# block layout ever changes incompatibly; the frontend checks it.
RESULT_MAGIC = b"HFR1"

_DTYPES = {"f8": np.float64, "f4": np.float32, "i4": np.int32}


def _encode_result_binary(times, vertex_lonlat, tri_indices, elevation, depth, speed):
    """
    Pack the solution as: magic | header length | JSON header | typed blocks.

    Why not JSON any more: a JSON result has to be materialised in the browser
    as ONE JavaScript string before JSON.parse can run, and V8 caps a single
    string near 512MB regardless of how much heap the process was given. Large
    runs sailed past that and surfaced as "Unexpected end of JSON input".
    Typed blocks are read straight into TypedArrays — no intermediate string,
    no millions of per-triangle JS objects, and decoding is a memcpy rather
    than a parse.

    The header stays JSON because it is tiny and self-describing; only the bulk
    numeric arrays go binary.
    """
    blocks = {}
    payload = bytearray()

    def add(name, arr, dtype):
        a = np.ascontiguousarray(arr, dtype=_DTYPES[dtype]).reshape(-1)
        # Pad so every block starts 8-byte aligned. TypedArray views over an
        # ArrayBuffer must be aligned to their element size or the constructor
        # throws, and f8 needs 8.
        payload.extend(b"\0" * ((-len(payload)) % 8))
        blocks[name] = {"dtype": dtype, "offset": len(payload), "length": int(a.size)}
        payload.extend(a.tobytes())

    add("vertexLonLat", vertex_lonlat, "f8")  # f8: ~1e-7 deg matters at metre scale
    add("triIndices", tri_indices, "i4")
    add("elevation", elevation, "f4")
    add("depth", depth, "f4")
    add("speed", speed, "f4")

    header = {
        "version": 1,
        "nVertices": int(np.asarray(vertex_lonlat).shape[0]),
        "nTriangles": int(np.asarray(tri_indices).shape[0]),
        "nFrames": len(times),
        "times": [float(t) for t in times],
        "blocks": blocks,
    }
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    # Pad the header so the block section itself starts 8-byte aligned
    # (4 magic + 4 length + header). Trailing spaces are legal JSON whitespace.
    head += b" " * ((-(8 + len(head))) % 8)

    return b"".join(
        [RESULT_MAGIC, struct.pack("<I", len(head)), head, bytes(payload)]
    )


def _publish_progress(job_id, pct, msg):
    if not job_id or job_id == "local":
        return
    try:
        from redis import Redis
        from events import channel_for, encode

        url = os.getenv("REDIS_URL", "redis://redis:6379")
        Redis.from_url(url).publish(
            channel_for(job_id),
            encode("progress", {"progress": pct, "status_message": msg}),
        )
    except Exception as e:
        print(f"[tasks:gpu] progress publish failed: {e}")


def _make_q(q_spec):
    """Discharge spec -> scalar or callable(t).

    WARNING: the 'python' form exec()s payload code in this worker — that is
    remote code execution if the API is ever exposed beyond trusted users.
    Prefer sending a hydrograph as [[t, Q], ...] pairs (handled below).
    """
    if isinstance(q_spec, dict):
        if q_spec.get("type") == "python":
            ns = {}
            exec(q_spec["code"], {}, ns)  # noqa: S102 — trusted payloads only
            return ns["Q"]
        if q_spec.get("type") == "series":
            pts = np.asarray(q_spec["points"], dtype=float)  # [[t, Q], ...]
            ts, qs = pts[:, 0], pts[:, 1]
            return lambda t: float(np.interp(t, ts, qs))
    return q_spec  # plain scalar


def _make_rate(q_spec):
    """Rate spec -> scalar or callable(t).

    WARNING: the 'python' form exec()s payload code in this worker — that is
    remote code execution if the API is ever exposed beyond trusted users.
    Prefer sending a hydrograph as [[t, Q], ...] pairs (handled below).
    """
    if isinstance(q_spec, dict):
        if q_spec.get("type") == "python":
            ns = {}
            exec(q_spec["code"], {}, ns)  # noqa: S102 — trusted payloads only
            return ns["rate"]
        if q_spec.get("type") == "series":
            pts = np.asarray(q_spec["points"], dtype=float)  # [[t, Q], ...]
            ts, qs = pts[:, 0], pts[:, 1]
            return lambda t: float(np.interp(t, ts, qs))
    else:
        q_spec = q_spec * 1e-3 / 3600.0
    return q_spec  # plain scalar


def _run_gpu_worker(args, payload=None):
    """Single-process GPU simulation. Returns gzipped JSON bytes of a
    binary result container. If args.result is set, also writes those bytes there."""
    import anuga

    src_epsg, dst_epsg = args.src_epsg, args.dst_epsg

    if payload is None:
        with open(args.payload) as f:
            payload = json.load(f)

    # Rank identity up front: it gates the domain build below. anuga defines
    # these even without MPI (0 and 1), so the serial path is unchanged.
    from anuga import myid as _myid

    _is_root = _myid == 0

    config = payload["config"]
    features = payload["features"]
    duration = config["duration"]
    yieldstep = config.get("output_timestep", args.yieldstep)
    # Depth below which a cell is not worth storing. Per-instance so a fine
    # study can lower it; the default keeps result size proportional to the
    # flood rather than to the rainfall footprint.
    min_depth = float(config.get("min_depth", os.getenv("ANUGA_MIN_DEPTH", "0.01")))

    # Only rank 0 clips the DEM (below), so this must exist on every rank or the
    # others NameError before they ever reach distribute().
    elevation_file = None

    # ---- Reproject all feature polygons ----
    for ftype in [
        "region",
        "inlet",
        "rate",
        "elevation",
        "interiorBoundry",
        "meshResolution",
    ]:
        for feat in features.get(ftype, []):
            feat["_coords_proj"] = _reproject_polygon(
                feat["geometry"]["coordinates"], src_epsg, dst_epsg
            )

            # Rank 0 only: this writes a ~512MB ASCII grid, and only rank 0's
            # domain survives distribute(). Five ranks meant 2.5GB of duplicate
            # NFS writes for one usable raster.
            if ftype == "region" and _is_root:
                elevation_file = clip_dem_to_asc(
                    feat["geometry"]["coordinates"],
                    ers_path="./src/MDT_malla_5m_etrs89h30.ers",
                    src_epsg=4326,
                    # dst_epsg=25830,
                    out_path="region.asc",
                )

    if _is_root:
        print(f"[tasks:gpu] elevation raster: {elevation_file}")
    sim_region = features["region"][0]
    abs_coords = _close_ring_removed(sim_region["_coords_proj"])
    xs, ys = zip(*abs_coords)
    xllcorner, yllcorner = min(xs), min(ys)
    rel_coords = [[x - xllcorner, y - yllcorner] for x, y in abs_coords]

    geo_ref = anuga.Geo_reference(
        epsg=dst_epsg, xllcorner=xllcorner, yllcorner=yllcorner
    )
    boundary_tags = {}
    for i, edge in enumerate(sim_region["edges"]):
        boundary_tags.setdefault(edge["boundary"], []).append(i)

    # ---- Variable-resolution interior regions ----
    interior_regions = []
    for res_feat in features.get("meshResolution", []):  # NOTE: not `region`!
        res_abs = _close_ring_removed(res_feat["_coords_proj"])
        resolution = res_feat["properties"]["resolution"]
        res_rel = [[x - xllcorner, y - yllcorner] for x, y in res_abs]
        interior_regions.append([res_rel, resolution])

    interior_holes = []
    for res_feat in features.get("interiorBoundry", []):  # NOTE: not `region`!
        res_abs = res_feat["_coords_proj"]
        res_rel = [[x - xllcorner, y - yllcorner] for x, y in res_abs]
        interior_holes.append(res_rel)
    # ---- Build the global domain: RANK 0 ONLY -----------------------------
    # anuga's documented pattern (parallel/tests/run_parallel_distribute_domain.py,
    # and the MPI page in the docs): the master builds, everyone else passes
    # None to distribute(), which only ever reads its argument on rank 0.
    #
    # This is not a micro-optimisation. Every rank was meshing the SAME domain
    # and throwing 'its copy away: a 5-rank job on a 2.2M-triangle mesh wrote
    # five 512MB region.asc files to NFS and ran five single-threaded mesh
    # builds against 4 allocated CPUs, all of it invisible because progress is
    # only published once the solve starts.
    #
    # Everything above this point is cheap and deterministic (reprojection,
    # boundary tags, relative coordinates) and every rank still needs those
    # values afterwards to rebuild boundaries and operators on its own
    # subdomain, so only the expensive part is guarded.
    if _is_root:
        domain = anuga.create_domain_from_regions(
            rel_coords,
            boundary_tags=boundary_tags,
            maximum_triangle_area=config["mesh_max_area"],
            interior_regions=interior_regions,
            interior_holes=interior_holes,
        )
        domain.geo_reference = geo_ref
        # domain.set_zone(dst_epsg - 25800)  # 30
        # domain.geo_reference.zone = dst_epsg - 25800
        # domain.geo_reference.south = False  # ETRS89/UTM30N is northern
        # domain.geo_reference.hemisphere = "north"
        domain.set_name(args.output_name)
        # utm_zone = dst_epsg - 25800
        # domain.set_zone(utm_zone)
        domain.set_store(False)  # JSON is the product; skip per-yieldstep .sww
        gr = domain.geo_reference
        print("=== ZONE DEBUG ===")
        print("domain.get_zone():", domain.get_zone())
        print("geo_reference.zone:", getattr(gr, "zone", "MISSING"))
        print(
            "geo_reference.south / hemisphere:",
            getattr(gr, "south", getattr(gr, "hemisphere", "MISSING")),
        )
        print("==================")
        if config.get("flow_algorithm"):
            # Cost per step on GPU: DE0 ~1x (Euler, ANUGA default),
            # DE_ader2 ~1.1x (2nd order in time), DE1 ~2x (RK2), DE2 ~3x (RK3).
            domain.set_flow_algorithm(config["flow_algorithm"])

        # ---- Quantities (order matters: elevation before stage expression) ----
        props = sim_region["properties"]
        domain.set_quantity("elevation", filename=elevation_file, location="centroids")
        domain.set_quantity(
            "friction",
            props.get("friction", config.get("manning_default", 0.03)),
            location="centroids",
        )
        initial_stage = props.get("initial_stage")
        if initial_stage is None:
            # Dry start: water surface == terrain.
            domain.set_quantity("stage", expression="elevation", location="centroids")
        else:
            domain.set_quantity("stage", initial_stage, location="centroids")

        # ---- Boundaries (GPU-native types only -> keeps the fused C RK loop) ----
        print(f" interior holes{interior_holes}")

        B = []
        print(features)
        B = []
        for elevation_feat in features.get("elevation", []):
            coords_abs = elevation_feat["_coords_proj"]
            coords_rel = [[x - xllcorner, y - yllcorner] for x, y in coords_abs]
            value = elevation_feat["properties"]["elevation"]
            B.append((coords_rel, value))

        if B:
            domain.add_quantity(
                "elevation",
                Polygon_function(B, default=0.0),
                location="centroids",
            )
        # ---- Static per-triangle data (captured ONCE) ----
        vertices = _build_vertices(domain, dst_epsg=dst_epsg, src_epsg=src_epsg)
        tri_indices = np.asarray(domain.mesh.triangles, dtype=np.int64)
        elev = np.array(domain.quantities["elevation"].centroid_values, dtype=np.float32)
        friction = np.array(domain.quantities["friction"].centroid_values, dtype=np.float32)
    else:
        # distribute() ignores this on non-root ranks; the globals below are
        # rank 0's alone and are only used to build the result, which rank 0
        # also does.
        domain = None
        vertices = tri_indices = elev = friction = None
    _publish_progress(args.job_id, 0, "Building domain")

    # ---- Partition across MPI ranks (no-op when running on one) -------------
    # ANUGA puts one GPU on each rank (gpu_domain_init: device_id = rank %
    # num_devices), so N GPUs means N ranks and nothing else does.
    #
    # This sits exactly here for two reasons. Everything above is global and
    # has to be captured BEFORE partitioning — vertices, triangle indices,
    # elevation and friction describe the whole mesh, and after distribute()
    # each rank only sees its own slice. Everything below is the operators,
    # which distribute() does NOT carry: it transfers points, vertices,
    # boundary, quantities and the boundary map, and nothing else. An operator
    # created before this line would simply vanish on every rank.
    #
    # On ranks other than 0 the domain built above is thrown away — distribute
    # only reads its argument on rank 0. That wastes a mesh build per extra
    # rank; it is concurrent so it costs wall-clock nothing, and it keeps this
    # a small change to a function the in-cluster path also depends on.
    _par = _parallel_setup(domain)
    domain = _par["domain"]

    # Evolve settings must be re-applied AFTER distribute for the same reason
    # as the boundaries: distribute() transfers quantities and the boundary map,
    # not solver configuration, so a parallel domain silently reverts to anuga's
    # default 1000s cap. Measured: the serial run held dt <= 5s while the 2-rank
    # run opened with dt in [96, 1000] — a different trajectory through the
    # cold start, from the same inputs.
    domain.set_minimum_allowed_height(0.01)  # Ignore tiny puddles
    domain.set_maximum_allowed_speed(20.0)  # Cap water speed at 20 m/s
    domain.set_evolve_max_timestep(5.0)  # cap the cold-start step

    # ---- Boundaries (AFTER distribute) -------------------------------------
    # Ordering matters and matches anuga's own parallel test
    # (anuga/parallel/tests/run_parallel_distribute_domain.py): set_quantity
    # before distribute, set_boundary after. Every boundary object is
    # constructed with a reference to `domain` — Reflective_boundary(domain) —
    # so building them before partitioning would bind them to the sequential
    # domain that distribute() then replaces, leaving each rank evaluating its
    # boundaries against a mesh it no longer owns.
    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "transmissive": lambda: (
            anuga.Transmissive_n_momentum_zero_t_momentum_set_stage_boundary(
                domain, function=lambda t: 0
            )
        ),
    }
    boundries = {tag: bc_factory[tag]() for tag in boundary_tags}
    if len(interior_holes) > 0:
        boundries["interior"] = bc_factory["reflective"]()
    domain.set_boundary(boundries)

    # ---- Inflows ----
    # Rate_operator.inflow is GPU-resident (rate cached on device, one kernel
    # per step, no per-step D2H gathers), so the polygon size doesn't matter.
    # Semantics: total inflow == Q m^3/s over the polygon; Q scalar or Q(t).
    # NOTE vs Inlet_operator: water is added with zero momentum and no
    # surface-levelling — compare depths near the source once before trusting.
    for inlet in features.get("inlet", []):
        inlet_abs = _close_ring_removed(inlet["_coords_proj"])
        q = _make_q(inlet["properties"]["Q"])
        inlet_rel = [[x - xllcorner, y - yllcorner] for x, y in inlet_abs]
        anuga.Rate_operator.inflow(domain, rate=q, polygon=inlet_rel)

    for inlet in features.get("rate", []):
        inlet_abs = _close_ring_removed(inlet["_coords_proj"])
        q = _make_rate(inlet["properties"]["rate"])
        inlet_rel = [[x - xllcorner, y - yllcorner] for x, y in inlet_abs]
        anuga.Rate_operator(domain, rate=q, polygon=inlet_rel)

    # ---- Historical storms (rainfall raster, spatially + temporally varying) ----
    # Placement is saved in WGS84 degrees (the frontend gizmo); reproject it
    # into the domain's absolute EPSG:{dst_epsg} frame before sampling. No
    # `polygon=` kwarg on Rate_operator — the rate function already returns 0
    # for centroids outside the placed footprint (see storm_sampler.py), so
    # it can safely span the whole domain instead of being clipped to one.
    storms = features.get("storm", [])
    if storms:
        # flat import to match the worker's import root (see `events`)
        import db
        import storm_sampler

        for storm_feat in storms:
            storm_ref = storm_feat["storm_ref"]
            cube, storm_meta = _fetch_storm_cube(storm_ref, _par)
            if cube is None:
                print(f"[tasks:gpu] WARNING: storm {storm_ref} not found, skipping")
                continue
            domain_placement = _storm_placement_to_domain(
                storm_feat["placement"],
                xllcorner,
                yllcorner,
                src_epsg=src_epsg,
                dst_epsg=dst_epsg,
            )
            print(
                f"[tasks:gpu] storm {storm_ref}: meta={storm_meta} "
                f"raw_placement={storm_feat['placement']} "
                f"domain_placement={domain_placement}"
            )
            scale = float(storm_feat.get("scale", 1.0))
            driver = storm_sampler.build_storm_driver(
                domain, cube, storm_meta, domain_placement, scale=scale
            )
            # StormRateOperator (not a bare Rate_operator with a rate(x,y,t)
            # callable) — a callable spatial rate is not GPU-offloadable and
            # would force a GPU<->CPU sync of every quantity on every RK2
            # stage. See storm_sampler.py's module docstring.
            storm_sampler.StormRateOperator(domain, driver)

    # ---- Enable GPU acceleration (after boundaries/operators exist) ----
    print(f"[tasks:gpu] enabling GPU mode {args.gpu_mode}")
    domain.set_multiprocessor_mode(args.gpu_mode)

    # ---- Evolve ----
    snapshots = []
    _is_root = _par["myid"] == 0
    for t in domain.evolve(yieldstep=yieldstep, duration=duration):
        if _is_root:
            domain.print_timestepping_statistics()
        if _par["parallel"]:
            _snapshot_parallel(snapshots, domain, t, _par)
        else:
            _snapshot(snapshots, domain, t)
        # One progress stream, not N identical ones racing on the same channel.
        if _is_root:
            _publish_progress(args.job_id, round(t / duration * 100, 1), f"t={t}")

    # ---- Post-process once, vectorized ----
    # Only rank 0 holds the assembled snapshots, so only rank 0 can build the
    # result. The others have finished their share of the solve and return
    # None; hpc_run.py exits them quietly. They must NOT touch Postgres or
    # Redis — N ranks writing one solution would race over the same row.
    if _par["parallel"] and _par["myid"] != 0:
        from anuga import barrier, finalize

        barrier()
        finalize()
        return None

    print("building result (vectorized)")
    result = _finalize_result(
        snapshots,
        tri_indices,
        elev,
        friction,
        vertices,
        src_epsg=src_epsg,
        dst_epsg=dst_epsg,
        depth_threshold=min_depth,
    )

    print("gziping")
    _publish_progress(args.job_id, 100, "Simulation complete")
    # `result` is already the packed binary container.
    gz = gzip.compress(result)
    size_mb = len(gz) / (1024 * 1024)
    print(
        f"Compressed payload size: {len(gz)} bytes ({size_mb:.2f} MB) "
        f"from {len(result) / (1024 * 1024):.2f} MB raw"
    )

    if args.result is not None:
        with open(args.result, "wb") as f:
            f.write(gz)
        print(f"[tasks:gpu] wrote result to {args.result}")

    print("done")
    if _par["parallel"]:
        # Rank 0 waits for the others before finalizing MPI, so a rank cannot
        # tear down the communicator while another is still in the barrier.
        from anuga import barrier, finalize

        barrier()
        finalize()
    return gz


# ===========================================================================
# Entrypoint router (only used for the optional out-of-process path)
# ===========================================================================


def _parse_gpu_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu-worker", action="store_true")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--job-id", default="local")
    ap.add_argument("--output-name", default="simulation")
    ap.add_argument("--yieldstep", type=float, default=20)
    ap.add_argument("--src-epsg", type=int, default=4326)
    ap.add_argument("--dst-epsg", type=int, default=25830)
    ap.add_argument("--elevation-file", default="./src/mi_terreno.asc")
    ap.add_argument("--gpu-mode", type=int, default=2)
    return ap.parse_args()


if __name__ == "__main__":
    args = _parse_gpu_args()
    _run_gpu_worker(args)
