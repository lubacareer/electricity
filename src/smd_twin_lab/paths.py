"""Application-owned path helpers."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

from platformdirs import user_cache_path, user_data_path


def repository_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def sample_bundle_path() -> Path:
    override = os.environ.get("SMD_TWIN_SAMPLE_BUNDLE")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        root = repository_root()
        repository_sample = (
            root / "examples" / "sensor_status_board" / "bundle" / "project.json"
            if root is not None
            else None
        )
        if repository_sample is not None and repository_sample.is_file():
            candidate = repository_sample
        else:
            candidate = Path(str(files("smd_twin_lab") / "resources" / "sample" / "project.json"))
    if not candidate.is_file():
        raise FileNotFoundError(f"Sample bundle not found: {candidate}")
    return candidate


def cache_root() -> Path:
    path = Path(user_cache_path("SmdTwinLab", "SmdTwinLab"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_root() -> Path:
    path = Path(user_data_path("SmdTwinLab", "SmdTwinLab")) / "run-reports"
    path.mkdir(parents=True, exist_ok=True)
    return path
