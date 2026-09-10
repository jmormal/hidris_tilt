"""
The actual KPI math. Runs entirely from a decoded HFR1 solution (see hfr1.py)
plus OSM reference data cached in Postgres (see osm.py) — never touches
ANUGA or re-runs anything, which is the whole point: bump KPI_VERSION and
re-run compute_kpis() to change what's measured, without re-solving.
"""

import json

import numpy as np
import pyproj
from shapely.geometry import Polygon, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

import db
import osm

# v2: buildings.critical_facilities_affected -> critical_facilities, now
# listing every critical facility in the bbox (with an `affected` flag and
# name/coordinates) instead of only the flooded ones, so the map layer can
# show what's safe too.
# v3: each critical facility also carries its full GeoJSON footprint
# (`geometry`), not just a centroid, so the map can draw the real building
# outline instead of a generic point.
KPI_VERSION = 3

# A meaningfully flooded depth, not the file's own 1e-5m wet-filter epsilon
# (that's a numerical cutoff that decides whether a triangle is stored at
# all, not whether it represents a real flood hazard).
FLOOD_DEPTH_THRESHOLD_M = 0.1

# Depth x velocity danger bands (m^2/s) — a standard hydraulic-hazard
# convention, not invented for this project: below ~0.5 is broadly safe,
# 0.5-1.0 is dangerous for vehicles, above 1.0 is dangerous for people caught
# in the flow.
HAZARD_VEHICLE_M2S = 0.5
HAZARD_PEOPLE_M2S = 1.0

# Building severity bands, aligned to the frontend's own depth color ramp
# (services/frontend/src/utils/colors.ts: MAX_VALUES.depth = 3.0, so these
# are that scale's thirds) so a building's KPI severity means the same thing
# as its color on the map.
SEVERITY_MEDIUM_M = 1.0
SEVERITY_HIGH_M = 2.0

CRITICAL_AMENITIES = {"hospital", "school", "fire_station", "police", "clinic"}

_TO_METRIC = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25830", always_xy=True).transform
_METRIC_TRANSFORM = lambda geom: transform(_TO_METRIC, geom)  # noqa: E731


def compute(dataset: dict) -> dict:
    depth = dataset["depth"]  # (nTriangles, nFrames)
    speed = dataset["speed"]
    times = dataset["times"]
    vertex_lonlat = dataset["vertex_lonlat"]
    tri_indices = dataset["tri_indices"]

    vx, vy = _TO_METRIC(vertex_lonlat[:, 0], vertex_lonlat[:, 1])
    vx, vy = np.asarray(vx), np.asarray(vy)
    area_m2 = _triangle_areas(vx, vy, tri_indices)

    wet_mask = depth > FLOOD_DEPTH_THRESHOLD_M
    ever_wet = wet_mask.any(axis=1)
    max_depth_per_tri = depth.max(axis=1)

    flood = _flood_kpis(area_m2, depth, speed, times, wet_mask, ever_wet, max_depth_per_tri)

    ever_wet_idx = np.flatnonzero(ever_wet)
    tri_polys = [Polygon(vertex_lonlat[tri_indices[t]]) for t in ever_wet_idx]
    tri_depths = max_depth_per_tri[ever_wet_idx]
    flood_extent = unary_union(tri_polys) if tri_polys else None
    tree = STRtree(tri_polys) if tri_polys else None

    # psycopg2 can't adapt numpy scalars, so cast to plain Python floats
    # before they touch any db.* call.
    min_lon = float(vertex_lonlat[:, 0].min())
    min_lat = float(vertex_lonlat[:, 1].min())
    max_lon = float(vertex_lonlat[:, 0].max())
    max_lat = float(vertex_lonlat[:, 1].max())
    osm.ensure_coverage(min_lon, min_lat, max_lon, max_lat)

    buildings = _building_kpis(
        min_lon, min_lat, max_lon, max_lat, flood_extent, tree, tri_polys, tri_depths
    )
    landuse = _landuse_kpis(min_lon, min_lat, max_lon, max_lat, flood_extent)

    return {
        "meta": {
            "kpi_version": KPI_VERSION,
            "flood_depth_threshold_m": FLOOD_DEPTH_THRESHOLD_M,
            "bbox": [min_lon, min_lat, max_lon, max_lat],
        },
        "flood": flood,
        "buildings": buildings,
        "landuse": landuse,
    }


def _triangle_areas(vx: np.ndarray, vy: np.ndarray, tri_indices: np.ndarray) -> np.ndarray:
    """Shoelace formula, vectorized, on projected (metric) coordinates —
    lon/lat degrees are not equal-area, so this must run post-reprojection."""
    p0, p1, p2 = tri_indices[:, 0], tri_indices[:, 1], tri_indices[:, 2]
    x0, y0 = vx[p0], vy[p0]
    x1, y1 = vx[p1], vy[p1]
    x2, y2 = vx[p2], vy[p2]
    return 0.5 * np.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))


