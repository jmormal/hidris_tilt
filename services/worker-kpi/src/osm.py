"""
OSM building/land-use ingestion, cached spatially so a basin is only ever
pulled from the Overpass API (via osmnx) once. Grid cells are the coverage
unit rather than per-instance bboxes — two instances drawn over the same
basin share the cache instead of each re-fetching their own bbox.
"""

import math

import osmnx as ox
from shapely.geometry import box

import db

# Pods are ephemeral (run-once, like every worker in this repo — see
# CLAUDE.md) so osmnx's on-disk cache buys nothing and just writes into a
# container that may have a read-only root filesystem.
ox.settings.use_cache = False
ox.settings.timeout = 120

GRID_DEG = 0.05  # ~5km cells at these latitudes

URBAN_LANDUSE = {
    "residential", "commercial", "industrial", "retail",
    "construction", "institutional", "education",
}
RURAL_LANDUSE = {
    "farmland", "farmyard", "orchard", "vineyard", "meadow",
    "grass", "allotments", "greenhouse_horticulture",
}
WATER_LANDUSE = {"reservoir", "basin"}
RURAL_NATURAL = {"wood", "forest", "scrub", "heath", "grassland"}
WATER_NATURAL = {"water", "wetland"}

CRITICAL_AMENITIES = {"hospital", "school", "fire_station", "police", "clinic"}


def classify_landuse(tags: dict) -> str:
    landuse = tags.get("landuse")
    natural = tags.get("natural")
    if landuse in URBAN_LANDUSE:
        return "urban"
    if landuse in RURAL_LANDUSE:
        return "rural"
    if landuse in WATER_LANDUSE or natural in WATER_NATURAL:
        return "water"
    if natural in RURAL_NATURAL:
        return "rural"
    return "unclassified"


def bbox_to_cells(min_lon, min_lat, max_lon, max_lat, grid_deg: float = GRID_DEG):
    x0, x1 = math.floor(min_lon / grid_deg), math.floor(max_lon / grid_deg)
    y0, y1 = math.floor(min_lat / grid_deg), math.floor(max_lat / grid_deg)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def ensure_coverage(min_lon, min_lat, max_lon, max_lat, grid_deg: float = GRID_DEG):
    """Fetch+store any grid cells overlapping the bbox that aren't already
    cached, marking each covered immediately after it succeeds so a crash
    partway through a large bbox doesn't lose progress on the rest."""
    cells = bbox_to_cells(min_lon, min_lat, max_lon, max_lat, grid_deg)
    missing = db.get_missing_osm_coverage(cells, grid_deg)
    for cx, cy in missing:
        _fetch_cell(cx, cy, grid_deg)
        db.mark_osm_coverage([(cx, cy)], grid_deg)


def _fetch_cell(cx: int, cy: int, grid_deg: float):
    min_lon, max_lon = cx * grid_deg, (cx + 1) * grid_deg
    min_lat, max_lat = cy * grid_deg, (cy + 1) * grid_deg
    cell_polygon = box(min_lon, min_lat, max_lon, max_lat)

    buildings_gdf = _safe_features(cell_polygon, {"building": True})
    if buildings_gdf is not None and len(buildings_gdf):
        db.upsert_osm_buildings(_building_rows(buildings_gdf))

    landuse_gdf = _safe_features(cell_polygon, {"landuse": True, "natural": True})
    if landuse_gdf is not None and len(landuse_gdf):
        db.upsert_osm_landuse(_landuse_rows(landuse_gdf))


def _safe_features(polygon, tags: dict):
    try:
        return ox.features_from_polygon(polygon, tags=tags)
    except Exception as e:
        # osmnx raises when a cell has zero matching features (e.g. open
        # water or mountain terrain with nothing tagged) — treat as an empty
        # cell rather than failing the whole KPI job over one grid square.
        print(f"KPI worker: OSM fetch for tags={tags} on cell {polygon.bounds} "
              f"returned nothing ({e})")
        return None


def _osm_id_from_index(idx) -> int:
    # osmnx >= 1.3 indexes features by a (element_type, osmid) MultiIndex;
    # older versions use a bare osmid. Either way the trailing element is the
    # numeric id we key osm_buildings/osm_landuse on.
    return int(idx[-1]) if isinstance(idx, tuple) else int(idx)


def _is_jsonable(v) -> bool:
    if v is None:
        return True
    if isinstance(v, bool):
        return True
    if isinstance(v, (str, int)):
        return True
    if isinstance(v, float):
        return not math.isnan(v)
    return False


def _tags_from_row(row) -> dict:
    return {k: v for k, v in row.items() if k != "geometry" and _is_jsonable(v)}


def _building_rows(gdf) -> list[dict]:
    rows = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        tags = _tags_from_row(row)
        rows.append({
            "osm_id": _osm_id_from_index(idx),
            "wkt": geom.wkt,
            "building_type": tags.get("building"),
            "amenity": tags.get("amenity"),
            "tags": tags,
        })
    return rows


def _landuse_rows(gdf) -> list[dict]:
    rows = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        tags = _tags_from_row(row)
        cls = classify_landuse(tags)
        if cls == "unclassified":
            continue
        rows.append({
            "osm_id": _osm_id_from_index(idx),
            "wkt": geom.wkt,
            "landuse_class": cls,
            "tags": tags,
        })
    return rows
