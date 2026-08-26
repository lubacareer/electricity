from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from smd_twin_lab.importers import KiCadProjectImporter
from smd_twin_lab.models import CapabilityStatus, ComponentSide

LOGICAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export version="D">
  <components>
    <comp ref="R1">
      <value>10k</value><footprint>Resistor_SMD:R_0603</footprint>
      <fields><field name="Tolerance">1%</field></fields>
    </comp>
    <comp ref="C1"><value>100n</value><footprint>Capacitor_SMD:C_0603</footprint></comp>
  </components>
  <nets>
    <net code="1" name="SENSE">
      <node ref="R1" pin="2"/><node ref="C1" pin="1"/>
    </net>
    <net code="2" name="GND"><node ref="C1" pin="2"/></net>
  </nets>
</export>
"""


class FakeKiCad:
    def __init__(
        self,
        *,
        mutate_source: Path | None = None,
        fail_spice: bool = False,
        empty_spice: bool = False,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.mutate_source = mutate_source
        self.fail_spice = fail_spice
        self.empty_spice = empty_spice

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), kwargs.copy()))
        assert isinstance(args, list)
        assert kwargs["shell"] is False
        if args[1:] == ["version"]:
            return subprocess.CompletedProcess(args, 0, "10.0.5\n", "")

        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        command = args[1:4]
        if command == ["sch", "export", "bom"]:
            output.write_text(
                "Refs,Value,Footprint,MPN\n"
                "R1,10k,Resistor_SMD:R_0603,R-10K\n"
                "C1,100n,Capacitor_SMD:C_0603,C-100N\n",
                encoding="utf-8",
            )
        elif command == ["sch", "export", "netlist"]:
            output_format = args[args.index("--format") + 1]
            if output_format == "spice" and self.fail_spice:
                return subprocess.CompletedProcess(args, 2, "", "missing model")
            output.write_text(
                LOGICAL_XML
                if output_format == "kicadxml"
                else ".title Empty schematic\n.end\n"
                if self.empty_spice
                else "* generated\nV1 VCC 0 5\nR1 VCC SENSE 10k\n.end\n",
                encoding="utf-8",
            )
        elif args[1:3] == ["sch", "erc"] or args[1:3] == ["pcb", "drc"]:
            output.write_text('{"violations": []}\n', encoding="utf-8")
            if args[1:3] == ["pcb", "drc"]:
                Path(args[-1]).with_suffix(".kicad_prl").write_text(
                    "generated state", encoding="utf-8"
                )
        elif command == ["pcb", "export", "pos"]:
            output.write_text(
                "Ref,Val,Package,PosX,PosY,Rot,Side\n"
                "R1,10k,R_0603_1608Metric,10.0,-12.0,90.0,Front\n"
                "U1,STM32,QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm,25.0,-8.0,270.0,Back\n",
                encoding="utf-8",
            )
        elif command == ["pcb", "export", "dxf"]:
            output.write_text(
                "0\nSECTION\n2\nHEADER\n10\n1e20\n20\n-1e20\n0\nENDSEC\n"
                "0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n40\n21\n20\n"
                "0\nENDSEC\n0\nEOF\n",
                encoding="utf-8",
            )
        elif command == ["pcb", "export", "svg"]:
            output.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8")
        else:  # pragma: no cover - catches accidental new command shapes
            raise AssertionError(f"Unexpected fake KiCad command: {args}")

        if self.mutate_source is not None and args[1:3] == ["pcb", "drc"]:
            self.mutate_source.write_text("changed concurrently", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")


def _source_project(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    project = tmp_path / "training.kicad_pro"
    schematic = tmp_path / "training.kicad_sch"
    board = tmp_path / "training.kicad_pcb"
    project.write_bytes(b"{project: read-only}")
    schematic.write_bytes(b"(kicad_sch (version 20250114))")
    board.write_bytes(b"(kicad_pcb (version 20250114))")
    return project, {path.name: path.read_bytes() for path in (project, schematic, board)}


def test_kicad_import_exports_normalizes_and_preserves_sources(tmp_path: Path) -> None:
    source_dir = tmp_path / "source with spaces"
    source_dir.mkdir()
    project_file, original = _source_project(source_dir)
    cache_root = tmp_path / "cache"
    fake = FakeKiCad()

    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake kicad-cli.exe",
        cache_root=cache_root,
        runner=fake,
    ).import_project(project_file, variant="prototype A")

    assert project.kicad_version == "10.0.5"
    assert project.capabilities.geometry.status == CapabilityStatus.AVAILABLE
    assert project.capabilities.circuit.status == CapabilityStatus.AVAILABLE
    assert project.capabilities.firmware.status == CapabilityStatus.UNAVAILABLE
    assert project.capabilities.geometry.message_ref is not None
    assert project.capabilities.geometry.message_ref.message_id == (
        "capability.kicad.geometry_imported"
    )
    assert project.capabilities.geometry.message_ref.count == 3
    assert [component.reference for component in project.components] == ["C1", "R1", "U1"]

    resistor = next(component for component in project.components if component.reference == "R1")
    assert (resistor.in_bom, resistor.on_board, resistor.is_smd) == (True, True, True)
    assert (resistor.x_mm, resistor.y_mm, resistor.rotation_deg) == (10.0, -12.0, 90.0)
    assert resistor.side == ComponentSide.FRONT
    assert resistor.nets == ("SENSE",)
    assert resistor.fields["Tolerance"] == "1%"
    assert resistor.fields["MPN"] == "R-10K"

    assert project.geometry.min_x_mm == 0.0
    assert project.geometry.max_x_mm == 40.0
    assert project.geometry.max_y_mm == 20.0
    assert Path(project.geometry.top_preview_path or "").is_file()
    assert Path(project.geometry.bottom_preview_path or "").is_file()
    assert Path(project.geometry.outline_path or "").is_file()
    assert Path(project.spice_netlist_path or "").is_file()
    assert Path(project.cache_dir, "erc.json").is_file()
    assert Path(project.cache_dir, "drc.json").is_file()

    diagnostic_codes = {diagnostic.code for diagnostic in project.diagnostics}
    assert "BOM_ONLY_COMPONENT" in diagnostic_codes
    assert "BOARD_ONLY_COMPONENT" in diagnostic_codes
    for path in source_dir.iterdir():
        assert path.read_bytes() == original[path.name]
    assert project.source_hashes == {
        name: hashlib.sha256(content).hexdigest() for name, content in sorted(original.items())
    }

    export_calls = [call for call, _ in fake.calls if call[1:] != ["version"]]
    assert len(export_calls) == 9
    assert all(
        "--variant" in call and "prototype A" in call
        for call in export_calls
        if "erc" not in call and "drc" not in call
    )
    position_call = next(call for call in export_calls if call[1:4] == ["pcb", "export", "pos"])
    assert position_call[position_call.index("--units") + 1] == "mm"
    assert position_call[position_call.index("--side") + 1] == "both"
    assert all(isinstance(argument, str) for call in export_calls for argument in call)

    bundle_path = Path(project.cache_dir) / "project.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["coordinate_system"]["y_axis"] == "up"
    assert payload["coordinate_system"]["bottom_coordinates_mirrored"] is False
    assert payload["geometry"]["top_preview_path"] == "top.svg"

    # An unchanged source snapshot is served from cache without invoking KiCad.
    def should_not_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("cached import unexpectedly invoked KiCad")

    cached = KiCadProjectImporter(
        kicad_cli=tmp_path / "missing.exe",
        cache_root=cache_root,
        runner=should_not_run,
    ).import_project(project_file, variant="prototype A")
    assert cached.components == project.components
    assert cached.source_hashes == project.source_hashes
    assert cached.capabilities == project.capabilities
    assert cached.diagnostics == project.diagnostics


def test_direct_bundle_load_does_not_discover_or_run_kicad(tmp_path: Path) -> None:
    bundle = tmp_path / "portable bundle"
    bundle.mkdir()
    (bundle / "top.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    (bundle / "circuit.cir").write_text("V1 VCC 0 3.3\n.end\n", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "project_id": "portable-sample",
        "name": "Portable sample",
        "source_dir": ".",
        "cache_dir": ".",
        "source_hashes": {},
        "kicad_version": "10.0.5",
        "variant": "default",
        "components": [
            {
                "reference": "R1",
                "value": "10k",
                "footprint": "R_0603",
                "x_mm": 1.0,
                "y_mm": 2.0,
                "rotation_deg": 0.0,
                "side": "front",
                "in_bom": True,
                "on_board": True,
                "is_smd": True,
                "nets": ["SENSE"],
                "fields": {},
            }
        ],
        "nets": [{"name": "SENSE", "pins": [{"reference": "R1", "pin": "1"}]}],
        "geometry": {
            "outline_path": None,
            "top_preview_path": "top.svg",
            "bottom_preview_path": None,
            "min_x_mm": 0,
            "min_y_mm": 0,
            "max_x_mm": 10,
            "max_y_mm": 10,
        },
        "capabilities": {
            "geometry": {"status": "available", "detail": "Bundled preview."},
            "circuit": {"status": "available", "detail": "Bundled circuit."},
            "firmware": {"status": "unavailable", "detail": "No firmware."},
            "hardware": {"status": "unavailable", "detail": "No hardware."},
        },
        "diagnostics": [],
        "spice_netlist_path": "circuit.cir",
        "twin_manifest_path": None,
    }
    (bundle / "project.json").write_text(json.dumps(payload), encoding="utf-8")

    def should_not_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("bundle loading unexpectedly invoked an external process")

    project = KiCadProjectImporter(runner=should_not_run).import_project(bundle)
    assert project.project_id == "portable-sample"
    assert project.cache_dir == str(bundle.resolve())
    assert project.components[0].side == ComponentSide.FRONT
    assert Path(project.geometry.top_preview_path or "") == (bundle / "top.svg").resolve()
    assert Path(project.spice_netlist_path or "") == (bundle / "circuit.cir").resolve()


def test_failed_spice_export_is_diagnostic_and_keeps_geometry(tmp_path: Path) -> None:
    project_file, _ = _source_project(tmp_path)
    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake.exe",
        cache_root=tmp_path / "cache",
        runner=FakeKiCad(fail_spice=True),
    ).import_project(project_file)

    assert project.capabilities.geometry.status == CapabilityStatus.AVAILABLE
    assert project.capabilities.circuit.status == CapabilityStatus.INVALID
    failure = next(
        diagnostic
        for diagnostic in project.diagnostics
        if diagnostic.code == "KICAD_EXPORT_FAILED" and diagnostic.reference == "spice_netlist"
    )
    assert "missing model" in failure.message
    assert failure.message_ref is not None
    assert failure.message_ref.parameters["artifact"] == "spice_netlist"
    assert project.spice_netlist_path is None


def test_empty_spice_export_is_not_reported_as_circuit_ready(tmp_path: Path) -> None:
    project_file, _ = _source_project(tmp_path)
    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake.exe",
        cache_root=tmp_path / "cache",
        runner=FakeKiCad(empty_spice=True),
    ).import_project(project_file)

    assert project.capabilities.circuit.status == CapabilityStatus.INVALID
    assert project.spice_netlist_path is None
    diagnostic = next(
        item
        for item in project.diagnostics
        if item.code == "KICAD_ARTIFACT_INVALID" and item.reference == "spice_netlist"
    )
    assert "no circuit elements" in diagnostic.message


def test_source_change_during_export_rejects_cache_snapshot(tmp_path: Path) -> None:
    project_file, _ = _source_project(tmp_path)
    schematic = project_file.with_suffix(".kicad_sch")
    cache_root = tmp_path / "cache"
    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake.exe",
        cache_root=cache_root,
        runner=FakeKiCad(mutate_source=schematic),
    ).import_project(project_file)

    assert project.capabilities.geometry.status == CapabilityStatus.INVALID
    assert project.capabilities.circuit.status == CapabilityStatus.INVALID
    assert "SOURCE_CHANGED_DURING_IMPORT" in {diagnostic.code for diagnostic in project.diagnostics}
    assert not list(cache_root.rglob("project.json"))


def test_malformed_bundle_returns_actionable_invalid_project(tmp_path: Path) -> None:
    bundle = tmp_path / "project.json"
    bundle.write_text("{broken", encoding="utf-8")
    project = KiCadProjectImporter().import_project(bundle)

    assert project.capabilities.geometry.status == CapabilityStatus.INVALID
    assert project.diagnostics[0].code == "BUNDLE_JSON_INVALID"
    assert "could not be read" in project.diagnostics[0].message
    assert project.diagnostics[0].message_ref is not None
    assert project.diagnostics[0].message_ref.message_id == "diagnostic.bundle.json_invalid"


def test_importer_uses_strict_manifest_validation(tmp_path: Path) -> None:
    project_file, _ = _source_project(tmp_path)
    (tmp_path / "twin.yaml").write_text(
        """schema_version: 1
