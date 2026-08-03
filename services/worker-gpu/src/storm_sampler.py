"""
storm_sampler.py — turn a stored storm cube + a user placement into a
GPU-resident rainfall operator for ANUGA.

Why this is not a rate(x, y, t) function
----------------------------------------
The obvious way to drive spatially varying rain is to hand
anuga.Rate_operator a callable rate(x, y, t). Don't: a callable spatial rate
sets rate_spatial=True, and Rate_operator._init_gpu() explicitly refuses to
offload spatial (or xarray) rates. The operator then counts as "CPU-only" in
Domain._has_cpu_only_fractional_operators(), which forces a full GPU->CPU->GPU
sync of every conserved quantity on EVERY RK2 stage:

    WARNING: CPU-only fractional operators detected (GPU<->CPU sync every RK2 step)
      - Rate_operator

That sync costs far more than the rain calculation itself and erases most of
the GPU speedup.

What we do instead
------------------
ANUGA *does* offload the 'centroid_array' rate type (a plain (N,) array, one
value per triangle centroid). A storm's spatial pattern is constant within a
frame — only the frame changes with time — so:

  - The centroid -> (row, col) mapping is precomputed ONCE (placement is
    static during a solve).
  - Per frame, a single vectorized gather builds the (N,) rate array.
  - StormRateOperator swaps that array via set_rate() only when the frame
    actually changes. set_rate() flips ANUGA's _gpu_rate_changed flag, so
    exactly one host->device copy happens per storm frame; every other step
    runs entirely on the device.

Semantics preserved from the previous rate(x, y, t) implementation:
  - Time lookup is a STEP function (floor to the current frame): the cube
    holds accumulated depth per interval, so a frame's value is the rain that
    fell across [t_i, t_{i+1}), held constant. Interpolating would smear the
    totals.
  - Units -> m/s:
        mm_per_step:  rate = depth_mm * 1e-3 / timestep_s
        mm_hr:        rate = intensity_mm_hr * 1e-3 / 3600
  - `scale` multiplies the whole storm — the per-placement "Rain Scale"
    property (default 1).
  - Centroids outside the placed grid, and times outside the storm, get zero.

The placement transform mirrors the frontend weather overlay:
    centerX, centerY  : grid centre in the domain's LOCAL frame (metres,
                        relative to xllcorner/yllcorner) — the same frame
                        domain.centroid_coordinates uses. NOT absolute EPSG.
    halfW, halfH      : half-extent of the placed grid (metres)
    rotationDeg       : clockwise rotation of the grid
The grid is sampled in its own local (un-rotated) frame: local +x spans cols
left->right, local +y spans rows. Row 0 is the TOP of the grid (north-up),
matching how the cube was stacked from rasters (band/row 0 = top).
"""

import anuga
import numpy as np


