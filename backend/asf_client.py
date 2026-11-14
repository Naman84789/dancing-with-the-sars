"""
ASF DAAC client for searching NISAR / Sentinel-1 SAR granules.
"""

import os
from pathlib import Path
import requests
from fastapi import HTTPException

# Load .env if present
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ASF_SEARCH_URL = "https://api.daac.asf.alaska.edu/services/search/param"


def get_earthdata_headers() -> dict:
    """Return Bearer auth header if EARTHDATA_TOKEN is set, else empty dict."""
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if token and token != "your_earthdata_token_here":
        return {"Authorization": f"Bearer {token}"}
    return {}


def search_nisar(
    bbox: str,
    start_date: str,
    end_date: str,
    max_results: int = 10,
    platform: str = "NISAR,Sentinel-1",
) -> dict:
    """
    Search ASF DAAC for SAR granules across NASA & partner missions.

    Parameters
    ----------
    bbox : str
        Bounding box as "west,south,east,north" (EPSG:4326).
    start_date : str
        ISO-8601 start date, e.g. "2024-01-01T00:00:00Z".
    end_date : str
        ISO-8601 end date.
    max_results : int
        Maximum number of results to return.
    platform : str
        Platform filter: "NISAR,Sentinel-1", "NISAR", "Sentinel-1", or "UAVSAR".

    Returns
    -------
    dict
        GeoJSON FeatureCollection of matching granules.

    Raises
    ------
    HTTPException(502)
        When the upstream ASF API call fails.
    """
    params = {
        "platform": platform,
        "bbox": bbox,
        "start": start_date,
        "end": end_date,
        "maxResults": max_results,
        "output": "geojson",
    }

    # Set appropriate processing levels per platform
    if "NISAR" in platform and "Sentinel-1" in platform:
        params["processingLevel"] = "GCOV,GRD_HD,GSLC,RSLC"
    elif platform == "NISAR":
        params["processingLevel"] = "GCOV,GSLC,RSLC"
    elif platform == "Sentinel-1":
        params["processingLevel"] = "GRD_HD"

    headers = get_earthdata_headers()

    try:
        resp = requests.get(ASF_SEARCH_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=502,
            detail="ASF DAAC search timed out after 30 s.",
        )
    except requests.exceptions.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ASF DAAC search failed: {exc}",
        )
