"""
storm_sampler.py — turn a stored storm cube + a user placement into a
spatially/temporally varying rate function rate(x, y, t) for ANUGA's
Rate_operator.

Design:
  - Placement is STATIC during a solve, so we precompute each domain centroid's
    (row, col) in the storm grid ONCE. The per-timestep work is then a pure
    vectorized gather: cube[frame, rows, cols].
  - Time lookup is a STEP function (floor to the current frame), because the
    cube holds accumulated depth per interval — a frame's value is the rain
    that fell across [t_i, t_{i+1}), held constant over that interval. (Linear
    interpolation would smear the totals, so we don't.)
  - Units -> m/s:
        mm_per_step:  rate = depth_mm * 1e-3 / timestep_s
        mm_hr:        rate = intensity_mm_hr * 1e-3 / 3600
  - `scale` multiplies the whole cube before conversion — the per-placement
    "Rain Scale" property (default 1), so the same catalog storm can be
    replayed at a different intensity per instance without re-uploading it.
  - Centroids outside the placed grid get zero rain.

The placement transform mirrors the frontend weather overlay:
    centerX, centerY  : grid centre in the domain's LOCAL frame (metres,
                        relative to xllcorner/yllcorner) — the same frame
                        domain.centroid_coordinates uses, i.e. what
                        anuga.Rate_operator itself samples a spatial rate(x,y,t)
                        in. NOT the absolute EPSG frame.
    halfW, halfH      : half-extent of the placed grid (metres)
    rotationDeg       : clockwise rotation of the grid
The grid is sampled in its own local (un-rotated) frame: local +x spans cols
left->right, local +y spans rows. Row 0 is the TOP of the grid (north-up),
matching how the cube was stacked from rasters (band/row 0 = top).
"""

import numpy as np


def build_storm_rate(domain, cube, meta, placement, scale=1.0):
    """
    Parameters
    ----------
    domain     : the ANUGA domain (already built; we read centroid coords).
    cube       : (T, H, W) float32 array of the storm.
    meta       : dict with timestep_s, units, cell_size_m, n_frames, grid_rows,
                 grid_cols, nodata.
    placement  : dict with centerX, centerY, halfW, halfH, rotationDeg
                 (in the domain's LOCAL frame, metres — see module docstring).
    scale      : multiplies every cell's rainfall value (default 1.0).

    Returns
    -------
    rate(x, y, t) -> ndarray of m/s, accepted by anuga Rate_operator.
    """
    T, H, W = cube.shape
    timestep_s = float(meta["timestep_s"])
    units = meta.get("units", "mm_per_step")

    # Convert the whole cube to m/s ONCE (cheap, and avoids per-step math).
    if units == "mm_per_step":
        cube_ms = cube.astype(np.float32) * (1e-3 / timestep_s) * scale
    else:  # mm_hr
        cube_ms = cube.astype(np.float32) * (1e-3 / 3600.0) * scale

    cx = float(placement["centerX"])
    cy = float(placement["centerY"])
    halfW = float(placement["halfW"])
    halfH = float(placement["halfH"])
    rot = np.radians(float(placement.get("rotationDeg", 0.0)))

    # Domain centroid coords — the SAME attribute anuga.Rate_operator itself
    # reads (Operator.__init__: self.coord_c = self.domain.centroid_coordinates)
    # when it calls a spatial rate(x, y, t). get_centroid_coordinates(absolute=True)
    # looked like the "correct" absolute-frame call, but in this build it does
    # NOT reproduce the offset Rate_operator actually samples in (confirmed by
    # a 0/N coverage count with a placement that should overlap the domain) —
    # so `placement` (centerX/centerY) must be supplied in this same LOCAL
    # frame (relative to xllcorner/yllcorner), not the absolute EPSG frame.
    cc = domain.centroid_coordinates  # (N, 2) in domain-local metres
    px = cc[:, 0]
    py = cc[:, 1]

    # Inverse-rotate centroid offsets into the grid's local axis-aligned frame.
    dx = px - cx
    dy = py - cy
    cos, sin = np.cos(-rot), np.sin(-rot)
    local_x = dx * cos - dy * sin      # spans [-halfW, +halfW] across cols
    local_y = dx * sin + dy * cos      # spans [-halfH, +halfH] across rows

    # Map local coords -> fractional grid indices.
    # col: local_x from -halfW (col 0 left edge) to +halfW (col W right edge)
    # row: local_y from +halfH (row 0 TOP) to -halfH (row H bottom)  [north-up]
    u = (local_x + halfW) / (2.0 * halfW)   # 0..1 left->right
    v = (halfH - local_y) / (2.0 * halfH)   # 0..1 top->bottom

    col = np.floor(u * W).astype(np.int64)
    row = np.floor(v * H).astype(np.int64)

    # Mask centroids that fall outside the placed grid -> no rain there.
    inside = (col >= 0) & (col < W) & (row >= 0) & (row < H)
    print(
        f"[storm_sampler] coverage: {int(inside.sum())}/{len(inside)} centroids "
        f"inside placement | domain bbox x=({px.min():.0f},{px.max():.0f}) "
        f"y=({py.min():.0f},{py.max():.0f}) | storm center=({cx:.0f},{cy:.0f}) "
        f"half=({halfW:.0f},{halfH:.0f}) rot={float(placement.get('rotationDeg', 0.0))} "
        f"| cube shape={cube.shape} cube_ms range=({cube_ms.min():.3e},{cube_ms.max():.3e})"
    )
    # Clamp the out-of-range indices to 0 so the gather is safe; we zero them
    # after the gather using `inside`.
    row_c = np.where(inside, row, 0)
    col_c = np.where(inside, col, 0)

    N = cc.shape[0]
    zeros = np.zeros(N, dtype=np.float32)
    last_frame = T - 1

    sample_at_domain = cube_ms[0, row_c[inside], col_c[inside]]
    print(
        f"[storm_sampler] frame0 sampled at domain cells: "
        f"row range=({row_c.min()},{row_c.max()}) col range=({col_c.min()},{col_c.max()}) "
        f"rate m/s range=({sample_at_domain.min():.3e},{sample_at_domain.max():.3e}) "
        f"mean={sample_at_domain.mean():.3e}"
    )

    def rate(x, y, t):
        # x, y are passed by ANUGA but our centroid mapping is precomputed and
        # static, so we ignore them and index by the precomputed cells.
        frame = int(t // timestep_s)
        if frame < 0:
            return zeros
        if frame > last_frame:
            return zeros  # storm finished -> dry
        vals = cube_ms[frame, row_c, col_c]   # (N,) gather
        return np.where(inside, vals, 0.0).astype(np.float32)

    return rate