def _flood_kpis(area_m2, depth, speed, times, wet_mask, ever_wet, max_depth_per_tri) -> dict:
    n_frames = depth.shape[1]
    frame_dt = float(times[1] - times[0]) if n_frames > 1 else 0.0

    wet_count_per_frame = wet_mask.sum(axis=0)
    peak_frame = int(np.argmax(wet_count_per_frame)) if n_frames else 0
    peak_mask = wet_mask[:, peak_frame] if n_frames else np.zeros(0, dtype=bool)

    flooded_area_peak_m2 = float(area_m2[peak_mask].sum())
    flooded_area_ever_m2 = float(area_m2[ever_wet].sum())
    volume_peak_m3 = float((area_m2[peak_mask] * depth[peak_mask, peak_frame]).sum())

    duration_s_per_tri = wet_mask.sum(axis=1).astype(np.float64) * frame_dt

    hazard = depth * speed
    hazard_max_per_tri = hazard.max(axis=1) if n_frames else np.zeros(depth.shape[0])
    vehicle_danger = ever_wet & (hazard_max_per_tri > HAZARD_VEHICLE_M2S)
    people_danger = ever_wet & (hazard_max_per_tri > HAZARD_PEOPLE_M2S)

    return {
        "flooded_area_peak_m2": flooded_area_peak_m2,
        "flooded_area_ever_m2": flooded_area_ever_m2,
        "volume_peak_m3": volume_peak_m3,
        "max_depth_m": float(depth.max()) if depth.size else 0.0,
        "mean_peak_depth_m": float(max_depth_per_tri[ever_wet].mean()) if ever_wet.any() else 0.0,
        "peak_frame": peak_frame,
        "peak_time_s": float(times[peak_frame]) if n_frames else 0.0,
        "duration_s": {
            "max": float(duration_s_per_tri[ever_wet].max()) if ever_wet.any() else 0.0,
            "mean": float(duration_s_per_tri[ever_wet].mean()) if ever_wet.any() else 0.0,
        },
        "hazard_area_m2": {
            "vehicles": float(area_m2[vehicle_danger].sum()),
            "people": float(area_m2[people_danger].sum()),
        },
    }


def _building_kpis(
    min_lon, min_lat, max_lon, max_lat, flood_extent, tree, tri_polys, tri_depths
) -> dict:
    rows = db.get_osm_buildings_in_bbox(min_lon, min_lat, max_lon, max_lat)
    total = len(rows)

    by_severity = {"low": 0, "medium": 0, "high": 0}
    affected = 0
    # Every critical facility in the bbox, not just the affected ones — the
    # map layer needs the safe ones too, to show what's NOT at risk.
    critical_facilities = []

    for r in rows:
        geojson_dict = json.loads(r["geojson"])
        geom = shape(geojson_dict)
        tags = r.get("tags") or {}
        amenity = tags.get("amenity")
        is_critical = amenity in CRITICAL_AMENITIES

        intersects = flood_extent is not None and geom.intersects(flood_extent)
        max_depth = 0.0
        if intersects and tree is not None:
            affected += 1
            candidate_idx = tree.query(geom)
            for i in candidate_idx:
                if tri_polys[i].intersects(geom):
                    max_depth = max(max_depth, float(tri_depths[i]))
            band = (
                "high" if max_depth >= SEVERITY_HIGH_M
                else "medium" if max_depth >= SEVERITY_MEDIUM_M
                else "low"
            )
            by_severity[band] += 1

        if is_critical:
            centroid = geom.centroid
            critical_facilities.append({
                "osm_id": r["osm_id"],
                "name": tags.get("name"),
                "amenity": amenity,
                "lon": round(centroid.x, 6),
                "lat": round(centroid.y, 6),
                "affected": intersects,
                "max_depth_m": round(max_depth, 3),
                # Full footprint (already stored in osm_buildings.geom), not
                # just the centroid — the frontend draws the actual building
                # outline instead of a generic pin.
                "geometry": geojson_dict,
            })

    return {
        "count_total_in_bbox": total,
        "count_affected": affected,
        "by_severity": by_severity,
        "critical_facilities": critical_facilities,
    }


def _landuse_kpis(min_lon, min_lat, max_lon, max_lat, flood_extent) -> dict:
    rows = db.get_osm_landuse_in_bbox(min_lon, min_lat, max_lon, max_lat)
    area_by_class = {"urban": 0.0, "rural": 0.0, "water": 0.0}

    if flood_extent is not None:
        for r in rows:
            cls = r["landuse_class"]
            if cls not in area_by_class:
                continue
            geom = shape(json.loads(r["geojson"]))
            inter = geom.intersection(flood_extent)
            if inter.is_empty:
                continue
            area_by_class[cls] += _METRIC_TRANSFORM(inter).area

    return {
        "urban_area_m2": area_by_class["urban"],
        "rural_area_m2": area_by_class["rural"],
        "water_area_m2": area_by_class["water"],
        "classified_area_m2": sum(area_by_class.values()),
    }
