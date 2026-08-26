import json
from dataclasses import replace
from pathlib import Path

import pytest

from smd_twin_lab.eda import DrcEngine, divider_project
from smd_twin_lab.eda.kicad import (
    BridgeStatus,
    FabricationPackager,
    KiCad10Bridge,
    ValidationReport,
    _count_report_violations,
    _semantic_netlist_summary,
    render_board,
    render_schematic,
)
from smd_twin_lab.eda.model import (
    EDA_SCHEMA_VERSION,
    BoardDocument,
    BoardFootprint,
    BoardPad,
    BoardSide,
    BoardTrack,
    BoardVia,
    CopperLayer,
    EdaProjectDocument,
    PointNm,
    ProjectManifest,
    SchematicDocument,
    SchematicPin,
    SchematicSymbol,
    SchematicWire,
    mm,
)
from smd_twin_lab.engines.process import ProcessResult
from smd_twin_lab.tooling import discover_tools


def _document() -> EdaProjectDocument:
    pin_1 = SchematicPin("r1-pin-1", "1", "1", PointNm(-mm(2.54), 0))
    pin_2 = SchematicPin("r1-pin-2", "2", "2", PointNm(mm(2.54), 0))
    symbol = SchematicSymbol(
        "r1-symbol",
        "R1",
        "10 kOhm",
        "Device:R",
        "resistor",
        PointNm(mm(20), mm(20)),
        (pin_1, pin_2),
        "Resistor_SMD:R_0603_1608Metric",
    )
    footprint = BoardFootprint(
        "r1-footprint",
        "R1",
        "Resistor_SMD:R_0603_1608Metric",
        symbol.symbol_id,
        PointNm(mm(10), mm(10)),
        (
            BoardPad("r1-pad-1", "1", PointNm(-mm(0.8), 0), mm(0.9), mm(0.95), "VCC"),
            BoardPad("r1-pad-2", "2", PointNm(mm(0.8), 0), mm(0.9), mm(0.95), "OUT"),
        ),
        courtyard_width_nm=mm(2.4),
        courtyard_height_nm=mm(1.4),
    )
    return EdaProjectDocument(
        ProjectManifest(EDA_SCHEMA_VERSION, "test-divider", "Test divider"),
        SchematicDocument(symbols=(symbol,)),
        BoardDocument(
            outline=(
                PointNm(0, 0),
                PointNm(mm(30), 0),
                PointNm(mm(30), mm(20)),
                PointNm(0, mm(20)),
            ),
            footprints=(footprint,),
        ),
    )


def test_generated_files_use_owned_identity_and_canonical_coordinates(tmp_path: Path) -> None:
    document = _document()
    bridge = KiCad10Bridge(kicad_cli=tmp_path / "missing-kicad-cli.exe")

    generated = bridge.generate(document, tmp_path / "generated")

    schematic = Path(generated.schematic_file).read_text(encoding="utf-8")
    board = Path(generated.board_file).read_text(encoding="utf-8")
    assert '(generator "smd_twin_lab")' in schematic
    assert '(generator "smd_twin_lab")' in board
    assert '(property "Reference" "R1"' in board
    assert '(net 1 "/OUT")' in board
    assert '(net 2 "/VCC")' in board
    assert render_schematic(document, "Test_divider") == schematic
    assert render_board(document) == board
    assert generated.expected_references == ("R1",)
    project = json.loads(Path(generated.project_file).read_text(encoding="utf-8"))
    assert project["erc"]["rule_severities"] == {
        "footprint_link_issues": "ignore",
        "lib_symbol_issues": "ignore",
    }


def test_render_board_assigns_net_zero_to_unassigned_copper() -> None:
    document = _document()
    document = replace(
        document,
        board=replace(
            document.board,
            tracks=(
                BoardTrack(
                    "unassigned-track",
                    "",
                    PointNm(mm(5), mm(5)),
                    PointNm(mm(10), mm(5)),
                    mm(0.25),
                ),
            ),
            vias=(
                BoardVia(
                    "unassigned-via",
                    "",
                    PointNm(mm(10), mm(10)),
                    mm(0.8),
                    mm(0.4),
                ),
            ),
        ),
    )

    board = render_board(document)

    assert board.count("\n\t\t(net 0)") == 2


def test_kicad_report_counter_includes_sheet_erc_and_parity() -> None:
    report = {
        "sheets": [
            {"violations": [{"severity": "warning"}]},
            {"violations": [{"severity": "error"}, {"severity": "warning"}]},
        ],
        "schematic_parity": [{"type": "missing_footprint"}],
        "unconnected_items": [{"type": "unconnected"}],
    }

    assert _count_report_violations(report) == (4, 1)


