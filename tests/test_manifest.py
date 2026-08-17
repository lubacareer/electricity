from pathlib import Path

import pytest

from smd_twin_lab.manifest import ManifestError, load_twin_manifest
from smd_twin_lab.paths import repository_root, sample_bundle_path


def test_reference_manifest_is_valid() -> None:
    root = repository_root()
    assert root is not None
    manifest = load_twin_manifest(root / "examples/sensor_status_board/twin.yaml")

    assert manifest.project_id == "sensor-status-v1"
    assert "thermistor_open" in manifest.faults
    assert manifest.scenarios["thermistor_open"]["fault"] == "thermistor_open"


def test_manifest_rejects_unknown_fault(tmp_path: Path) -> None:
    path = tmp_path / "twin.yaml"
    path.write_text(
        """schema_version: 1
project_id: test
name: Test
coordinate_system:
  unit: mm
  viewpoint: top
  x_direction: right
  y_direction: up
  rotation: counter_clockwise_degrees
scenarios:
  - id: broken
    fault: missing
""",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="unknown fault"):
        load_twin_manifest(path)


def test_sample_bundle_path_exists() -> None:
    assert sample_bundle_path().is_file()
