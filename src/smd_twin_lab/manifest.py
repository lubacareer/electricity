"""Validation for the human-authored companion ``twin.yaml`` file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """Raised when a twin manifest is ambiguous or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class TwinManifest:
    schema_version: int
    project_id: str
    name: str
    source_path: Path
    faults: dict[str, dict[str, Any]]
    scenarios: dict[str, dict[str, Any]]
    lessons: dict[str, str]
    raw: dict[str, Any]

    def resolve(self, value: str | None) -> Path | None:
        if not value:
            return None
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        return (self.source_path.parent / candidate).resolve()


def _indexed(items: Any, section: str) -> dict[str, dict[str, Any]]:
    if items is None:
        return {}
    if not isinstance(items, list):
        raise ManifestError(f"{section} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ManifestError(f"{section}[{index}] must be a mapping")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ManifestError(f"{section}[{index}] requires a non-empty id")
        if item_id in result:
            raise ManifestError(f"duplicate {section} id: {item_id}")
        result[item_id] = item
    return result


def load_twin_manifest(path: Path) -> TwinManifest:
    path = path.resolve()
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read twin manifest {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ManifestError("twin manifest root must be a mapping")

    schema_version = loaded.get("schema_version")
    if schema_version != 1:
        raise ManifestError(f"unsupported twin manifest schema: {schema_version!r}")
    project_id = loaded.get("project_id")
    name = loaded.get("name")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ManifestError("project_id must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("name must be a non-empty string")

    coordinate_system = loaded.get("coordinate_system", {})
    expected_coordinates = {
        "unit": "mm",
        "viewpoint": "top",
        "x_direction": "right",
        "y_direction": "up",
        "rotation": "counter_clockwise_degrees",
    }
    if not isinstance(coordinate_system, dict):
        raise ManifestError("coordinate_system must be a mapping")
    for key, expected in expected_coordinates.items():
        if coordinate_system.get(key) != expected:
            raise ManifestError(f"coordinate_system.{key} must be {expected!r}")

    faults = _indexed(loaded.get("faults"), "faults")
    scenarios = _indexed(loaded.get("scenarios"), "scenarios")
    for scenario_id, scenario in scenarios.items():
        fault_id = scenario.get("fault", "none")
        if fault_id != "none" and fault_id not in faults:
            raise ManifestError(f"scenario {scenario_id!r} refers to unknown fault {fault_id!r}")

    lessons = loaded.get("lessons", {})
    if not isinstance(lessons, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in lessons.items()
    ):
        raise ManifestError("lessons must map context keys to lesson identifiers")

    return TwinManifest(
        schema_version=1,
        project_id=project_id,
        name=name,
        source_path=path,
        faults=faults,
        scenarios=scenarios,
        lessons=lessons,
        raw=loaded,
    )