def test_semantic_netlist_summary_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "netlist.xml"
    path.write_text(
        "<export><components><comp ref='R2'/><comp ref='R1'/></components>"
        "<nets><net name='/VOUT'/><net name='/GND'/></nets></export>",
        encoding="utf-8",
    )

    assert _semantic_netlist_summary(path) == (("R1", "R2"), ("/GND", "/VOUT"))


def test_schematic_polylines_are_serialized_as_kicad_segments() -> None:
    document = _document()
    wire = SchematicWire(
        "wire-polyline",
        (PointNm(0, 0), PointNm(mm(5), 0), PointNm(mm(5), mm(5))),
    )
    document = document.revised(
        schematic=SchematicDocument(
            symbols=document.schematic.symbols,
            wires=(wire,),
        )
    )

    rendered = render_schematic(document, "Test_divider")

    assert rendered.count("\t(wire (pts") == 2
    assert "(xy 0 0) (xy 5 0) (xy 5 -5)" not in rendered


def test_bottom_smd_pad_uses_back_copper_in_drc_and_kicad() -> None:
    pad = BoardPad(
        "bottom-pad",
        "1",
        PointNm(0, 0),
        mm(1),
        mm(1),
        "PAD_NET",
    )
    footprint = BoardFootprint(
        "bottom-footprint",
        "U1",
        "Test:BottomSmd",
        "",
        PointNm(mm(10), mm(10)),
        (pad,),
        side=BoardSide.BACK,
    )
    document = EdaProjectDocument(
        ProjectManifest(EDA_SCHEMA_VERSION, "bottom-smd", "Bottom SMD"),
        SchematicDocument(),
        BoardDocument(
            outline=(
                PointNm(0, 0),
                PointNm(mm(20), 0),
                PointNm(mm(20), mm(20)),
                PointNm(0, mm(20)),
                PointNm(0, 0),
            ),
            footprints=(footprint,),
            tracks=(
                BoardTrack(
                    "back-track",
                    "BACK_NET",
                    PointNm(mm(8), mm(10)),
                    PointNm(mm(12), mm(10)),
                    mm(0.25),
                    CopperLayer.BACK,
                ),
                BoardTrack(
                    "front-track",
                    "FRONT_NET",
                    PointNm(mm(8), mm(10)),
                    PointNm(mm(12), mm(10)),
                    mm(0.25),
                    CopperLayer.FRONT,
                ),
            ),
        ),
    )

    clearance_pairs = {
        frozenset(issue.item_ids)
        for issue in DrcEngine().check(document).issues
        if issue.code == "drc.copper_clearance"
    }
    rendered = render_board(document)

    assert pad.layers == (CopperLayer.FRONT,)
    assert frozenset((pad.pad_id, "back-track")) in clearance_pairs
    assert frozenset((pad.pad_id, "front-track")) not in clearance_pairs
    assert '(layer "B.Cu")' in rendered
    assert '(layers "B.Cu" "B.Paste" "B.Mask")' in rendered
    assert '(width 0.25) (layer "B.Cu")' in rendered


def test_missing_kicad_is_actionable(tmp_path: Path) -> None:
    bridge = KiCad10Bridge(kicad_cli=tmp_path / "missing-kicad-cli.exe")
    generated = bridge.generate(_document(), tmp_path / "generated")

    report = bridge.validate(generated)

    assert report.status is BridgeStatus.UNAVAILABLE
    assert report.diagnostics[0].code == "KICAD_UNAVAILABLE"


