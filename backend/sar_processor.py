"""
SAR change-detection processing utilities for NISAR GCOV / Sentinel-1 data.

All computations are in linear power → dB space.  NoData pixels are masked so
they never pollute statistics or the rendered PNG.
"""

from __future__ import annotations

import base64
import io
from typing import Optional

import h5py
import matplotlib
matplotlib.use("Agg")  # headless backend — must be set before pyplot import
import matplotlib.pyplot as plt
import numpy as np

EPS = 1e-5  # floor for log-safe clipping
CHANGE_THRESHOLD_DB = 3.0  # |diff| > 3 dB → "changed"
DOWNSAMPLE_FACTOR = 4  # how aggressively to subsample large HDF5 rasters


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

def generate_synthetic_sar_pair(seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate a pair of 512×512 float32 arrays that mimic SAR backscatter.

    The *pre* image is spatially-correlated speckle noise.  The *post* image is
    the same field with a localised ~6 dB change injected in a circular region
    near the centre, simulating deforestation or flooding.
    """
    rng = np.random.default_rng(seed)

    # Spatially-correlated base field (linear power, positive)
    raw = rng.exponential(scale=0.05, size=(512, 512)).astype(np.float32)
    # Light 3×3 box-blur for spatial correlation
    kernel = np.ones((3, 3), dtype=np.float32) / 9.0
    from numpy.lib.stride_tricks import sliding_window_view
    padded = np.pad(raw, 1, mode="reflect")
    windows = sliding_window_view(padded, (3, 3))
    pre = np.mean(windows, axis=(-2, -1)).astype(np.float32)
    pre = np.clip(pre, EPS, None)

    # Post: inject a circular change zone (centre ±64 px, radius 80 px)
    post = pre.copy()
    cy, cx = 256 + rng.integers(-64, 64), 256 + rng.integers(-64, 64)
    yy, xx = np.ogrid[:512, :512]
    mask = ((yy - cy) ** 2 + (xx - cx) ** 2) <= 80**2
    # ~6 dB increase inside the zone
    post[mask] *= 10 ** (6.0 / 10.0)

    return pre, post


# ---------------------------------------------------------------------------
# HDF5 I/O
# ---------------------------------------------------------------------------

_DEFAULT_DATASET = "science/LSAR/GCOV/grids/frequencyA/HHHH"


def load_h5_slice(
    file_path: str,
    dataset_path: str = _DEFAULT_DATASET,
) -> tuple[np.ndarray, dict]:
    """
    Read a 2-D slice from an HDF5/NetCDF file and return it with metadata.

    Parameters
    ----------
    file_path : str
        Path to the .h5 / .nc file.
    dataset_path : str
        Internal HDF5 dataset path.

    Returns
    -------
    array : np.ndarray  (float32, 2-D, down-sampled)
    metadata : dict      keys: bounds, geotransform, shape_original, crs
    """
    with h5py.File(file_path, "r") as f:
        if dataset_path not in f:
            available = []
            f.visit(lambda name: available.append(name))
            raise FileNotFoundError(
                f"Dataset '{dataset_path}' not found in {file_path}. "
                f"Available datasets: {available[:30]}"
            )

        ds = f[dataset_path]
        # Down-sample large rasters so the browser can handle the PNG
        step = DOWNSAMPLE_FACTOR
        if ds.ndim == 2:
            arr = ds[::step, ::step].astype(np.float32)
        elif ds.ndim == 3:
            arr = ds[0, ::step, ::step].astype(np.float32)
        else:
            raise ValueError(
                f"Expected 2-D or 3-D dataset, got {ds.ndim}-D ({ds.shape})."
            )

        # Keep NaN / Inf as NaN so they are properly masked in compute_change_detection
        nan_mask = ~np.isfinite(arr)
        arr[nan_mask] = np.nan

        # --- extract metadata -------------------------------------------
        metadata: dict = {
            "shape_original": list(ds.shape),
            "crs": None,
            "bounds": None,
            "geotransform": None,
        }

        # Try standard NISAR / CF metadata locations
        for gt_key in ("geotransform", "GeoTransform", "geoTransform"):
            if gt_key in ds.attrs:
                metadata["geotransform"] = list(ds.attrs[gt_key])
                break
            # Check parent group
            parent = ds.parent
            if parent is not None and gt_key in parent.attrs:
                metadata["geotransform"] = list(parent.attrs[gt_key])
                break

        for crs_key in ("crs", "spatial_ref", "projection"):
            if crs_key in ds.attrs:
                metadata["crs"] = str(ds.attrs[crs_key])
                break
            parent = ds.parent
            if parent is not None and crs_key in parent.attrs:
                metadata["crs"] = str(parent.attrs[crs_key])
                break

        # Derive bounds from geotransform if available (ensuring min/max orientation)
        gt = metadata["geotransform"]
        if gt is not None and len(gt) >= 6:
            ox, px, _, oy, _, py = gt[:6]
            rows, cols = ds.shape[-2], ds.shape[-1]
            x_min = min(float(ox), float(ox + cols * px))
            x_max = max(float(ox), float(ox + cols * px))
            y_min = min(float(oy), float(oy + rows * py))
            y_max = max(float(oy), float(oy + rows * py))
            metadata["bounds"] = {
                "west": x_min,
                "south": y_min,
                "east": x_max,
                "north": y_max,
            }

    return arr, metadata


def inspect_h5_metadata(file_path: str) -> dict:
    """
    List available datasets, their shapes, and any CRS / bounds metadata.
    """
    info: dict = {"file": file_path, "datasets": [], "crs": None, "bounds": None}

    with h5py.File(file_path, "r") as f:
        def _visitor(name: str, obj: h5py.Dataset | h5py.Group):
            if isinstance(obj, h5py.Dataset):
                info["datasets"].append({
                    "path": name,
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                })
            # Scan for CRS / bounds at any group level
            for key in ("crs", "spatial_ref", "projection"):
                if key in getattr(obj, "attrs", {}):
                    info["crs"] = str(obj.attrs[key])
            for key in ("geotransform", "GeoTransform"):
                if key in getattr(obj, "attrs", {}):
                    gt = list(obj.attrs[key])
                    info["geotransform"] = gt

        f.visititems(_visitor)

    return info


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def compute_change_detection(
    pre_array: np.ndarray,
    post_array: np.ndarray,
    nodata_value: Optional[float] = None,
    mode: str = "observed",
    threshold_db: float = CHANGE_THRESHOLD_DB,
) -> dict:
    """
    Compute SAR change-detection between two co-registered rasters.

    Parameters
    ----------
    pre_array, post_array : np.ndarray
        Linear-power backscatter arrays (same shape).
    nodata_value : float | None
        If set, pixels equal to this value in *either* image are excluded.
    mode : str
        "observed" or "synthetic" — propagated into the response so the
        frontend never silently displays synthetic data as real.
    threshold_db : float
        Absolute dB change threshold (|diff_db| > threshold_db) to classify
        a pixel as significantly changed. Defaults to 3.0 dB.

    Returns
    -------
    dict with keys:
        mean_db, changed_area_pct, image_base64, valid_pixel_count,
        total_pixel_count, mode, threshold_db
    """
    if pre_array.shape != post_array.shape:
        raise ValueError(
            f"Shape mismatch: pre {pre_array.shape} vs post {post_array.shape}. "
            "Arrays must be co-registered and the same size."
        )

    pre = pre_array.astype(np.float64)
    post = post_array.astype(np.float64)

    # --- build validity mask ------------------------------------------------
    valid = np.isfinite(pre) & np.isfinite(post)
    if nodata_value is not None:
        valid &= (pre != nodata_value) & (post != nodata_value)
    # Also exclude true zeros (not the same as EPS-clipped low values)
    valid &= (pre > 0) & (post > 0)

    total_pixels = int(pre.size)
    valid_pixels = int(np.count_nonzero(valid))

    # Clip to EPS *after* masking so nodata zeros stay masked
    pre_safe = np.clip(pre, EPS, None)
    post_safe = np.clip(post, EPS, None)

    # --- dB difference ------------------------------------------------------
    diff_db = np.zeros_like(pre, dtype=np.float64)
    diff_db[valid] = 10.0 * np.log10(post_safe[valid] / pre_safe[valid])

    # Metrics over valid pixels only
    if valid_pixels > 0:
        mean_db = float(np.mean(diff_db[valid]))
        changed = np.abs(diff_db[valid]) > threshold_db
        changed_pct = float(np.count_nonzero(changed) / valid_pixels * 100.0)
    else:
        mean_db = 0.0
        changed_pct = 0.0

    # --- render north-up PNG ------------------------------------------------
    # matplotlib imshow with origin="upper" renders row-0 at the top, which is
    # the north edge of a standard raster whose geotransform has a negative
    # y-pixel-size.  That is the correct orientation for Leaflet overlays.
    render = np.where(valid, diff_db, np.nan)

    fig, ax = plt.subplots(figsize=(6, 6), dpi=100)
    im = ax.imshow(
        render,
        cmap="RdBu",
        vmin=-10,
        vmax=10,
        origin="upper",
        interpolation="nearest",
    )
    ax.set_axis_off()
    fig.colorbar(im, ax=ax, label="ΔdB", shrink=0.8)
    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", transparent=True)
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    image_base64 = f"data:image/png;base64,{b64}"

    return {
        "mean_db": round(mean_db, 4),
        "changed_area_pct": round(changed_pct, 2),
        "image_base64": image_base64,
        "valid_pixel_count": valid_pixels,
        "total_pixel_count": total_pixels,
        "threshold_db": threshold_db,
        "mode": mode,
    }
