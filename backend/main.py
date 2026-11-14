"""
Dancing with the SARs — FastAPI backend (Stage 1).

Endpoints
---------
GET  /api/health   → {"status": "ok"}
GET  /api/search   → GeoJSON from ASF DAAC
POST /api/analyze  → SAR change-detection result + Leaflet overlay bounds
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from asf_client import search_nisar
from sar_processor import (
    compute_change_detection,
    generate_synthetic_sar_pair,
    load_h5_slice,
)

# Load .env if present
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(
    title="Dancing with the SARs",
    version="0.1.0",
    description="NASA Space Apps — SAR change-detection API (Stage 1)",
)

# Allow all origins so the React / Leaflet frontend can call us from any host
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Pydantic models ------------------------------------------------

class AnalyzeRequest(BaseModel):
    pre_file: Optional[str] = None
    post_file: Optional[str] = None
    polarization: Optional[str] = "HHHH"
    threshold_db: Optional[float] = 3.0
    use_synthetic: Optional[bool] = False


# ---------- Endpoints -------------------------------------------------------

@app.get("/api/health")
async def health():
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    has_token = bool(token and token != "your_earthdata_token_here")
    has_topo = bool(os.environ.get("OPENTOPOGRAPHY_API_KEY", "").strip())
    return {
        "status": "ok",
        "earthdata_token_configured": has_token,
        "opentopography_key_configured": has_topo,
    }


@app.get("/api/search")
async def search(
    bbox: str = Query(
        default="-122.5,37.5,-122.0,38.0",
        description="west,south,east,north (EPSG:4326)",
    ),
    start: str = Query(
        default="2024-01-01T00:00:00Z",
        description="ISO-8601 start date",
    ),
    end: str = Query(
        default="2024-12-31T23:59:59Z",
        description="ISO-8601 end date",
    ),
    max_results: int = Query(default=10, ge=1, le=100),
    platform: str = Query(
        default="NISAR,Sentinel-1",
        description="Platforms to query: NISAR,Sentinel-1, NISAR, Sentinel-1, or UAVSAR",
    ),
):
    """Proxy search to ASF DAAC and return GeoJSON."""
    geojson = search_nisar(
        bbox=bbox,
        start_date=start,
        end_date=end,
        max_results=max_results,
        platform=platform,
    )
    return geojson


@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest):
    """
    Run SAR change-detection.

    - If use_synthetic=True is explicitly requested: generates synthetic demo data.
    - If pre_file/post_file are provided (or "sample_pre"/"sample_post"): loads
      HDF5 observations.
    - Never silently substitutes synthetic data if real files fail or are missing.
    - The response always includes an explicit `mode` ("observed" | "synthetic").
    """
    sample_dir = Path(__file__).resolve().parent / "sample_data"
    sample_pre = sample_dir / "nisar_gcov_20240115_pre.h5"
    sample_post = sample_dir / "nisar_gcov_20240215_post.h5"

    pre_file = body.pre_file
    post_file = body.post_file

    # Map alias names to bundled sample dataset
    if pre_file == "sample_pre" or (pre_file is None and not body.use_synthetic and sample_pre.exists()):
        pre_file = str(sample_pre)
    if post_file == "sample_post" or (post_file is None and not body.use_synthetic and sample_post.exists()):
        post_file = str(sample_post)

    threshold = body.threshold_db if body.threshold_db is not None else 3.0
    pol = body.polarization or "HHHH"
    dataset_path = f"science/LSAR/GCOV/grids/frequencyA/{pol}"

    bounds = None
    provenance: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "polarization": pol,
        "threshold_db": threshold,
    }

    if body.use_synthetic:
        # Explicit synthetic mode requested
        pre_arr, post_arr = generate_synthetic_sar_pair()
        mode = "synthetic"
        bounds = {
            "west": -122.5,
            "south": 37.5,
            "east": -122.0,
            "north": 38.0,
        }
        provenance["shape"] = [512, 512]
        provenance["pre_file"] = "synthetic_field_A"
        provenance["post_file"] = "synthetic_field_B"
        provenance["note"] = (
            "Synthetic simulation requested. Results are purely demonstrative "
            "and MUST NOT be labeled as real satellite observations."
        )
    else:
        # Real observed data path
        if not pre_file or not post_file:
            raise HTTPException(
                status_code=400,
                detail="pre_file and post_file must be provided. For synthetic demo, pass use_synthetic=true.",
            )

        p_pre = Path(pre_file)
        p_post = Path(post_file)

        if not p_pre.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Pre-event HDF5 file not found: {pre_file}. Will NOT silently substitute synthetic data.",
            )
        if not p_post.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Post-event HDF5 file not found: {post_file}. Will NOT silently substitute synthetic data.",
            )

        try:
            pre_arr, pre_meta = load_h5_slice(str(p_pre), dataset_path)
            post_arr, post_meta = load_h5_slice(str(p_post), dataset_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Error reading HDF5 files: {exc}",
            )

        # Shape compatibility check and crop to intersection
        if pre_arr.shape != post_arr.shape:
            min_rows = min(pre_arr.shape[0], post_arr.shape[0])
            min_cols = min(pre_arr.shape[1], post_arr.shape[1])
            pre_arr = pre_arr[:min_rows, :min_cols]
            post_arr = post_arr[:min_rows, :min_cols]

        bounds = pre_meta.get("bounds") or post_meta.get("bounds")
        mode = "observed"
        provenance["pre_file"] = str(p_pre.name)
        provenance["post_file"] = str(p_post.name)
        provenance["shape"] = list(pre_arr.shape)
        provenance["crs"] = pre_meta.get("crs") or post_meta.get("crs")

    # --- change detection ---
    result = compute_change_detection(
        pre_arr,
        post_arr,
        nodata_value=0.0,
        mode=mode,
        threshold_db=threshold,
    )

    # Leaflet overlay bounds (SW, NE corners)
    leaflet_bounds = None
    if bounds:
        leaflet_bounds = [
            [bounds["south"], bounds["west"]],
            [bounds["north"], bounds["east"]],
        ]

    return {
        **result,
        "bounds": bounds,
        "leaflet_bounds": leaflet_bounds,
        "provenance": provenance,
    }


# Mount frontend static files so opening http://localhost:8000 loads the explorer directly
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.is_dir():
    from starlette.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

