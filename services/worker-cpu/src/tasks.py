"""
tasks.py — RQ task + parallel ANUGA simulation in ONE file.

Two entry modes, like the dana_benchmark self-relaunch pattern:

  1. RQ mode (default import):  the worker calls run_anuga(payload), which
     shells out to:
         mpiexec [--allow-run-as-root] -np N python tasks.py --mpi-worker ...
  2. MPI mode (--mpi-worker):   each rank runs _run_mpi_worker(), which builds
     the parallel domain, evolves, gathers centroids to rank 0, and writes
     result.json.

The original in-memory save logic (build_result_skeleton / append_timestep /
filter_active_mesh) is preserved. Rank 0 owns the full mesh + SimulationResult;
the per-timestep centroid arrays are gathered from all ranks via a COLLECTIVE
call (every rank must call it or the run deadlocks).

Env vars:
  ANUGA_NPROCS        MPI ranks                (default: 4)
  ANUGA_ALLOW_ROOT    pass --allow-run-as-root (default: 1, since worker is root)
  ANUGA_OVERSUBSCRIBE pass --oversubscribe     (default: 1)
  ANUGA_ELEVATION     path to the .asc DEM     (default: ./src/mi_terreno.asc)
  TETIS_REDIS_URL     redis url for progress   (default: redis://redis:6379)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon
from pydantic import BaseModel


# ===========================================================================
# Data model
# ===========================================================================


class Vertex(BaseModel):
    lat: float
    lon: float


class Triangle(BaseModel):
    vertices: tuple[int, int, int]
    elevation: float
    friction: float
    stage: list[float]
    depth: list[float]
    xmomentum: list[float]
    ymomentum: list[float]
    speed: list[float]
    xvelocity: list[float]
    yvelocity: list[float]


class SimulationResult(BaseModel):
    times: list[float]
    vertices: list[Vertex]
    triangles: list[Triangle]


# ===========================================================================
# PART 1 — RQ launcher (runs in the single RQ worker process)
# ===========================================================================


def run_anuga(
    payload,
    src_epsg=4326,
    dst_epsg=25830,
    output_name="simulation",
    yieldstep=20,
):
    """RQ entrypoint. Launches `tasks.py --mpi-worker` under mpiexec."""
    from rq import get_current_job
    from events import channel_for, encode

    print("WORKER: Received job. Launching MPI simulation...")
    job = get_current_job()
    job_id = job.id if job is not None else "local"

    nprocs = int(os.getenv("ANUGA_NPROCS", "7"))
    allow_root = os.getenv("ANUGA_ALLOW_ROOT", "1") == "1"
    oversubscribe = os.getenv("ANUGA_OVERSUBSCRIBE", "1") == "1"
    elevation_file = os.getenv("ANUGA_ELEVATION", "./src/mi_terreno.asc")
    if not os.path.isabs(elevation_file):
        elevation_file = os.path.abspath(elevation_file)

    workdir = tempfile.mkdtemp(prefix=f"anuga_{job_id}_")
    payload_path = os.path.join(workdir, "payload.json")
    result_path = os.path.join(workdir, "result.json")

    # This very file — re-invoked under mpiexec (self-relaunch pattern).
    this_file = os.path.abspath(__file__)

    proc = None
    try:
        with open(payload_path, "w") as f:
            json.dump(payload, f)

        mpi_prefix = ["mpiexec"]
        if allow_root:
            mpi_prefix.append("--allow-run-as-root")
        if oversubscribe:
            mpi_prefix.append("--oversubscribe")
        mpi_prefix += ["-np", str(nprocs)]

        cmd = mpi_prefix + [
            sys.executable,
            "-u",
            this_file,
            "--mpi-worker",
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
            job.meta["mpi_pid"] = proc.pid
            job.save_meta()

        captured = []
        for line in proc.stdout:
            print(line, end="")
            captured.append(line)
        proc.wait()

        if proc.returncode != 0:
            tail = "".join(captured[-60:]) or "(no output captured)"
            raise RuntimeError(
                f"MPI simulation failed (exit code {proc.returncode}).\n"
                f"--- subprocess output (last 60 lines) ---\n{tail}"
            )

        with open(result_path) as f:
            result = json.load(f)

        if job is not None:
            job.meta["progress"] = 1.0
            job.meta["status_message"] = "Simulation complete"
            job.save_meta()
            job.connection.publish(
                channel_for(job.id),
                encode(
                    "complete",
                    {"job_id": job.id, "file": f"/api/simulate/{job.id}/result"},
                ),
            )

        return result

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# PART 2 — MPI worker (runs once per rank under mpiexec)
# ===========================================================================
# Everything below imports anuga / mpi4py lazily, so importing this module in
# the RQ worker (PART 1) doesn't require a working MPI stack.


def _reproject_polygon(coordinates, src_epsg=4326, dst_epsg=25830):
    raw_coords = coordinates[0]
    # Frontend (DeckGL/GeoJSON) already sends [Lon, Lat] — no swap needed.
    poly = Polygon(raw_coords)
    gdf = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{src_epsg}")
    gdf = gdf.to_crs(f"EPSG:{dst_epsg}")
    return list(gdf.geometry[0].exterior.coords)


def _build_result_skeleton(domain, dst_epsg=25830, src_epsg=4326):
    """Run on rank 0 BEFORE distribute(), on the full sequential mesh."""
    nodes = domain.mesh.nodes
    abs_nodes = domain.geo_reference.get_absolute(nodes)

    gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(abs_nodes[:, 0], abs_nodes[:, 1]),
        crs=f"EPSG:{dst_epsg}",
    ).to_crs(f"EPSG:{src_epsg}")

    vertices = [Vertex(lat=float(pt.y), lon=float(pt.x)) for pt in gdf.geometry]

    tri_indices = domain.mesh.triangles
    elevation = domain.quantities["elevation"].centroid_values.copy()
    friction = domain.quantities["friction"].centroid_values.copy()

    triangles = []
    for i in range(len(tri_indices)):
        triangles.append(
            Triangle(
                vertices=(
                    int(tri_indices[i][0]),
                    int(tri_indices[i][1]),
                    int(tri_indices[i][2]),
                ),
                elevation=round(float(elevation[i]), 6),
                friction=round(float(friction[i]), 6),
                stage=[],
                depth=[],
                xmomentum=[],
                ymomentum=[],
                speed=[],
                xvelocity=[],
                yvelocity=[],
            )
        )

    return SimulationResult(times=[], vertices=vertices, triangles=triangles)


# --- centroid gather shim (resolved once) ---
_GATHER = None


def _resolve_gather(quantity, myid):
    global _GATHER
    if _GATHER is not None:
        return _GATHER
    for name in ("get_full_centroid_values", "get_full_values"):
        if hasattr(quantity, name):

            def _GATHER(q, _n=name):
                return np.asarray(getattr(q, _n)())

            if myid == 0:
                print(f"[tasks:mpi] centroid gather via Quantity.{name}()")
            return _GATHER
    if myid == 0:
        print("[tasks:mpi] WARNING: no get_full_* method; manual mpi4py gather")
    _GATHER = _manual_gather
    return _GATHER


def _manual_gather(quantity):
    from mpi4py import MPI

    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    domain = quantity.domain
    local_vals = np.asarray(quantity.centroid_values)

    full_flag = np.asarray(domain.tri_full_flag, dtype=bool)
    gid = np.asarray(
        domain.tri_l2g if hasattr(domain, "tri_l2g") else domain.mesh.tri_l2g
    )

    owned_vals = local_vals[full_flag]
    owned_gids = gid[full_flag]

    all_vals = comm.gather(owned_vals, root=0)
    all_gids = comm.gather(owned_gids, root=0)

    if rank != 0:
        return np.empty(0)

    total = sum(len(v) for v in all_vals)
    out = np.zeros(total, dtype=float)
    for vals, gids in zip(all_vals, all_gids):
        out[gids] = vals
    return out


def _gather_centroids(domain, name, myid):
    q = domain.quantities[name]
    return _resolve_gather(q, myid)(q)


def _append_timestep(result, domain, t, myid):
    """COLLECTIVE: gathers run on every rank; only rank 0 appends."""
    stage = _gather_centroids(domain, "stage", myid)
    elev = _gather_centroids(domain, "elevation", myid)
    xmom = _gather_centroids(domain, "xmomentum", myid)
    ymom = _gather_centroids(domain, "ymomentum", myid)

    if myid != 0:
        return

    result.times.append(t)
    depth = stage - elev
    speed = np.sqrt(xmom**2 + ymom**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        xvel = np.where(depth > 1e-6, xmom / depth, 0.0)
        yvel = np.where(depth > 1e-6, ymom / depth, 0.0)

    for i, tri in enumerate(result.triangles):
        tri.stage.append(round(float(stage[i]), 6))
        tri.depth.append(round(float(depth[i]), 6))
        tri.xmomentum.append(round(float(xmom[i]), 6))
        tri.ymomentum.append(round(float(ymom[i]), 6))
        tri.speed.append(round(float(speed[i]), 6))
        tri.xvelocity.append(round(float(xvel[i]), 6))
        tri.yvelocity.append(round(float(yvel[i]), 6))


def _filter_active_mesh(result, depth_threshold=1e-5):
    wet_triangles = []
    used_vertex_indices = set()
    for tri in result.triangles:
        if max(tri.depth) > depth_threshold:
            wet_triangles.append(tri)
            used_vertex_indices.update(tri.vertices)

    old_to_new = {}
    new_vertices = []
    for new_idx, old_idx in enumerate(sorted(used_vertex_indices)):
        old_to_new[old_idx] = new_idx
        old_v = result.vertices[old_idx]
        new_vertices.append(Vertex(lat=old_v.lat, lon=old_v.lon))

    remapped = []
    for tri in wet_triangles:
        remapped.append(
            Triangle(
                vertices=(
                    old_to_new[tri.vertices[0]],
                    old_to_new[tri.vertices[1]],
                    old_to_new[tri.vertices[2]],
                ),
                elevation=tri.elevation,
                friction=tri.friction,
                stage=tri.stage,
                depth=tri.depth,
                xmomentum=tri.xmomentum,
                ymomentum=tri.ymomentum,
                speed=tri.speed,
                xvelocity=tri.xvelocity,
                yvelocity=tri.yvelocity,
            )
        )

    return SimulationResult(
        times=result.times, vertices=new_vertices, triangles=remapped
    )


def _publish_progress(job_id, pct, msg):
    if not job_id or job_id == "local":
        return
    try:
        from redis import Redis
        from events import channel_for, encode

        url = os.getenv("TETIS_REDIS_URL", "redis://redis:6379")
        Redis.from_url(url).publish(
            channel_for(job_id),
            encode("progress", {"progress": pct, "status_message": msg}),
        )
    except Exception as e:
        print(f"[tasks:mpi] progress publish failed: {e}")


def _run_mpi_worker(args):
    """Runs once per MPI rank."""
    import anuga
    from anuga.parallel import myid, numprocs, distribute, barrier, finalize
    from mpi4py import MPI

    import anuga.parallel.parallel_inlet as _pi

    if not hasattr(anuga, "Compute_fluxes_boundary"):
        anuga.Compute_fluxes_boundary = type("Compute_fluxes_boundary", (), {})

    src_epsg, dst_epsg = args.src_epsg, args.dst_epsg

    with open(args.payload) as f:
        payload = json.load(f)
    config = payload["config"]
    features = payload["features"]
    duration = config["duration"]

    boundary_tags = None
    xllcorner = yllcorner = None
    domain = None
    result = None

    barrier()
    # ---- Sequential setup: rank 0 only ----
    if myid == 0:
        for ftype in ["region", "inlet", "rate", "elevation"]:
            for feat in features.get(ftype, []):
                feat["_coords_proj"] = _reproject_polygon(
                    feat["geometry"]["coordinates"], src_epsg, dst_epsg
                )

        region = features["region"][0]
        abs_coords = region["_coords_proj"]
        if abs_coords[0] == abs_coords[-1]:
            abs_coords = abs_coords[:-1]
        xs, ys = zip(*abs_coords)
        xllcorner, yllcorner = min(xs), min(ys)
        rel_coords = [[x - xllcorner, y - yllcorner] for x, y in abs_coords]

        geo_ref = anuga.Geo_reference(
            epsg=dst_epsg, xllcorner=xllcorner, yllcorner=yllcorner
        )

        boundary_tags = {}
        for i, edge in enumerate(region["edges"]):
            boundary_tags.setdefault(edge["boundary"], []).append(i)

        domain = anuga.create_domain_from_regions(
            rel_coords,
            boundary_tags=boundary_tags,
            maximum_triangle_area=config["mesh_max_area"],
        )
        domain.geo_reference = geo_ref
        domain.set_name(args.output_name)
        if config.get("flow_algorithm"):
            domain.set_flow_algorithm(config["flow_algorithm"])

        props = region["properties"]
        domain.set_quantity(
            "friction",
            props.get("friction", config["manning_default"]),
            location="centroids",
        )
        domain.set_quantity(
            "stage", props.get("initial_stage", 0), location="centroids"
        )
        domain.set_quantity(
            "elevation", filename=args.elevation_file, location="centroids"
        )
        domain.set_quantity("friction", 0.01, location="centroids")
        domain.set_quantity("stage", expression="elevation", location="centroids")

        result = _build_result_skeleton(domain, dst_epsg=dst_epsg, src_epsg=src_epsg)
        _publish_progress(args.job_id, 0, "Distributing mesh")
    else:
        domain = None

    # ---- Broadcast scalars needed post-distribute on all ranks ----
    comm = MPI.COMM_WORLD
    boundary_tags = comm.bcast(boundary_tags, root=0)
    xllcorner = comm.bcast(xllcorner, root=0)
    yllcorner = comm.bcast(yllcorner, root=0)
    features = comm.bcast(features, root=0)

    # ---- Split across ranks ----
    domain = distribute(domain)
    domain.set_maximum_allowed_speed(20.0)  # Cap water speed at 20 m/s
    domain.set_minimum_allowed_height(0.01)  # Ignore tiny puddles
    # ---- Post-distribute setup: ALL ranks ----
    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "transmissive": lambda: anuga.Transmissive_boundary(domain),
    }
    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "tr1ansmissive": lambda: anuga.Transmissive_boundary(domain),
        "transmissive": lambda: (
            anuga.Transmissive_n_momentum_zero_t_momentum_set_stage_boundary(
                domain, function=lambda t: -9999.0
            )
        ),
    }
    domain.set_boundary({tag: bc_factory[tag]() for tag in boundary_tags})

    for inlet in features.get("inlet", []):
        inlet_abs = inlet["_coords_proj"]
        if inlet_abs[0] == inlet_abs[-1]:
            inlet_abs = inlet_abs[:-1]

        q = inlet["properties"]["Q"]
        if isinstance(q, dict) and q.get("type") == "python":
            ns = {}
            exec(q["code"], {}, ns)
            q = ns["Q"]

        # Pass the ABSOLUTE coordinates as a line.
        # Anuga will use domain.geo_reference to offset them safely.
        anuga.Inlet_operator(domain, inlet_abs, Q=q)

    # ---- Evolve ----
    for t in domain.evolve(yieldstep=60, duration=duration):
        if myid == 0:
            domain.print_timestepping_statistics()
        _append_timestep(result, domain, float(t), myid)  # collective
        if myid == 0:
            _publish_progress(args.job_id, round(t / duration * 100, 1), f"t={t}")

    barrier()

    if myid == 0:
        print("filtering mesh")
        result = _filter_active_mesh(result, depth_threshold=1e-5)
        with open(args.result, "w") as f:
            json.dump(result.model_dump(), f)
        print(f"[tasks:mpi] wrote result to {args.result}")
        _publish_progress(args.job_id, 100, "Simulation complete")

    finalize()


# ===========================================================================
# Entrypoint router (self-relaunch pattern)
# ===========================================================================


def _parse_mpi_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mpi-worker", action="store_true")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--job-id", default="local")
    ap.add_argument("--output-name", default="simulation")
    ap.add_argument("--yieldstep", type=float, default=20)
    ap.add_argument("--src-epsg", type=int, default=4326)
    ap.add_argument("--dst-epsg", type=int, default=25830)
    ap.add_argument("--elevation-file", default="./src/mi_terreno.asc")
    return ap.parse_args()


if __name__ == "__main__":
    # Only reached when launched under mpiexec (or by hand for testing).
    args = _parse_mpi_args()
    _run_mpi_worker(args)
