import anuga
import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon
import copy
import json
from branca.colormap import LinearColormap
from branca.element import MacroElement, Template
from pydantic import BaseModel

from rq import get_current_job
from redis import Redis
from events import channel_for, encode
import os
import os

if not hasattr(anuga, "Compute_fluxes_boundary"):
    anuga.Compute_fluxes_boundary = type("Compute_fluxes_boundary", (), {})

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reproject_polygon(coordinates, src_epsg=4326, dst_epsg=25830):
    raw_coords = coordinates[0]

    # Frontend (DeckGL/GeoJSON) already sends [Lon, Lat] — no swap needed.
    poly = Polygon(raw_coords)
    gdf = gpd.GeoDataFrame(geometry=[poly], crs=f"EPSG:{src_epsg}")
    gdf = gdf.to_crs(f"EPSG:{dst_epsg}")
    return list(gdf.geometry[0].exterior.coords)


def build_result_skeleton(domain, dst_epsg=25830, src_epsg=4326):
    """
    Called once before the evolve loop.
    Creates the SimulationResult with vertices and triangles populated
    (static fields filled, dynamic lists empty).
    """
    # --- FIX 2: ANUGA ABSOLUTE NODE PROJECTION ---
    nodes = domain.mesh.nodes  # (n_vertices, 2) in local coords

    # Use geo_reference to properly apply the UTM offsets instead of get_nodes(absolute=True)
    abs_nodes = domain.geo_reference.get_absolute(nodes)

    gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(abs_nodes[:, 0], abs_nodes[:, 1]),
        crs=f"EPSG:{dst_epsg}",
    ).to_crs(f"EPSG:{src_epsg}")

    vertices = [Vertex(lat=float(pt.y), lon=float(pt.x)) for pt in gdf.geometry]

    # --- Triangles: connectivity + static quantities ---
    tri_indices = domain.mesh.triangles  # (n_triangles, 3) vertex indices
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


def append_timestep(result: SimulationResult, domain, t: float):
    """Append one timestep of dynamic values to every triangle."""
    result.times.append(t)

    stage = domain.quantities["stage"].centroid_values
    elev = domain.quantities["elevation"].centroid_values
    xmom = domain.quantities["xmomentum"].centroid_values
    ymom = domain.quantities["ymomentum"].centroid_values

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


