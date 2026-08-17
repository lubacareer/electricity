"""Portable normalized-project bundle serialization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from smd_twin_lab.models import (
    BoardGeometry,
    Capability,
    CapabilityStatus,
    Component,
    ComponentSide,
    Diagnostic,
    DiagnosticSeverity,
    ImportedProject,
    Net,
    PinRef,
    ProjectCapabilities,
)

BUNDLE_FILENAME = "project.json"
BUNDLE_FORMAT = "smd-twin-lab.imported-project"
BUNDLE_VERSION = 1


def _unavailable_capabilities(detail: str) -> ProjectCapabilities:
    unavailable = Capability(CapabilityStatus.UNAVAILABLE, detail)
    return ProjectCapabilities(unavailable, unavailable, unavailable, unavailable)


def _invalid_project(path: Path, code: str, message: str) -> ImportedProject:
    identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    invalid = Capability(CapabilityStatus.INVALID, message)
    return ImportedProject(
        schema_version=1,
        project_id=identity,
        name=path.stem or "Invalid bundle",
        source_dir=str(path.parent.resolve()),
        cache_dir=str(path.parent.resolve()),
        source_hashes={},
        kicad_version=None,
        variant="default",
        components=(),
        nets=(),
        geometry=BoardGeometry(),
        capabilities=ProjectCapabilities(invalid, invalid, invalid, invalid),
        diagnostics=(
            Diagnostic(DiagnosticSeverity.ERROR, code, message, path=str(path.resolve())),
        ),
    )


def _bundle_file(path: Path) -> Path | None:
    if path.is_file():
        return path if path.suffix.casefold() in {".json", ".smdtwin"} else None
    if not path.is_dir():
        return None
    preferred = [
        path / BUNDLE_FILENAME,
        path / "bundle.json",
        path / "normalized_project.json",
        path / "normalized-project.json",
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    for candidate in sorted(path.glob("*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and (
            payload.get("bundle_format") == BUNDLE_FORMAT
            or "project_id" in payload
            or isinstance(payload.get("project"), dict)
        ):
            return candidate
    return None


def is_bundle_path(path: Path) -> bool:
    """Return whether *path* identifies a normalized bundle, without loading tools."""

    return _bundle_file(Path(path)) is not None


def _resolve_asset(value: Any, bundle_dir: Path) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = bundle_dir / candidate
    return str(candidate.resolve())


def _status(value: Any) -> CapabilityStatus:
    try:
        return CapabilityStatus(str(value))
    except ValueError:
        return CapabilityStatus.INVALID


def _capability(value: Any, fallback: str) -> Capability:
    if not isinstance(value, dict):
        return Capability(CapabilityStatus.UNAVAILABLE, fallback)
    return Capability(
        _status(value.get("status", "unavailable")), str(value.get("detail", fallback))
    )


def _side(value: Any) -> ComponentSide:
    try:
        return ComponentSide(str(value))
    except ValueError:
        return ComponentSide.UNKNOWN


def _severity(value: Any) -> DiagnosticSeverity:
    try:
        return DiagnosticSeverity(str(value))
    except ValueError:
        return DiagnosticSeverity.ERROR


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _decode_project(payload: dict[str, Any], bundle_path: Path) -> ImportedProject:
    bundle_dir = bundle_path.parent.resolve()
    diagnostics: list[Diagnostic] = []

    components: list[Component] = []
    for index, raw in enumerate(payload.get("components", [])):
        if not isinstance(raw, dict) or not str(raw.get("reference", "")).strip():
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "BUNDLE_COMPONENT_INVALID",
                    f"Component entry {index} has no reference designator.",
                    path=str(bundle_path),
                )
            )
            continue
        try:
            components.append(
                Component(
                    reference=str(raw["reference"]),
                    value=str(raw.get("value", "")),
                    footprint=str(raw.get("footprint", "")),
                    x_mm=_float_or_none(raw.get("x_mm")),
                    y_mm=_float_or_none(raw.get("y_mm")),
                    rotation_deg=float(raw.get("rotation_deg", 0.0)),
                    side=_side(raw.get("side", "unknown")),
                    in_bom=bool(raw.get("in_bom", False)),
                    on_board=bool(raw.get("on_board", False)),
                    is_smd=bool(raw.get("is_smd", False)),
                    nets=tuple(str(item) for item in raw.get("nets", [])),
                    fields={str(key): str(value) for key, value in raw.get("fields", {}).items()},
                )
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "BUNDLE_COMPONENT_INVALID",
                    f"Component {raw.get('reference', index)!r} is invalid: {exc}",
                    path=str(bundle_path),
                )
            )

    nets: list[Net] = []
    for index, raw in enumerate(payload.get("nets", [])):
        if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "BUNDLE_NET_INVALID",
                    f"Net entry {index} has no name.",
                    path=str(bundle_path),
                )
            )
            continue
        pins = tuple(
            PinRef(str(pin.get("reference", "")), str(pin.get("pin", "")))
            for pin in raw.get("pins", [])
            if isinstance(pin, dict) and pin.get("reference") and pin.get("pin") is not None
        )
        nets.append(Net(str(raw["name"]), pins))

    geometry_raw = payload.get("geometry", {})
    if not isinstance(geometry_raw, dict):
        geometry_raw = {}
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_GEOMETRY_INVALID",
                "The geometry entry must be an object.",
                path=str(bundle_path),
            )
        )
    try:
        geometry = BoardGeometry(
            outline_path=_resolve_asset(geometry_raw.get("outline_path"), bundle_dir),
            top_preview_path=_resolve_asset(geometry_raw.get("top_preview_path"), bundle_dir),
            bottom_preview_path=_resolve_asset(geometry_raw.get("bottom_preview_path"), bundle_dir),
            min_x_mm=float(geometry_raw.get("min_x_mm", 0.0)),
            min_y_mm=float(geometry_raw.get("min_y_mm", 0.0)),
            max_x_mm=float(geometry_raw.get("max_x_mm", 100.0)),
            max_y_mm=float(geometry_raw.get("max_y_mm", 60.0)),
        )
    except (TypeError, ValueError) as exc:
        geometry = BoardGeometry()
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_GEOMETRY_INVALID",
                f"The geometry bounds are invalid: {exc}",
                path=str(bundle_path),
            )
        )

    capabilities_raw = payload.get("capabilities", {})
    if not isinstance(capabilities_raw, dict):
        capabilities_raw = {}
    geometry_capability = _capability(
        capabilities_raw.get("geometry"), "No geometry capability recorded in bundle."
    )
    circuit_capability = _capability(
        capabilities_raw.get("circuit"), "No circuit capability recorded in bundle."
    )
    firmware_capability = _capability(
        capabilities_raw.get("firmware"), "No firmware capability recorded in bundle."
    )
    hardware_capability = _capability(
        capabilities_raw.get("hardware"), "No hardware capability recorded in bundle."
    )

    listed_geometry = [
        ("outline", geometry.outline_path),
        ("top preview", geometry.top_preview_path),
        ("bottom preview", geometry.bottom_preview_path),
    ]
    missing_geometry = [
        label for label, path in listed_geometry if path and not Path(path).is_file()
    ]
    if missing_geometry:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_ASSET_MISSING",
                "Bundle is missing geometry assets: " + ", ".join(missing_geometry) + ".",
                path=str(bundle_path),
            )
        )
        geometry_capability = Capability(
            CapabilityStatus.INVALID,
            "One or more geometry files named by the bundle are missing.",
        )

    spice_path = _resolve_asset(payload.get("spice_netlist_path"), bundle_dir)
    if spice_path and not Path(spice_path).is_file():
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_SPICE_MISSING",
                "The SPICE netlist named by the bundle is missing.",
                path=spice_path,
            )
        )
        circuit_capability = Capability(
            CapabilityStatus.INVALID, "The bundled SPICE netlist is missing."
        )

    manifest_path = _resolve_asset(payload.get("twin_manifest_path"), bundle_dir)
    if manifest_path and not Path(manifest_path).is_file():
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_MANIFEST_MISSING",
                "The twin manifest named by the bundle is missing.",
                path=manifest_path,
            )
        )
        if firmware_capability.status == CapabilityStatus.AVAILABLE:
            firmware_capability = Capability(
                CapabilityStatus.INVALID, "The bundled twin manifest is missing."
            )

    for raw in payload.get("diagnostics", []):
        if not isinstance(raw, dict):
            continue
        diagnostics.append(
            Diagnostic(
                severity=_severity(raw.get("severity", "error")),
                code=str(raw.get("code", "BUNDLE_DIAGNOSTIC")),
                message=str(raw.get("message", "")),
                reference=str(raw["reference"]) if raw.get("reference") is not None else None,
                path=str(raw["path"]) if raw.get("path") is not None else None,
            )
        )

    schema_version = int(payload.get("schema_version", 1))
    if schema_version != 1:
        detail = f"Bundle schema {schema_version} is unsupported; this build supports schema 1."
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "BUNDLE_SCHEMA_UNSUPPORTED",
                detail,
                path=str(bundle_path),
            )
        )
        invalid = Capability(CapabilityStatus.INVALID, detail)
        geometry_capability = circuit_capability = firmware_capability = hardware_capability = (
            invalid
        )

    source_dir = Path(str(payload.get("source_dir", bundle_dir))).expanduser()
    if not source_dir.is_absolute():
        source_dir = bundle_dir / source_dir

    return ImportedProject(
        schema_version=schema_version,
        project_id=str(
            payload.get("project_id") or hashlib.sha256(str(bundle_path).encode()).hexdigest()[:16]
        ),
        name=str(payload.get("name") or bundle_path.parent.name),
        source_dir=str(source_dir.resolve()),
        cache_dir=str(bundle_dir),
        source_hashes={
            str(key): str(value) for key, value in payload.get("source_hashes", {}).items()
        },
        kicad_version=(
            str(payload["kicad_version"]) if payload.get("kicad_version") is not None else None
        ),
        variant=str(payload.get("variant", "default")),
        components=tuple(components),
        nets=tuple(nets),
        geometry=geometry,
        capabilities=ProjectCapabilities(
            geometry_capability,
            circuit_capability,
            firmware_capability,
            hardware_capability,
        ),
        diagnostics=tuple(diagnostics),
        spice_netlist_path=spice_path,
        twin_manifest_path=manifest_path,
    )


def load_bundle(path: Path) -> ImportedProject:
    """Load a portable normalized bundle without discovering or invoking KiCad."""

    requested = Path(path).expanduser()
    bundle_path = _bundle_file(requested)
    if bundle_path is None:
        return _invalid_project(
            requested,
            "BUNDLE_NOT_FOUND",
            "No normalized project JSON was found. Select project.json or its "
            "containing bundle folder.",
        )
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _invalid_project(
            bundle_path,
            "BUNDLE_JSON_INVALID",
            f"The normalized project JSON could not be read: {exc}",
        )
    if not isinstance(raw, dict):
        return _invalid_project(
            bundle_path,
            "BUNDLE_ROOT_INVALID",
            "The normalized project JSON root must be an object.",
        )
    payload = raw.get("project", raw)
    if not isinstance(payload, dict):
        return _invalid_project(
            bundle_path,
            "BUNDLE_PROJECT_INVALID",
            "The bundle's project entry must be an object.",
        )
    try:
        return _decode_project(payload, bundle_path)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid_project(
            bundle_path,
            "BUNDLE_PROJECT_INVALID",
            f"The normalized project data is invalid: {exc}",
        )


def _portable_path(value: str | None, bundle_dir: Path) -> str | None:
    if value is None:
        return None
    path = Path(value)
    try:
        return path.resolve().relative_to(bundle_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_bundle(
    project: ImportedProject,
    path: Path,
    *,
    artifacts: dict[str, str] | None = None,
) -> None:
    """Atomically write a schema-1 normalized project JSON."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(project)
    payload["cache_dir"] = "."
    payload["geometry"]["outline_path"] = _portable_path(
        project.geometry.outline_path, destination.parent
    )
    payload["geometry"]["top_preview_path"] = _portable_path(
        project.geometry.top_preview_path, destination.parent
    )
    payload["geometry"]["bottom_preview_path"] = _portable_path(
        project.geometry.bottom_preview_path, destination.parent
    )
    payload["spice_netlist_path"] = _portable_path(project.spice_netlist_path, destination.parent)
    payload["twin_manifest_path"] = _portable_path(project.twin_manifest_path, destination.parent)
    payload["bundle_format"] = BUNDLE_FORMAT
    payload["bundle_version"] = BUNDLE_VERSION
    payload["coordinate_system"] = {
        "units": "mm",
        "view": "top",
        "x_axis": "right",
        "y_axis": "up",
        "rotation": "counter_clockwise_degrees",
        "bottom_coordinates_mirrored": False,
    }
    if artifacts:
        payload["artifacts"] = {
            key: _portable_path(value, destination.parent) for key, value in artifacts.items()
        }

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
