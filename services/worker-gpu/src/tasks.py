"""
tasks.py — RQ task + GPU-accelerated ANUGA simulation in ONE file.

Refactored version. Changes vs the original:

  PERFORMANCE
  - Per-yieldstep saving is now an O(1)-Python snapshot of 3 float32 arrays
    (stage, xmom, ymom). Depth/speed/velocities are derived ONCE at the end,
    vectorized. (Was: a Python loop over every triangle x 7 quantities per
    yieldstep -> seconds of GPU-idle time, ~10x more RAM.)
  - Elevation/friction are static: captured once, not per timestep.
  - domain.set_store(False): skips the per-yieldstep .sww write + D2H sync
    (nobody reads the .sww; the JSON is the product).
  - Inlet discharge now uses Rate_operator.inflow (GPU-resident: no per-step
    device<->host gathers; polygon size no longer matters). Same semantics:
    total inflow = Q m^3/s, Q may be a scalar or a callable Q(t).

  BUG FIXES
  - friction / initial stage are no longer silently overwritten by leftover
    debug lines (friction=0.01, stage=elevation).
  - `region` loop-variable shadowing fixed (props were read from the last
    meshResolution feature when one existed).
  - bc_factory defined once; typo entry "tr1ansmissive" removed. Both
    remaining boundary types are GPU-native, so the fused C RK loop is kept.
  - Subprocess mode now returns the same thing as in-process mode
    (gzipped JSON bytes).

Env vars:
  ANUGA_GPU_MODE        multiprocessor mode int  (default: 2)
  ANUGA_ELEVATION       path to the .asc DEM     (default: ./src/mi_terreno.asc)
  TETIS_REDIS_URL       redis url for progress   (default: redis://redis:6379)
  ANUGA_GPU_SUBPROCESS  "1" -> run simulation out-of-process (recommended in
                        production: a native crash can't kill the RQ worker,
                        and GPU state is fully released between jobs).
"""

import argparse
import gzip
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

from anuga.geometry.polygon_function import Polygon_function

# ===========================================================================
# Data model (unchanged wire format)
# ===========================================================================
PRECISION_SAVE = 1  # decimals kept in the JSON. NOTE: depths < 0.05 m round
# to 0.0 — bump to 2 if small depths matter (gzip makes
# the size cost small).


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
    """RQ entrypoint. Runs the GPU simulation, either in-process (default) or
    out-of-process via `tasks.py --gpu-worker` when ANUGA_GPU_SUBPROCESS=1.

    Returns gzipped JSON bytes of a SimulationResult in BOTH modes.
    """
    from rq import get_current_job
    from events import channel_for, encode

    print("WORKER: Received job. Starting GPU simulation...")
    job = get_current_job()
    job_id = job.id if job is not None else "local"

    use_subprocess = os.getenv("ANUGA_GPU_SUBPROCESS", "0") == "1"
    elevation_file = os.getenv("ANUGA_ELEVATION", "./src/mi_terreno.asc")
    if not os.path.isabs(elevation_file):
        elevation_file = os.path.abspath(elevation_file)

    def _notify_complete():
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
        result = _run_gpu_worker(args, payload=payload)
        _notify_complete()
        return result

    # ---- Out-of-process path ----
    workdir = tempfile.mkdtemp(prefix=f"anuga_{job_id}_")
    payload_path = os.path.join(workdir, "payload.json")
    result_path = os.path.join(workdir, "result.json.gz")
    this_file = os.path.abspath(__file__)

    proc = None
    try:
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

        _notify_complete()
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