def test_export_never_overwrites_nonempty_destination(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "user-file.txt"
    marker.write_text("preserve me", encoding="utf-8")

    report = KiCad10Bridge().export_new(_document(), destination)

    assert not report.success
    assert report.diagnostics[0].code == "EXPORT_DESTINATION_NOT_EMPTY"
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_successful_export_rebinds_validation_artifacts_to_committed_copy(
    tmp_path: Path,
) -> None:
    class CleanBridge(KiCad10Bridge):
        def validate(self, generated):
            directory = Path(generated.directory)
            (directory / "erc.json").write_text("{}", encoding="utf-8")
            (directory / "drc.json").write_text("{}", encoding="utf-8")
            return ValidationReport(
                BridgeStatus.AVAILABLE,
                "10.0.5",
                0,
                0,
                0,
                erc_report_path=str(directory / "erc.json"),
                drc_report_path=str(directory / "drc.json"),
            )

    destination = tmp_path / "exported"
    report = CleanBridge().export_new(divider_project(), destination)

    assert report.success
    assert report.validation is not None
    assert report.validation.erc_report_path == str(destination / "erc.json")
    assert report.validation.drc_report_path == str(destination / "drc.json")
    assert Path(report.validation.erc_report_path).is_file()


def test_internal_drc_blocks_kicad_export_before_files_are_written(tmp_path: Path) -> None:
    document = divider_project()
    document = document.revised(rules=replace(document.rules, minimum_track_width_nm=mm(1.0)))
    destination = tmp_path / "unsafe-export"

    report = KiCad10Bridge().export_new(document, destination)

    assert not report.success
    assert report.diagnostics[0].code == "INTERNAL_DRC_BLOCKED"
    assert not destination.exists()


def test_round_trip_synchronization_remains_explicitly_gated(tmp_path: Path) -> None:
    plan = KiCad10Bridge().synchronize(_document(), tmp_path / "managed")

    assert not plan.supported
    assert plan.diagnostics[0].code == "ROUND_TRIP_NOT_MATURE"


def test_fabrication_output_uses_resolved_directory_and_requires_files(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.touch()
    bridge = KiCad10Bridge(kicad_cli=executable)
    generated = bridge.generate(_document(), tmp_path / "generated")
    validation = ValidationReport(BridgeStatus.AVAILABLE, "10.0.5", 0, 0, 0)

    def fake_process(argv, **_kwargs) -> ProcessResult:
        command = tuple(str(item) for item in argv)
        output = Path(command[command.index("--output") + 1])
        assert output.is_absolute()
        if command[2:4] == ("export", "gerbers"):
            (output / "board-F_Cu.gtl").write_text("gerber", encoding="utf-8")
        elif command[2:4] == ("export", "drill"):
            (output / "board.drl").write_text("drill", encoding="utf-8")
        else:
            output.write_text("data", encoding="utf-8")
        return ProcessResult(command, 0, "", "")

    monkeypatch.setattr("smd_twin_lab.eda.kicad.run_isolated_process", fake_process)
    output = tmp_path / "fab"

    manifest = FabricationPackager(bridge).build(generated, validation, output)

    assert manifest.output_directory == str(output.resolve())
    assert manifest.design_hash == generated.design_hash
    assert {name for name, _digest in manifest.files} == {
        "board-F_Cu.gtl",
        "board.drl",
        "bom.csv",
        "placements.csv",
    }


def test_failed_fabrication_export_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.touch()
    bridge = KiCad10Bridge(kicad_cli=executable)
    generated = bridge.generate(divider_project(), tmp_path / "generated")
    validation = ValidationReport(BridgeStatus.AVAILABLE, "10.0.5", 0, 0, 0)
    calls = 0

    def fake_process(argv, **_kwargs) -> ProcessResult:
        nonlocal calls
        calls += 1
        command = tuple(str(item) for item in argv)
        output = Path(command[command.index("--output") + 1])
        if calls == 1:
            (output / "partial.gbr").write_text("partial", encoding="utf-8")
            return ProcessResult(command, 0, "", "")
        return ProcessResult(command, 1, "", "synthetic failure")

    monkeypatch.setattr("smd_twin_lab.eda.kicad.run_isolated_process", fake_process)
    destination = tmp_path / "manufacturing"

    with pytest.raises(RuntimeError, match="synthetic failure"):
        FabricationPackager(bridge).build(generated, validation, destination)

    assert not destination.exists()


_NATIVE_KICAD = discover_tools().kicad_cli


@pytest.mark.skipif(_NATIVE_KICAD is None, reason="KiCad 10 is not installed")
def test_native_kicad_accepts_and_packages_the_divider(tmp_path: Path) -> None:
    bridge = KiCad10Bridge(_NATIVE_KICAD)
    destination = tmp_path / "kicad-divider"

    report = bridge.export_new(divider_project(), destination)

    assert report.success
    assert report.validation is not None
    assert report.validation.clean
    assert report.validation.semantic_match
    assert report.validation.erc_violation_count == 0
    assert report.validation.drc_violation_count == 0
    assert report.validation.unconnected_count == 0

    manifest = FabricationPackager(bridge).build(
        report.generated,
        report.validation,
        destination / "manufacturing",
    )
    assert len(manifest.files) >= 4
    assert all(
        Path(manifest.output_directory, name).stat().st_size > 0 for name, _ in manifest.files
    )