def filter_active_mesh(
    result: SimulationResult, depth_threshold: float = 1e-5
) -> SimulationResult:
    """
    Filters a SimulationResult to only include triangles that experienced
    water depth greater than the threshold at any point during the simulation.
    Orphaned vertices are removed and triangle indices are remapped.
    """
    wet_triangles = []
    used_vertex_indices = set()

    # 1. Identify triangles that get "wet" at any timestep
    for tri in result.triangles:
        # Check if the maximum depth ever exceeds our threshold
        if max(tri.depth) > depth_threshold:
            wet_triangles.append(tri)
            # Record the vertices this triangle uses
            used_vertex_indices.update(tri.vertices)

    # 2. Create a mapping from old vertex indices to new, filtered vertex indices
    old_to_new_v_idx = {}
    new_vertices = []

    # Sort the indices to maintain spatial order
    for new_idx, old_idx in enumerate(sorted(used_vertex_indices)):
        old_to_new_v_idx[old_idx] = new_idx

        # EXTRACT the raw data to bypass Jupyter's class-redefinition quirk
        old_v = result.vertices[old_idx]
        new_vertices.append(Vertex(lat=old_v.lat, lon=old_v.lon))

    # 3. Rebuild the wet triangles with the remapped vertex indices
    remapped_triangles = []
    for tri in wet_triangles:
        remapped_triangles.append(
            Triangle(
                vertices=(
                    old_to_new_v_idx[tri.vertices[0]],
                    old_to_new_v_idx[tri.vertices[1]],
                    old_to_new_v_idx[tri.vertices[2]],
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

    # 4. Return a clean, heavily optimized SimulationResult
    return SimulationResult(
        times=result.times, vertices=new_vertices, triangles=remapped_triangles
    )


# ---------------------------------------------------------------------------
# ANUGA runner
# ---------------------------------------------------------------------------


def run_anuga(
    payload,
    src_epsg=4326,
    dst_epsg=25830,
    output_name="simulation",
    yieldstep=20,
    # duration=150,
):

    print("WORKER: Received job. Processing...")
    job = get_current_job()

    payload = copy.deepcopy(payload)
    config = payload["config"]
    features = payload["features"]
    duration = config["duration"]

    for ftype in ["region", "inlet", "rate", "elevation"]:
        for feat in features.get(ftype, []):
            feat["_coords_proj"] = reproject_polygon(
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
        tag = edge["boundary"]
        boundary_tags.setdefault(tag, []).append(i)

    domain = anuga.create_domain_from_regions(
        rel_coords,
        boundary_tags=boundary_tags,
        maximum_triangle_area=config["mesh_max_area"],
    )
    domain.geo_reference = geo_ref
    domain.set_name(output_name)
    if config.get("flow_algorithm"):
        domain.set_flow_algorithm(config["flow_algorithm"])

    props = region["properties"]
    domain.set_quantity(
        "friction",
        props.get("friction", config["manning_default"]),
        location="centroids",
    )
    domain.set_quantity("stage", props.get("initial_stage", 0), location="centroids")

    if job is not None:
        job.meta["progress"] = 0
        job.meta["status_message"] = f"Setting boundry"
        job.save_meta()
        job.connection.publish(
            channel_for(job.id),
            encode(
                "progress",
                {
                    "progress": 0,
                    "status_message": f"Setting boundry",
                },
            ),
        )

    bc_factory = {
        "reflective": lambda: anuga.Reflective_boundary(domain),
        "transmissive": lambda: anuga.Transmissive_boundary(domain),
    }
    domain.set_boundary({tag: bc_factory[tag]() for tag in boundary_tags})

    for inlet in features.get("inlet", []):
        inlet_abs = inlet["_coords_proj"]
        if inlet_abs[0] == inlet_abs[-1]:
            inlet_abs = inlet_abs[:-1]
        inlet_rel = [[x - xllcorner, y - yllcorner] for x, y in inlet_abs]
        q = inlet["properties"]["Q"]
        if isinstance(q, dict) and q.get("type") == "python":
            ns = {}
            exec(q["code"], {}, ns)
            q = ns["Q"]
        inlet_region = anuga.Region(domain, polygon=inlet_rel)
        anuga.Inlet_operator(domain, inlet_region, Q=q)

    if job is not None:
        job.meta["progress"] = 0
        job.meta["status_message"] = f"Setting Height"
        job.save_meta()
        job.connection.publish(
            channel_for(job.id),
            encode(
                "progress",
                {
                    "progress": 0,
                    "status_message": f"Setting heighit",
                },
            ),
        )

    domain.set_quantity(
        "elevation", filename="./src/mi_terreno.asc", location="centroids"
    )
    domain.set_quantity("friction", 0.01, location="centroids")
    domain.set_quantity("stage", expression="elevation", location="centroids")

    from anuga.utilities.animate import Domain_plotter

    dplotter = Domain_plotter(domain)

    # Build skeleton with vertices + triangles (empty dynamic lists)
    result = build_result_skeleton(domain, dst_epsg=dst_epsg, src_epsg=src_epsg)

    # Evolve and append each timestep
    for t in domain.evolve(yieldstep=yieldstep, duration=duration):
        domain.print_timestepping_statistics()
        dplotter.save_depth_frame()
        append_timestep(result, domain, float(t))

        if job is not None:
            job.meta["progress"] = t / duration
            job.meta["status_message"] = f"The simulation is at the second {t}"
            job.save_meta()
            job.connection.publish(
                channel_for(job.id),
                encode(
                    "progress",
                    {
                        "progress": round(t / duration * 100, 1),
                        "status_message": f"t={t}",
                    },
                ),
            )

    result = filter_active_mesh(result, depth_threshold=1e-5)
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
    return result.model_dump()