project_id: test
name: Test
coordinate_system:
  unit: inches
  viewpoint: top
  x_direction: right
  y_direction: up
  rotation: counter_clockwise_degrees
faults: []
scenarios:
  - id: broken
    fault: missing
""",
        encoding="utf-8",
    )

    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake.exe",
        cache_root=tmp_path / "cache",
        runner=FakeKiCad(),
    ).import_project(project_file)

    assert project.capabilities.firmware.status == CapabilityStatus.INVALID
    assert project.capabilities.hardware.status == CapabilityStatus.INVALID
    assert "TWIN_MANIFEST_INVALID" in {item.code for item in project.diagnostics}


def test_importer_checks_declared_firmware_executable(tmp_path: Path) -> None:
    project_file, _ = _source_project(tmp_path)
    (tmp_path / "twin.yaml").write_text(
        """schema_version: 1
project_id: test
name: Test
coordinate_system:
  unit: mm
  viewpoint: top
  x_direction: right
  y_direction: up
  rotation: counter_clockwise_degrees
firmware:
  engine: renode
  executable: build/missing.elf
""",
        encoding="utf-8",
    )

    project = KiCadProjectImporter(
        kicad_cli=tmp_path / "fake.exe",
        cache_root=tmp_path / "cache",
        runner=FakeKiCad(),
    ).import_project(project_file)

    assert project.capabilities.firmware.status == CapabilityStatus.INVALID
    assert "FIRMWARE_IMAGE_MISSING" in {item.code for item in project.diagnostics}
