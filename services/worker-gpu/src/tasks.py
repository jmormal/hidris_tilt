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

            if ftype == "region":
                elevation_file = clip_dem_to_asc(
                    feat["geometry"]["coordinates"],
                    ers_path="./src/MDT_malla_5m_etrs89h30.ers",
                    src_epsg=4326,
                    # dst_epsg=25830,
                    out_path="region.asc",
                )

    print(f" elevation filw {elevation_file}")
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

    domain.set_minimum_allowed_height(0.01)  # Ignore tiny puddles

    domain.set_maximum_allowed_speed(20.0)  # Cap water speed at 20 m/s
    domain.set_evolve_max_timestep(5.0)  # cap the cold-start step
    # ---- Boundaries (GPU-native types only -> keeps the fused C RK loop) ----
    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "transmissive": lambda: (
            anuga.Transmissive_n_momentum_zero_t_momentum_set_stage_boundary(
                domain, function=lambda t: 0
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
            cube, storm_meta = db.get_storm_cube(storm_ref)
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
