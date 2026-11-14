"""
API endpoint validation tests for Dancing with the SARs (FastAPI).

Tests:
1. GET /api/health
2. GET /api/search (ASF DAAC proxy)
3. POST /api/analyze (Synthetic mode explicitly requested)
4. POST /api/analyze (Real observed HDF5 dataset)
5. POST /api/analyze (Error handling: missing files must NOT silently fall back to synthetic)
"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"
SAMPLE_PRE = SAMPLE_DIR / "nisar_gcov_20240115_pre.h5"
SAMPLE_POST = SAMPLE_DIR / "nisar_gcov_20240215_post.h5"


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_search_asf_daac():
    """Verify ASF search proxy responds with GeoJSON FeatureCollection."""
    response = client.get(
        "/api/search",
        params={
            "bbox": "-122.5,37.5,-122.0,38.0",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-02-01T00:00:00Z",
            "max_results": 3,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("type") == "FeatureCollection"
    assert "features" in data
    # Features should have properties
    if len(data["features"]) > 0:
        feat = data["features"][0]
        assert "properties" in feat
        assert "geometry" in feat


def test_analyze_synthetic_explicit():
    """Verify explicitly requested synthetic mode returns mode='synthetic'."""
    response = client.post("/api/analyze", json={"use_synthetic": True})
    assert response.status_code == 200
    data = response.json()

    assert data["mode"] == "synthetic"
    assert "image_base64" in data
    assert data["image_base64"].startswith("data:image/png;base64,")
    assert "mean_db" in data
    assert "changed_area_pct" in data
    assert data["leaflet_bounds"] is not None
    assert "provenance" in data
    assert "synthetic" in data["provenance"]["note"].lower()


def test_analyze_real_sample_pair():
    """Verify observed analysis on reproducible NISAR GCOV sample pair."""
    if not (SAMPLE_PRE.exists() and SAMPLE_POST.exists()):
        pytest.skip("Sample files not present")

    response = client.post(
        "/api/analyze",
        json={
            "pre_file": str(SAMPLE_PRE),
            "post_file": str(SAMPLE_POST),
            "polarization": "HHHH",
            "threshold_db": 3.0,
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["mode"] == "observed"
    assert "image_base64" in data
    assert data["mean_db"] != 0.0
    assert data["changed_area_pct"] > 0.0
    assert data["valid_pixel_count"] > 0
    assert data["total_pixel_count"] > data["valid_pixel_count"]  # Nodata masked
    assert data["threshold_db"] == 3.0

    # Leaflet bounds must be valid [[south, west], [north, east]]
    lb = data["leaflet_bounds"]
    assert len(lb) == 2
    south, west = lb[0]
    north, east = lb[1]
    assert south < north
    assert west < east

    # Provenance
    prov = data["provenance"]
    assert "nisar_gcov_20240115_pre.h5" in prov["pre_file"]
    assert "nisar_gcov_20240215_post.h5" in prov["post_file"]


def test_analyze_missing_file_rejected_without_silent_synthetic():
    """If a real file is requested but missing, fail with 404 instead of silent synthetic."""
    response = client.post(
        "/api/analyze",
        json={
            "pre_file": "nonexistent_pre_file.h5",
            "post_file": "nonexistent_post_file.h5",
            "use_synthetic": False,
        },
    )
    assert response.status_code == 404
    detail = response.json().get("detail", "")
    assert "not found" in detail.lower()