def _build_vertices(domain, dst_epsg, src_epsg):
    """Reproject mesh nodes back to lat/lon once."""
    nodes = domain.mesh.nodes
    abs_nodes = domain.geo_reference.get_absolute(nodes)
    gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(abs_nodes[:, 0], abs_nodes[:, 1]),
        crs=f"EPSG:{dst_epsg}",
    ).to_crs(f"EPSG:{src_epsg}")
    return [Vertex(lat=float(pt.y), lon=float(pt.x)) for pt in gdf.geometry]


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
    depth_threshold=1e-5,
):
    """Derive all quantities vectorized, filter dry triangles, build the
    SimulationResult ONCE. Only wet triangles ever become Python objects."""
    times = [s[0] for s in snapshots]
    stage = np.stack([s[1] for s in snapshots])  # (T, N) float32
    xmom = np.stack([s[2] for s in snapshots])
    ymom = np.stack([s[3] for s in snapshots])

    depth = stage - elev[None, :]
    speed = np.sqrt(xmom**2 + ymom**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        xvel = np.where(depth > 1e-6, xmom / depth, 0.0)
        yvel = np.where(depth > 1e-6, ymom / depth, 0.0)

    # float64 BEFORE rounding so 0.1 serializes as 0.1 (a rounded float32
    # converted later would print as 0.10000000149...).
    def prep(a):
        return np.round(a.astype(np.float64), PRECISION_SAVE)

    stage, depth, xmom, ymom = prep(stage), prep(depth), prep(xmom), prep(ymom)
    speed, xvel, yvel = prep(speed), prep(xvel), prep(yvel)

    # Wet filter on the ROUNDED depths (matches the original behaviour:
    # with PRECISION_SAVE=1 the effective threshold is one rounding unit).
    wet = depth.max(axis=0) > depth_threshold  # (N,)
    wet_idx = np.flatnonzero(wet)

    tri_wet = tri_indices[wet_idx]  # (W, 3)
    used = np.unique(tri_wet)  # sorted old vertex ids
    remapped = np.searchsorted(used, tri_wet)  # (W, 3) new ids

    new_vertices = [vertices[int(i)] for i in used]

    elev_r = np.round(elev.astype(np.float64), PRECISION_SAVE)
    fric_r = np.round(friction.astype(np.float64), PRECISION_SAVE)

    # Transpose to (N, T) so each triangle's series is one contiguous row.
    stage_t, depth_t = stage.T, depth.T
    xmom_t, ymom_t = xmom.T, ymom.T
    speed_t, xvel_t, yvel_t = speed.T, xvel.T, yvel.T

    triangles = []
    for w, i in enumerate(wet_idx):
        triangles.append(
            Triangle(
                vertices=(
                    int(remapped[w, 0]),
                    int(remapped[w, 1]),
                    int(remapped[w, 2]),
                ),
                elevation=float(elev_r[i]),
                friction=float(fric_r[i]),
                stage=stage_t[i].tolist(),
                depth=depth_t[i].tolist(),
                xmomentum=xmom_t[i].tolist(),
                ymomentum=ymom_t[i].tolist(),
                speed=speed_t[i].tolist(),
                xvelocity=xvel_t[i].tolist(),
                yvelocity=yvel_t[i].tolist(),
            )
        )

    return SimulationResult(times=times, vertices=new_vertices, triangles=triangles)


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
    else:
        q_spec = q_spec * 1e-3 / 3600.0
    return q_spec  # plain scalar


def _run_gpu_worker(args, payload=None):
    """Single-process GPU simulation. Returns gzipped JSON bytes of a
    SimulationResult. If args.result is set, also writes those bytes there."""
    import anuga

    src_epsg, dst_epsg = args.src_epsg, args.dst_epsg

    if payload is None:
        with open(args.payload) as f:
            payload = json.load(f)

    config = payload["config"]
    features = payload["features"]
    duration = config["duration"]
    yieldstep = config.get("output_timestep", args.yieldstep)

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
    domain = anuga.create_domain_from_regions(
        rel_coords,
        boundary_tags=boundary_tags,
        maximum_triangle_area=config["mesh_max_area"],
        interior_regions=interior_regions,
        interior_holes=interior_holes,
    )
    domain.geo_reference = geo_ref
    domain.set_name(args.output_name)
    domain.set_store(False)  # JSON is the product; skip per-yieldstep .sww
    if config.get("flow_algorithm"):
        # Cost per step on GPU: DE0 ~1x (Euler, ANUGA default),
        # DE_ader2 ~1.1x (2nd order in time), DE1 ~2x (RK2), DE2 ~3x (RK3).
        domain.set_flow_algorithm(config["flow_algorithm"])

    # ---- Quantities (order matters: elevation before stage expression) ----
    props = sim_region["properties"]
    domain.set_quantity("elevation", filename=args.elevation_file, location="centroids")
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

    domain.set_minimum_allowed_height(0.01)  # Ignore tiny puddles

    domain.set_evolve_max_timestep(5.0)  # cap the cold-start step
    # ---- Boundaries (GPU-native types only -> keeps the fused C RK loop) ----
    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "transmissive": lambda: (
            anuga.Transmissive_n_momentum_zero_t_momentum_set_stage_boundary(
                domain, function=lambda t: -9999.0
            )
        ),
    }
    boundries = {tag: bc_factory[tag]() for tag in boundary_tags}
    print(f" interior holes{interior_holes}")
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
    _publish_progress(args.job_id, 0, "Building domain")

    # ---- Enable GPU acceleration (after boundaries/operators exist) ----
    print(f"[tasks:gpu] enabling GPU mode {args.gpu_mode}")
    domain.set_multiprocessor_mode(args.gpu_mode)

    # ---- Evolve ----
    snapshots = []
    for t in domain.evolve(yieldstep=yieldstep, duration=duration):
        domain.print_timestepping_statistics()
        _snapshot(snapshots, domain, t)
        _publish_progress(args.job_id, round(t / duration * 100, 1), f"t={t}")

    # ---- Post-process once, vectorized ----
    print("building result (vectorized)")
    result = _finalize_result(
        snapshots,
        tri_indices,
        elev,
        friction,
        vertices,
        src_epsg=src_epsg,
        dst_epsg=dst_epsg,
        depth_threshold=1e-5,
    )

    print("gziping")
    _publish_progress(args.job_id, 100, "Simulation complete")
    json_bytes = result.model_dump_json().encode("utf-8")
    gz = gzip.compress(json_bytes)
    size_mb = len(gz) / (1024 * 1024)
    print(f"Compressed payload size: {len(gz)} bytes ({size_mb:.2f} MB)")

    if args.result is not None:
        with open(args.result, "wb") as f:
            f.write(gz)
        print(f"[tasks:gpu] wrote result to {args.result}")

    print("done")
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
