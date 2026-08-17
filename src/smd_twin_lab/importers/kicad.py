"""Read-only KiCad 10 project importer.

Only documented ``kicad-cli`` exports are consumed.  Every command writes into
an isolated temporary directory and the selected KiCad source files are hashed
before and after export.  The normalized cache is therefore disposable and no
generated file needs to live beside an authorized source project.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path

from smd_twin_lab.manifest import ManifestError, load_twin_manifest
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
    ProjectCapabilities,
)
from smd_twin_lab.tooling import discover_tools

from .bundle import BUNDLE_FILENAME, is_bundle_path, load_bundle, write_bundle
from .parsers import (
    ArtifactParseError,
    BomPart,
    Placement,
    dxf_bounds,
    natural_key,
    parse_bom_csv,
    parse_logical_netlist,
    parse_placement_csv,
    validate_svg,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class _SourceFiles:
    project: Path
    schematic: Path | None
    board: Path | None
    manifest: Path | None

    def existing(self) -> tuple[Path, ...]:
        return tuple(
            item
            for item in (self.project, self.schematic, self.board, self.manifest)
            if item is not None and item.is_file()
        )


@dataclass(frozen=True, slots=True)
class _ManifestInfo:
    data: Mapping[str, Any]
    valid: bool
    missing_models: tuple[str, ...]
    firmware_status: Capability
    hardware_status: Capability


class KiCadProjectImporter:
    """Import KiCad projects or load already-normalized project bundles."""

    def __init__(
        self,
        *,
        kicad_cli: str | Path | None = None,
        cache_root: str | Path | None = None,
        timeout_s: float = 120.0,
        runner: Runner | None = None,
    ) -> None:
        self._explicit_kicad_cli = Path(kicad_cli) if kicad_cli is not None else None
        self.cache_root = (
            Path(cache_root)
            if cache_root is not None
            else Path(user_cache_path("smd-twin-lab", appauthor=False)) / "imports"
        )
        self.timeout_s = timeout_s
        self._runner: Runner = runner or subprocess.run

    def import_project(self, project_path: Path, variant: str = "default") -> ImportedProject:
        """Import a ``.kicad_pro`` file or load a normalized JSON bundle.

        Bundle loading is checked first and intentionally does not perform tool
        discovery, which lets packaged examples open on clean Windows systems.
        """

        selected = Path(project_path).expanduser()
        if is_bundle_path(selected):
            return load_bundle(selected)
        if selected.suffix.casefold() != ".kicad_pro":
            if selected.is_dir() or selected.suffix.casefold() in {".json", ".smdtwin"}:
                return load_bundle(selected)
            raise ValueError(
                "Select a KiCad .kicad_pro file, a normalized project.json file, "
                "or a bundle directory."
            )
        if not selected.is_file():
            raise FileNotFoundError(f"KiCad project does not exist: {selected}")
        if not variant.strip():
            raise ValueError("variant must not be empty")
        return self._import_kicad(selected.resolve(), variant.strip())

    def _import_kicad(self, project_path: Path, variant: str) -> ImportedProject:
        diagnostics: list[Diagnostic] = []
        sources = self._discover_sources(project_path, diagnostics)
        source_hashes_before = _hash_sources(sources.existing())
        manifest_info = self._read_manifest(sources.manifest, diagnostics)
        project_id = str(manifest_info.data.get("project_id") or _project_id(project_path))
        cache_dir = self._cache_directory(
            project_path.stem, project_id, variant, source_hashes_before
        )
        cached_bundle = cache_dir / BUNDLE_FILENAME
        stale_cache: ImportedProject | None = None
        if cached_bundle.is_file():
            cached = load_bundle(cached_bundle)
            cache_matches = (
                cached.source_hashes == source_hashes_before and cached.variant == variant
            )
            cache_is_complete = not any(
                diagnostic.code.startswith("BUNDLE_")
                and diagnostic.severity == DiagnosticSeverity.ERROR
                for diagnostic in cached.diagnostics
            )
            if cache_matches and cache_is_complete:
                return cached
            if cache_matches:
                stale_cache = cached

        cli = self._kicad_cli()
        if cli is None:
            if stale_cache is not None:
                return stale_cache
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "KICAD_CLI_MISSING",
                    "KiCad CLI was not found. Install KiCad 10 or set SMD_TWIN_KICAD_CLI; "
                    "normalized project.json bundles can still be opened without KiCad.",
                    path=str(project_path),
                )
            )
            return self._unavailable_source_project(
                sources,
                variant,
                project_id,
                source_hashes_before,
                manifest_info,
                diagnostics,
            )

        if sources.schematic is None and sources.board is None:
            return self._unavailable_source_project(
                sources,
                variant,
                project_id,
                source_hashes_before,
                manifest_info,
                diagnostics,
            )

        with tempfile.TemporaryDirectory(prefix="smd-twin-kicad-") as temporary:
            work_dir = Path(temporary)
            try:
                staged_sources = _stage_sources(sources, work_dir / "source")
            except OSError as exc:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "SOURCE_STAGING_FAILED",
                        f"KiCad inputs could not be copied to an isolated workspace: {exc}",
                        path=str(project_path.parent),
                    )
                )
                return self._unavailable_source_project(
                    sources,
                    variant,
                    project_id,
                    source_hashes_before,
                    manifest_info,
                    diagnostics,
                )
            kicad_version = self._read_version(cli, work_dir, diagnostics)
            exported: dict[str, Path] = {}

            if staged_sources.schematic is not None:
                schematic = staged_sources.schematic
                self._export(
                    cli,
                    "bom",
                    [
                        "sch",
                        "export",
                        "bom",
                        "--output",
                        str(work_dir / "bom.csv"),
                        *self._variant_args(variant),
                        str(schematic),
                    ],
                    work_dir / "bom.csv",
                    work_dir,
                    exported,
                    diagnostics,
                )
                self._export(
                    cli,
                    "logical_netlist",
                    [
                        "sch",
                        "export",
                        "netlist",
                        "--output",
                        str(work_dir / "logical.xml"),
                        "--format",
                        "kicadxml",
                        *self._variant_args(variant),
                        str(schematic),
                    ],
                    work_dir / "logical.xml",
                    work_dir,
                    exported,
                    diagnostics,
                )
                self._export(
                    cli,
                    "spice_netlist",
                    [
                        "sch",
                        "export",
                        "netlist",
                        "--output",
                        str(work_dir / "circuit.cir"),
                        "--format",
                        "spice",
                        *self._variant_args(variant),
                        str(schematic),
                    ],
                    work_dir / "circuit.cir",
                    work_dir,
                    exported,
                    diagnostics,
                )
                self._export(
                    cli,
                    "erc",
                    [
                        "sch",
                        "erc",
                        "--output",
                        str(work_dir / "erc.json"),
                        "--format",
                        "json",
                        "--units",
                        "mm",
                        str(schematic),
                    ],
                    work_dir / "erc.json",
                    work_dir,
                    exported,
                    diagnostics,
                )

            if staged_sources.board is not None:
                board = staged_sources.board
                self._export(
                    cli,
                    "placements",
                    [
                        "pcb",
                        "export",
                        "pos",
                        "--output",
                        str(work_dir / "placements.csv"),
                        "--format",
                        "csv",
                        "--units",
                        "mm",
                        "--side",
                        "both",
                        *self._variant_args(variant),
                        str(board),
                    ],
                    work_dir / "placements.csv",
                    work_dir,
                    exported,
                    diagnostics,
                )
                self._export(
                    cli,
                    "outline",
                    [
                        "pcb",
                        "export",
                        "dxf",
                        "--output",
                        str(work_dir / "outline.dxf"),
                        "--layers",
                        "Edge.Cuts",
                        "--output-units",
                        "mm",
                        "--mode-single",
                        *self._variant_args(variant),
                        str(board),
                    ],
                    work_dir / "outline.dxf",
                    work_dir,
                    exported,
                    diagnostics,
                )
                for side, layers in (
                    ("top", "F.Cu,F.Mask,F.Silkscreen,F.Fab,Edge.Cuts"),
                    ("bottom", "B.Cu,B.Mask,B.Silkscreen,B.Fab,Edge.Cuts"),
                ):
                    self._export(
                        cli,
                        f"{side}_preview",
                        [
                            "pcb",
                            "export",
                            "svg",
                            "--output",
                            str(work_dir / f"{side}.svg"),
                            "--layers",
                            layers,
                            "--mode-single",
                            "--fit-page-to-board",
                            "--exclude-drawing-sheet",
                            *self._variant_args(variant),
                            str(board),
                        ],
                        work_dir / f"{side}.svg",
                        work_dir,
                        exported,
                        diagnostics,
                    )
                self._export(
                    cli,
                    "drc",
                    [
                        "pcb",
                        "drc",
                        "--output",
                        str(work_dir / "drc.json"),
                        "--format",
                        "json",
                        "--units",
                        "mm",
                        str(board),
                    ],
                    work_dir / "drc.json",
                    work_dir,
                    exported,
                    diagnostics,
                )

            normalized = self._normalize(exported, diagnostics)
            source_hashes_after = _hash_sources(sources.existing())
            if source_hashes_after != source_hashes_before:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "SOURCE_CHANGED_DURING_IMPORT",
                        "A KiCad source file changed while it was being imported. No cache was "
                        "written; close active edits or save the project, then retry.",
                        path=str(project_path),
                    )
                )
                return self._changed_source_project(
                    sources,
                    variant,
                    project_id,
                    source_hashes_after,
                    kicad_version,
                    manifest_info,
                    diagnostics,
                )

            cache_dir.mkdir(parents=True, exist_ok=True)
            cached_artifacts = self._copy_artifacts(exported, cache_dir)
            cached_manifest = None
            if sources.manifest is not None:
                cached_manifest = cache_dir / "twin.yaml"
                _atomic_copy(sources.manifest, cached_manifest)
                cached_artifacts["twin_manifest"] = cached_manifest

            project = self._build_project(
                sources=sources,
                variant=variant,
                project_id=project_id,
                source_hashes=source_hashes_before,
                kicad_version=kicad_version,
                cache_dir=cache_dir,
                cached_artifacts=cached_artifacts,
                manifest_info=manifest_info,
                diagnostics=diagnostics,
                normalized=normalized,
            )
            write_bundle(
                project,
                cached_bundle,
                artifacts={key: str(path) for key, path in cached_artifacts.items()},
            )
            return project

    @staticmethod
    def _variant_args(variant: str) -> list[str]:
        return [] if variant.casefold() == "default" else ["--variant", variant]

    def _kicad_cli(self) -> Path | None:
        if self._explicit_kicad_cli is not None:
            return self._explicit_kicad_cli
        return discover_tools().kicad_cli

    def _discover_sources(self, project: Path, diagnostics: list[Diagnostic]) -> _SourceFiles:
        schematic = _discover_sibling(project, ".kicad_sch", "schematic", diagnostics)
        board = _discover_sibling(project, ".kicad_pcb", "board", diagnostics)
        manifest_candidates = [
            project.with_name("twin.yaml"),
            project.with_name(f"{project.stem}.twin.yaml"),
        ]
        manifest = next((path for path in manifest_candidates if path.is_file()), None)
        return _SourceFiles(project, schematic, board, manifest)

    def _read_manifest(self, path: Path | None, diagnostics: list[Diagnostic]) -> _ManifestInfo:
        unavailable_firmware = Capability(
            CapabilityStatus.UNAVAILABLE,
            "No firmware configuration is declared in twin.yaml.",
        )
        unavailable_hardware = Capability(
            CapabilityStatus.UNAVAILABLE,
            "No physical hardware target is configured.",
        )
        if path is None:
            return _ManifestInfo({}, True, (), unavailable_firmware, unavailable_hardware)
        try:
            manifest = load_twin_manifest(path)
        except ManifestError as exc:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "TWIN_MANIFEST_INVALID",
                    str(exc),
                    path=str(path),
                )
            )
            invalid = Capability(CapabilityStatus.INVALID, "twin.yaml is invalid.")
            return _ManifestInfo({}, False, (), invalid, invalid)
        raw = manifest.raw
        valid = True

        missing_models: list[str] = []
        simulation = raw.get("simulation", {})
        if simulation is not None and not isinstance(simulation, dict):
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "TWIN_SIMULATION_INVALID",
                    "The twin.yaml simulation entry must be a mapping.",
                    path=str(path),
                )
            )
            valid = False
            simulation = {}
        models = simulation.get("models", []) if isinstance(simulation, dict) else []
        if isinstance(models, (str, dict)):
            models = [models]
        if isinstance(models, list):
            for model in models:
                model_value = model.get("path") if isinstance(model, dict) else model
                if not isinstance(model_value, str) or not model_value.strip():
                    continue
                model_path = (path.parent / model_value).resolve()
                if not model_path.is_file():
                    missing_models.append(model_value)
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticSeverity.ERROR,
                            "SPICE_MODEL_MISSING",
                            f"SPICE model {model_value!r} named by twin.yaml was not found.",
                            path=str(model_path),
                        )
                    )

        firmware = raw.get("firmware")
        if firmware is None:
            firmware_status = unavailable_firmware
        elif not isinstance(firmware, dict):
            valid = False
            firmware_status = Capability(
                CapabilityStatus.INVALID, "The twin.yaml firmware entry must be a mapping."
            )
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "TWIN_FIRMWARE_INVALID",
                    firmware_status.detail,
                    path=str(path),
                )
            )
        else:
            image = next(
                (
                    firmware.get(key)
                    for key in ("elf", "executable", "image", "path")
                    if firmware.get(key) is not None
                ),
                None,
            )
            if image is not None and (
                not isinstance(image, str) or not (path.parent / image).resolve().is_file()
            ):
                valid = False
                image_path = (path.parent / str(image)).resolve()
                firmware_status = Capability(
                    CapabilityStatus.INVALID,
                    "The firmware image declared in twin.yaml is missing.",
                )
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "FIRMWARE_IMAGE_MISSING",
                        firmware_status.detail,
                        path=str(image_path),
                    )
                )
            elif firmware:
                firmware_status = Capability(
                    CapabilityStatus.AVAILABLE,
                    "Firmware configuration is declared in twin.yaml.",
                )
            else:
                firmware_status = unavailable_firmware

        hardware = raw.get("hardware")
        if hardware is not None and not isinstance(hardware, dict):
            valid = False
            hardware_status = Capability(
                CapabilityStatus.INVALID, "The twin.yaml hardware entry must be a mapping."
            )
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "TWIN_HARDWARE_INVALID",
                    hardware_status.detail,
                    path=str(path),
                )
            )
        elif hardware:
            hardware_status = Capability(
                CapabilityStatus.UNAVAILABLE,
                "A hardware target is configured; connect and qualify it before use.",
            )
        else:
            hardware_status = unavailable_hardware

        return _ManifestInfo(
            data=raw,
            valid=valid,
            missing_models=tuple(missing_models),
            firmware_status=firmware_status,
            hardware_status=hardware_status,
        )

    def _read_version(self, cli: Path, work_dir: Path, diagnostics: list[Diagnostic]) -> str | None:
        try:
            process = self._runner(
                [str(cli), "version"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    "KICAD_VERSION_UNAVAILABLE",
                    f"Could not query kicad-cli version: {exc}",
                    path=str(cli),
                )
            )
            return None
        if process.returncode != 0:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    "KICAD_VERSION_UNAVAILABLE",
                    "kicad-cli version failed: " + _process_detail(process),
                    path=str(cli),
                )
            )
            return None
        return next((line.strip() for line in process.stdout.splitlines() if line.strip()), None)

    def _export(
        self,
        cli: Path,
        label: str,
        command: Sequence[str],
        output: Path,
        work_dir: Path,
        exported: dict[str, Path],
        diagnostics: list[Diagnostic],
    ) -> None:
        arguments = [str(cli), *command]
        try:
            process = self._runner(
                arguments,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "KICAD_EXPORT_TIMEOUT",
                    f"KiCad timed out while generating {label}; retry or inspect "
                    "the project in KiCad.",
                    reference=label,
                    path=str(output),
                )
            )
            return
        except (OSError, subprocess.SubprocessError) as exc:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "KICAD_EXPORT_FAILED",
                    f"KiCad could not generate {label}: {exc}",
                    reference=label,
                    path=str(output),
                )
            )
            return
        if process.returncode != 0:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "KICAD_EXPORT_FAILED",
                    f"KiCad could not generate {label}: {_process_detail(process)}",
                    reference=label,
                    path=str(output),
                )
            )
            return
        if not output.is_file():
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "KICAD_EXPORT_OUTPUT_MISSING",
                    f"KiCad reported success but did not create the {label} output.",
                    reference=label,
                    path=str(output),
                )
            )
            return
        exported[label] = output

    def _normalize(
        self, exported: dict[str, Path], diagnostics: list[Diagnostic]
    ) -> dict[str, Any]:
        bom: dict[str, BomPart] = {}
        placements: dict[str, Placement] = {}
        nets: tuple[Net, ...] = ()
        logical_components: dict[str, dict[str, Any]] = {}
        valid: set[str] = set()
        bounds: tuple[float, float, float, float] | None = None

        parsers: tuple[tuple[str, Callable[[Path], Any]], ...] = (
            ("bom", parse_bom_csv),
            ("placements", parse_placement_csv),
            ("logical_netlist", parse_logical_netlist),
            ("outline", dxf_bounds),
            ("top_preview", validate_svg),
            ("bottom_preview", validate_svg),
        )
        parsed: dict[str, Any] = {}
        for label, parser in parsers:
            path = exported.get(label)
            if path is None:
                continue
            try:
                parsed[label] = parser(path)
                valid.add(label)
            except ArtifactParseError as exc:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "KICAD_ARTIFACT_INVALID",
                        f"The exported {label} is invalid: {exc}",
                        reference=label,
                        path=str(path),
                    )
                )

        bom = parsed.get("bom", {})
        placements = parsed.get("placements", {})
        if "logical_netlist" in parsed:
            nets, logical_components = parsed["logical_netlist"]
        if "outline" in parsed:
            bounds = parsed["outline"]

        spice = exported.get("spice_netlist")
        if spice is not None:
            try:
                content = spice.read_text(encoding="utf-8-sig", errors="strict")
                meaningful = [
                    line
                    for line in content.splitlines()
                    if line.strip() and not line.lstrip().startswith(("*", ";", ".", "#"))
                ]
                if not meaningful:
                    raise ArtifactParseError("SPICE netlist contains no circuit elements")
                valid.add("spice_netlist")
            except (OSError, UnicodeError, ArtifactParseError) as exc:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "KICAD_ARTIFACT_INVALID",
                        f"The exported SPICE netlist is invalid: {exc}",
                        reference="spice_netlist",
                        path=str(spice),
                    )
                )

        for label in ("erc", "drc"):
            report = exported.get(label)
            if report is None:
                continue
            try:
                json.loads(report.read_text(encoding="utf-8-sig"))
                valid.add(label)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.WARNING,
                        "KICAD_REPORT_INVALID",
                        f"The {label.upper()} JSON report could not be read: {exc}",
                        reference=label,
                        path=str(report),
                    )
                )

        components = _merge_components(
            bom,
            placements,
            logical_components,
            nets,
            diagnostics,
            bom_complete="bom" in valid,
            placement_complete="placements" in valid,
        )
        if bounds is None:
            positioned = [
                (component.x_mm, component.y_mm)
                for component in components
                if component.x_mm is not None and component.y_mm is not None
            ]
            if positioned:
                xs = [float(x) for x, _ in positioned if x is not None]
                ys = [float(y) for _, y in positioned if y is not None]
                bounds = (min(xs), min(ys), max(xs), max(ys))
        return {
            "bom": bom,
            "placements": placements,
            "nets": nets,
            "components": components,
            "valid": valid,
            "bounds": bounds,
        }

    def _copy_artifacts(self, exported: dict[str, Path], cache_dir: Path) -> dict[str, Path]:
        names = {
            "bom": "bom.csv",
            "logical_netlist": "logical.xml",
            "spice_netlist": "circuit.cir",
            "placements": "placements.csv",
            "outline": "outline.dxf",
            "top_preview": "top.svg",
            "bottom_preview": "bottom.svg",
            "erc": "erc.json",
            "drc": "drc.json",
        }
        copied: dict[str, Path] = {}
        for label, source in exported.items():
            destination = cache_dir / names[label]
            _atomic_copy(source, destination)
            copied[label] = destination
        return copied

    def _build_project(
        self,
        *,
        sources: _SourceFiles,
        variant: str,
        project_id: str,
        source_hashes: dict[str, str],
        kicad_version: str | None,
        cache_dir: Path,
        cached_artifacts: dict[str, Path],
        manifest_info: _ManifestInfo,
        diagnostics: list[Diagnostic],
        normalized: dict[str, Any],
    ) -> ImportedProject:
        valid: set[str] = normalized["valid"]
        bounds = normalized["bounds"] or (0.0, 0.0, 100.0, 60.0)
        visible_geometry = valid.intersection({"outline", "top_preview", "bottom_preview"})
        if sources.board is None:
            geometry_capability = Capability(
                CapabilityStatus.UNAVAILABLE, "No sibling .kicad_pcb board file was found."
            )
        elif visible_geometry:
            geometry_capability = Capability(
                CapabilityStatus.AVAILABLE,
                f"Board geometry imported with {len(normalized['components'])} "
                "normalized components.",
            )
        else:
            geometry_capability = Capability(
                CapabilityStatus.INVALID,
                "The board exists, but KiCad produced no usable outline or SVG preview.",
            )

        if sources.schematic is None:
            circuit_capability = Capability(
                CapabilityStatus.UNAVAILABLE, "No sibling .kicad_sch schematic file was found."
            )
        elif {"logical_netlist", "spice_netlist"}.issubset(valid):
            circuit_capability = Capability(
                CapabilityStatus.AVAILABLE,
                "Logical and SPICE netlists were exported; model coverage is checked at run time.",
            )
        else:
            circuit_capability = Capability(
                CapabilityStatus.INVALID,
                "The schematic exists, but its logical or SPICE netlist export is unusable.",
            )
        if manifest_info.missing_models:
            circuit_capability = Capability(
                CapabilityStatus.INVALID,
                "One or more SPICE model files declared in twin.yaml are missing.",
            )

        return ImportedProject(
            schema_version=1,
            project_id=project_id,
            name=str(manifest_info.data.get("name") or sources.project.stem),
            source_dir=str(sources.project.parent),
            cache_dir=str(cache_dir.resolve()),
            source_hashes=source_hashes,
            kicad_version=kicad_version,
            variant=variant,
            components=normalized["components"],
            nets=normalized["nets"],
            geometry=BoardGeometry(
                outline_path=(
                    str(cached_artifacts["outline"].resolve()) if "outline" in valid else None
                ),
                top_preview_path=(
                    str(cached_artifacts["top_preview"].resolve())
                    if "top_preview" in valid
                    else None
                ),
                bottom_preview_path=(
                    str(cached_artifacts["bottom_preview"].resolve())
                    if "bottom_preview" in valid
                    else None
                ),
                min_x_mm=float(bounds[0]),
                min_y_mm=float(bounds[1]),
                max_x_mm=float(bounds[2]),
                max_y_mm=float(bounds[3]),
            ),
            capabilities=ProjectCapabilities(
                geometry_capability,
                circuit_capability,
                manifest_info.firmware_status,
                manifest_info.hardware_status,
            ),
            diagnostics=tuple(diagnostics),
            spice_netlist_path=(
                str(cached_artifacts["spice_netlist"].resolve())
                if "spice_netlist" in valid
                else None
            ),
            twin_manifest_path=(
                str(cached_artifacts["twin_manifest"].resolve())
                if "twin_manifest" in cached_artifacts
                else None
            ),
        )

    def _unavailable_source_project(
        self,
        sources: _SourceFiles,
        variant: str,
        project_id: str,
        source_hashes: dict[str, str],
        manifest_info: _ManifestInfo,
        diagnostics: list[Diagnostic],
    ) -> ImportedProject:
        geometry = Capability(
            CapabilityStatus.UNAVAILABLE,
            "KiCad CLI is required to normalize the board geometry."
            if sources.board
            else "No sibling .kicad_pcb board file was found.",
        )
        circuit = Capability(
            CapabilityStatus.UNAVAILABLE,
            "KiCad CLI is required to export circuit netlists."
            if sources.schematic
            else "No sibling .kicad_sch schematic file was found.",
        )
        return ImportedProject(
            schema_version=1,
            project_id=project_id,
            name=str(manifest_info.data.get("name") or sources.project.stem),
            source_dir=str(sources.project.parent),
            cache_dir=str(self.cache_root.resolve()),
            source_hashes=source_hashes,
            kicad_version=None,
            variant=variant,
            components=(),
            nets=(),
            geometry=BoardGeometry(),
            capabilities=ProjectCapabilities(
                geometry, circuit, manifest_info.firmware_status, manifest_info.hardware_status
            ),
            diagnostics=tuple(diagnostics),
            twin_manifest_path=str(sources.manifest) if sources.manifest else None,
        )

    def _changed_source_project(
        self,
        sources: _SourceFiles,
        variant: str,
        project_id: str,
        source_hashes: dict[str, str],
        kicad_version: str | None,
        manifest_info: _ManifestInfo,
        diagnostics: list[Diagnostic],
    ) -> ImportedProject:
        invalid = Capability(
            CapabilityStatus.INVALID,
            "Source files changed during import; retry to obtain a consistent snapshot.",
        )
        return ImportedProject(
            schema_version=1,
            project_id=project_id,
            name=str(manifest_info.data.get("name") or sources.project.stem),
            source_dir=str(sources.project.parent),
            cache_dir=str(self.cache_root.resolve()),
            source_hashes=source_hashes,
            kicad_version=kicad_version,
            variant=variant,
            components=(),
            nets=(),
            geometry=BoardGeometry(),
            capabilities=ProjectCapabilities(
                invalid, invalid, manifest_info.firmware_status, manifest_info.hardware_status
            ),
            diagnostics=tuple(diagnostics),
            twin_manifest_path=str(sources.manifest) if sources.manifest else None,
        )

    def _cache_directory(
        self,
        name: str,
        project_id: str,
        variant: str,
        source_hashes: dict[str, str],
    ) -> Path:
        digest_input = json.dumps(
            {"schema": 1, "variant": variant, "sources": source_hashes},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(digest_input).hexdigest()[:16]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-") or "project"
        safe_project_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_id).strip(".-") or "project"
        safe_variant = re.sub(r"[^A-Za-z0-9_.-]+", "-", variant).strip(".-") or "default"
        return self.cache_root / f"{safe_name}-{safe_project_id}" / f"{safe_variant}-{digest}"


def _discover_sibling(
    project: Path,
    suffix: str,
    kind: str,
    diagnostics: list[Diagnostic],
) -> Path | None:
    exact = project.with_suffix(suffix)
    if exact.is_file():
        return exact.resolve()
    candidates = sorted(
        path.resolve() for path in project.parent.glob(f"*{suffix}") if path.is_file()
    )
    if len(candidates) == 1:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.INFO,
                f"{kind.upper()}_FALLBACK_DISCOVERY",
                f"Using the only {suffix} file in the project folder: {candidates[0].name}.",
                path=str(candidates[0]),
            )
        )
        return candidates[0]
    if candidates:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                f"{kind.upper()}_AMBIGUOUS",
                f"No {project.stem}{suffix} exists and multiple {suffix} files were found; "
                f"rename/select a project with an unambiguous sibling {kind}.",
                path=str(project.parent),
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.WARNING,
                f"{kind.upper()}_MISSING",
                f"No sibling {suffix} {kind} was found; its capabilities will be unavailable.",
                path=str(project.parent),
            )
        )
    return None


def _hash_sources(paths: Sequence[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.name] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def _stage_sources(sources: _SourceFiles, destination: Path) -> _SourceFiles:
    """Clone the project tree without following links or copying generated state."""

    destination.mkdir(parents=True, exist_ok=True)
    source_root = sources.project.parent
    excluded_directories = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "gerbers",
        "manufacturing",
        "out",
        "output",
        "venv",
    }

    for current, directory_names, file_names in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in excluded_directories
            and not name.casefold().endswith("-backups")
            and not (current_path / name).is_symlink()
        ]
        relative = current_path.relative_to(source_root)
        target_directory = destination / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in file_names:
            source = current_path / name
            lowered = name.casefold()
            if (
                source.is_symlink()
                or lowered.endswith(".kicad_prl")
                or lowered.endswith(".lck")
                or lowered.startswith("~")
            ):
                continue
            shutil.copy2(source, target_directory / name)

    return _SourceFiles(
        project=destination / sources.project.relative_to(source_root),
        schematic=(
            destination / sources.schematic.relative_to(source_root)
            if sources.schematic is not None
            else None
        ),
        board=(
            destination / sources.board.relative_to(source_root)
            if sources.board is not None
            else None
        ),
        manifest=(
            destination / sources.manifest.relative_to(source_root)
            if sources.manifest is not None
            else None
        ),
    )


def _project_id(project: Path) -> str:
    normalized = os.path.normcase(str(project.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _process_detail(process: subprocess.CompletedProcess[str]) -> str:
    detail = (process.stderr or process.stdout or f"exit code {process.returncode}").strip()
    return " ".join(detail.split())[:500]


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _merge_components(
    bom: dict[str, BomPart],
    placements: dict[str, Placement],
    logical_components: dict[str, dict[str, Any]],
    nets: tuple[Net, ...],
    diagnostics: list[Diagnostic],
    *,
    bom_complete: bool,
    placement_complete: bool,
) -> tuple[Component, ...]:
    net_names: dict[str, set[str]] = {}
    for net in nets:
        for pin in net.pins:
            net_names.setdefault(pin.reference.casefold(), set()).add(net.name)

    identities = set(bom) | set(placements) | set(logical_components)
    components: list[Component] = []
    for identity in identities:
        bom_part = bom.get(identity)
        placement = placements.get(identity)
        logical = logical_components.get(identity, {})
        reference = (
            bom_part.reference
            if bom_part is not None
            else placement.reference
            if placement is not None
            else str(logical.get("reference", identity))
        )
        if bom_complete and placement_complete:
            if bom_part is not None and placement is None:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.WARNING,
                        "BOM_ONLY_COMPONENT",
                        f"{reference} is in the schematic BOM but not in PCB placement output.",
                        reference=reference,
                    )
                )
            elif placement is not None and bom_part is None:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.WARNING,
                        "BOARD_ONLY_COMPONENT",
                        f"{reference} is in PCB placement output but not in the schematic BOM.",
                        reference=reference,
                    )
                )

        fields: dict[str, str] = {}
        logical_fields = logical.get("fields", {})
        if isinstance(logical_fields, dict):
            fields.update({str(key): str(value) for key, value in logical_fields.items()})
        if bom_part is not None:
            fields.update(bom_part.fields)
        value = (
            bom_part.value
            if bom_part is not None and bom_part.value
            else placement.value
            if placement is not None and placement.value
            else str(logical.get("value", ""))
        )
        footprint = (
            bom_part.footprint
            if bom_part is not None and bom_part.footprint
            else placement.footprint
            if placement is not None and placement.footprint
            else str(logical.get("footprint", ""))
        )
        components.append(
            Component(
                reference=reference,
                value=value,
                footprint=footprint,
                x_mm=placement.x_mm if placement is not None else None,
                y_mm=placement.y_mm if placement is not None else None,
                rotation_deg=placement.rotation_deg if placement is not None else 0.0,
                side=placement.side if placement is not None else ComponentSide.UNKNOWN,
                in_bom=bom_part is not None,
                on_board=placement is not None,
                is_smd=placement.is_smd if placement is not None else False,
                nets=tuple(sorted(net_names.get(identity, set()), key=natural_key)),
                fields=fields,
            )
        )
    components.sort(key=lambda item: natural_key(item.reference))
    return tuple(components)