class StormRateDriver:
    """Precomputed centroid->cell mapping for one placed storm.

    Holds no ANUGA state; just answers "which frame is time t in?" and
    "what is the (N,) m/s rate array for frame f?".
    """

    def __init__(self, cube, conv, timestep_s, inside, row_in, col_in, n_centroids):
        self._cube = cube
        self._conv = conv  # cube units -> m/s, including `scale`
        self.timestep_s = timestep_s
        self.n_frames = cube.shape[0]
        self.last_frame = self.n_frames - 1

        self._inside = inside
        self._row_in = row_in
        self._col_in = col_in
        self._n = n_centroids

        # Shared dry array — never mutated, so it is safe to hand out repeatedly.
        self._zeros = np.zeros(n_centroids, dtype=np.float64)

    def frame_at(self, t):
        """Frame index covering time t, or -1 when the storm isn't raining.

        Both "before the storm" and "after the storm" collapse to -1 so the
        dry state is a single value — the operator's change detection then
        won't fire spuriously at the end of the storm.
        """
        if t < 0:
            return -1
        frame = int(t // self.timestep_s)
        if frame > self.last_frame:
            return -1
        return frame

    def rate_array(self, frame):
        """(N,) float64 array of m/s for `frame`, zero outside the footprint.

        float64 matches what ANUGA's GPU path uploads, so no extra conversion
        copy is made on the way to the device.
        """
        if frame < 0:
            return self._zeros
        rate = np.zeros(self._n, dtype=np.float64)
        # Gather only the covered centroids; assignment upcasts float32 -> float64.
        rate[self._inside] = self._cube[frame, self._row_in, self._col_in] * self._conv
        return rate


class StormRateOperator(anuga.Rate_operator):
    """Rate_operator whose centroid rate array is swapped at frame boundaries.

    Subclassing keeps this GPU-eligible: isinstance(op, Rate_operator) still
    holds for Domain._has_cpu_only_fractional_operators(), and the rate stays
    a 'centroid_array' (rate_spatial=False), which _init_gpu() accepts.

    __call__ runs every RK2 stage but only touches set_rate() when the storm
    frame actually advances, so the device copy of the rate is reused for
    every step within a frame.
    """

    def __init__(self, domain, driver, **kwargs):
        self._driver = driver
        self._current_frame = 0
        super().__init__(domain, rate=driver.rate_array(0), **kwargs)

    def __call__(self):
        frame = self._driver.frame_at(self.domain.get_time())
        if frame != self._current_frame:
            self._current_frame = frame
            self.set_rate(self._driver.rate_array(frame))
        return super().__call__()


def build_storm_driver(domain, cube, meta, placement, scale=1.0):
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
    StormRateDriver — feed it to StormRateOperator(domain, driver).
    """
    T, H, W = cube.shape
    timestep_s = float(meta["timestep_s"])
    units = meta.get("units", "mm_per_step")

    # Scalar conversion applied at gather time. Deliberately NOT materialised
    # over the whole cube: `cube * conv` would double the storm's resident
    # memory (a 100-frame 1000x1000 storm is ~400 MB per copy) for no gain,
    # since only one frame is ever needed at a time.
    if units == "mm_per_step":
        conv = (1e-3 / timestep_s) * scale
    else:  # mm_hr
        conv = (1e-3 / 3600.0) * scale

    cx = float(placement["centerX"])
    cy = float(placement["centerY"])
    halfW = float(placement["halfW"])
    halfH = float(placement["halfH"])
    rot = np.radians(float(placement.get("rotationDeg", 0.0)))

    # Domain centroid coords — the SAME attribute anuga.Rate_operator itself
    # reads (Operator.__init__: self.coord_c = self.domain.centroid_coordinates).
    # get_centroid_coordinates(absolute=True) looked like the "correct"
    # absolute-frame call, but in this build it does NOT reproduce the offset
    # the operator samples in (confirmed by a 0/N coverage count with a
    # placement that should overlap the domain) — so `placement`
    # (centerX/centerY) must be supplied in this same LOCAL frame (relative to
    # xllcorner/yllcorner), not the absolute EPSG frame.
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
        f"| cube shape={cube.shape} cube range=({cube.min():.3e},{cube.max():.3e})"
    )

    # Keep only the covered centroids' cells — the per-frame gather is then
    # exactly as long as the covered set, and needs no clamping/where().
    row_in = row[inside]
    col_in = col[inside]

    driver = StormRateDriver(
        cube=cube,
        conv=conv,
        timestep_s=timestep_s,
        inside=inside,
        row_in=row_in,
        col_in=col_in,
        n_centroids=cc.shape[0],
    )

    frame0 = driver.rate_array(0)
    covered = frame0[inside]
    if covered.size:
        print(
            f"[storm_sampler] frame0 sampled at domain cells: "
            f"row range=({row_in.min()},{row_in.max()}) "
            f"col range=({col_in.min()},{col_in.max()}) "
            f"rate m/s range=({covered.min():.3e},{covered.max():.3e}) "
            f"mean={covered.mean():.3e} | frames={T} timestep={timestep_s}s"
        )
    else:
        print("[storm_sampler] WARNING: placement covers no domain centroids — no rain will fall")

    return driver
