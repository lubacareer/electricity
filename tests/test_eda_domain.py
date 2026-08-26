from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

import smd_twin_lab.eda.repository as repository_module
from smd_twin_lab.eda import (
    BoardSynchronizer,
    BoardTrack,
    CircuitFault,
    CircuitFaultKind,
    CopperLayer,
    DrcEngine,
    EdaPackageError,
    EdaProjectRepository,
    ManualRouter,
    ManualRouteRequest,
    PointNm,
    SchematicCompiler,
    SchematicDocument,
    SchematicJunction,
    SchematicPin,
    SchematicSymbol,
    SchematicWire,
    blank_project,
    divider_project,
    document_to_dict,
    mm,
    new_id,
    simulate_dc,
)


def test_divider_template_is_a_complete_clean_vertical_slice() -> None:
    document = divider_project()

    result = simulate_dc(document)
    report = DrcEngine().check(document, document.revision)

    assert result.success
    assert result.voltage("VCC") == pytest.approx(3.3)
    assert result.voltage("VOUT") == pytest.approx(1.65)
    assert report.clean
    assert report.unconnected_count == 0
    assert len(document.board.outline) == 5
    assert len(document.board.footprints) == 3
    assert len(document.board.tracks) == 7


def test_owned_solver_applies_open_and_wrong_value_faults() -> None:
    document = divider_project()

    open_result = simulate_dc(
        document,
        CircuitFault(CircuitFaultKind.OPEN, reference="R1"),
    )
    wrong_value_result = simulate_dc(
        document,
        CircuitFault(CircuitFaultKind.WRONG_VALUE, reference="R2", value_ohm=20_000),
    )

    assert open_result.success
    assert open_result.voltage("VOUT") == pytest.approx(0, abs=1e-7)
    assert wrong_value_result.success
    assert wrong_value_result.voltage("VOUT") == pytest.approx(2.2)


def test_circuit_fault_rejects_unknown_or_identical_short_nets() -> None:
    document = divider_project()

    unknown = simulate_dc(
        document,
        CircuitFault(CircuitFaultKind.SHORT, net_a="VOUT", net_b="MISSING"),
    )
    identical = simulate_dc(
        document,
        CircuitFault(CircuitFaultKind.SHORT, net_a="VOUT", net_b="VOUT"),
    )

    assert not unknown.success
    assert not identical.success
    assert unknown.issues[-1].code == "circuit.invalid_short_nets"
    assert identical.issues[-1].code == "circuit.invalid_short_nets"


def test_unmodelled_symbols_block_dc_simulation() -> None:
    document = divider_project()
    led = SchematicSymbol(
        new_id(),
        "D1",
        "LED",
        "Device:LED",
        "led",
        PointNm(mm(55), mm(15)),
        (),
    )
    document = document.revised(
        schematic=replace(
            document.schematic,
            symbols=(*document.schematic.symbols, led),
        )
    )

    result = simulate_dc(document)

    assert not result.success
    assert any(issue.code == "circuit.unsupported_symbol" for issue in result.issues)


def test_ground_palette_symbol_defines_the_reference_node() -> None:
    document = divider_project()
    ground = SchematicSymbol(
        new_id(),
        "GND1",
        "GND",
        "power:GND",
        "ground",
        PointNm(mm(10), mm(25)),
        (SchematicPin(new_id(), "1", "GND", PointNm(0, 0)),),
    )
    document = document.revised(
        schematic=replace(
            document.schematic,
            symbols=(*document.schematic.symbols, ground),
            labels=tuple(label for label in document.schematic.labels if label.text != "GND"),
        )
    )

    result = simulate_dc(document)

    assert result.success
    assert not any(issue.code == "circuit.missing_ground" for issue in result.issues)
    assert result.voltage("VOUT") == pytest.approx(1.65)


def _crossing_document(*, with_junction: bool) -> object:
    positions = (
        PointNm(mm(-10), 0),
        PointNm(mm(10), 0),
        PointNm(0, mm(-10)),
        PointNm(0, mm(10)),
    )
    symbols = tuple(
        SchematicSymbol(
            new_id(),
            f"TP{index}",
            "test",
            "smdtwin:TestPoint",
            "test_point",
            position,
            (SchematicPin(new_id(), "1", "TP", PointNm(0, 0), required=False),),
        )
        for index, position in enumerate(positions, start=1)
    )
    schematic = SchematicDocument(
        symbols=symbols,
        wires=(
            SchematicWire(new_id(), (positions[0], positions[1])),
            SchematicWire(new_id(), (positions[2], positions[3])),
        ),
        junctions=((SchematicJunction(new_id(), PointNm(0, 0)),) if with_junction else ()),
    )
    return replace(blank_project("Crossing"), schematic=schematic)


def test_crossing_wires_need_an_explicit_junction() -> None:
    separate = SchematicCompiler().compile(_crossing_document(with_junction=False))
    connected = SchematicCompiler().compile(_crossing_document(with_junction=True))

    assert len([net for net in separate.nets if net.pins]) == 2
    assert len([net for net in connected.nets if net.pins]) == 1


def test_repository_round_trip_is_deterministic_and_preserves_unicode(tmp_path: Path) -> None:
    repository = EdaProjectRepository(autosave_root=tmp_path / "autosaves")
    document = divider_project("Учебная плата")
    first = tmp_path / "first.smdeda"
    second = tmp_path / "second.smdeda"

    repository.save(document, first)
    repository.save(document, second)
    loaded = repository.load(first)
    autosave = repository.autosave(document)

    assert first.read_bytes() == second.read_bytes()
    assert document_to_dict(loaded) == document_to_dict(document)
    assert loaded.name == "Учебная плата"
    assert repository.load(autosave) == document


def test_repository_lists_autosaves_newest_first(tmp_path: Path) -> None:
    repository = EdaProjectRepository(autosave_root=tmp_path / "autosaves")
    first = repository.autosave(blank_project("First"))
    second_document = blank_project("Second")
    second = repository.autosave(second_document)
    first.touch()

    assert repository.list_autosaves() == (first, second)


def test_repository_save_is_atomic_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "project.smdeda"
    destination.write_bytes(b"original")
    repository = EdaProjectRepository(autosave_root=tmp_path / "autosaves")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(repository_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        repository.save(divider_project(), destination)

    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_repository_rejects_corrupt_and_unsupported_packages(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.smdeda"
    corrupt.write_bytes(b"not a zip")
    repository = EdaProjectRepository(autosave_root=tmp_path / "autosaves")

    with pytest.raises(EdaPackageError, match="cannot load"):
        repository.load(corrupt)

    unsupported = tmp_path / "unsupported.smdeda"
    info = ZipInfo("document.json", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    with ZipFile(unsupported, "w") as package:
        package.writestr(
            info,
            b'{"format":"smdeda","package_version":99,"document":{}}',
        )
    with pytest.raises(EdaPackageError, match="unsupported package version"):
        repository.load(unsupported)


def test_repository_rejects_duplicate_design_ids(tmp_path: Path) -> None:
    document = divider_project()
    first, second, *remaining = document.schematic.symbols
    duplicate = replace(second, symbol_id=first.symbol_id)
    invalid = replace(
        document,
        schematic=replace(document.schematic, symbols=(first, duplicate, *remaining)),
    )

    with pytest.raises(ValueError, match="identifiers must be unique"):
        EdaProjectRepository().save(invalid, tmp_path / "invalid.smdeda")


def test_board_synchronizer_adds_snapshot_footprint_and_preserves_routes() -> None:
    document = divider_project()
    removed = document.board.footprints[-1]
    edited = replace(
        document,
        board=replace(document.board, footprints=document.board.footprints[:-1]),
    )

    update = BoardSynchronizer().update_from_schematic(edited)

    restored = next(
        footprint
        for footprint in update.document.board.footprints
        if footprint.symbol_id == removed.symbol_id
    )
    assert update.document.revision == document.revision + 1
    assert restored.footprint_id in update.added_footprint_ids
    assert {pad.net for pad in restored.pads} == {"GND", "VOUT"}
    assert update.document.board.tracks == document.board.tracks


def test_board_synchronizer_is_a_noop_when_board_is_current() -> None:
    document = divider_project()

    update = BoardSynchronizer().update_from_schematic(document)

    assert update.document is document
    assert not update.added_footprint_ids
    assert not update.updated_footprint_ids
    assert not update.removed_footprint_ids
    assert not update.issues


def test_drc_rejects_stale_revision_and_wrong_net_track() -> None:
    document = divider_project()
    stale = DrcEngine().check(document, revision=document.revision + 1)
    bad_track = BoardTrack(
        new_id(),
        "VCC",
        PointNm(mm(4.5), mm(15)),
        PointNm(mm(36), mm(18)),
        mm(0.1),
        CopperLayer.FRONT,
    )
    invalid = replace(
        document,
        board=replace(document.board, tracks=(*document.board.tracks, bad_track)),
    )

    report = DrcEngine().check(invalid)
    codes = {issue.code for issue in report.issues}

    assert stale.stale
    assert stale.issues[0].code == "drc.stale_revision"
    assert "drc.track_too_narrow" in codes
    assert "drc.track_angle" in codes
    assert "drc.wrong_net_termination" in codes
    assert "drc.copper_clearance" in codes


def test_drc_rejects_copper_outside_a_closed_outline() -> None:
    document = divider_project()
    outside = BoardTrack(
        new_id(),
        "VCC",
        PointNm(mm(-5), mm(15)),
        PointNm(mm(-1), mm(15)),
        mm(0.25),
    )
    invalid = replace(
        document,
        board=replace(document.board, tracks=(*document.board.tracks, outside)),
    )

    report = DrcEngine().check(invalid)

    assert "drc.copper_outside_board" in {issue.code for issue in report.issues}


def test_drc_rotates_non_square_footprint_courtyards() -> None:
    document = divider_project()
    first, second, third = document.board.footprints
    first = replace(
        first,
        position=PointNm(mm(10), mm(10)),
        rotation_deg=0,
        courtyard_width_nm=mm(8),
        courtyard_height_nm=mm(2),
    )
    second = replace(
        second,
        position=PointNm(mm(10), mm(13.5)),
        courtyard_width_nm=mm(2),
        courtyard_height_nm=mm(2),
    )
    unrotated = replace(
        document,
        board=replace(
            document.board,
            footprints=(first, second, third),
            tracks=(),
        ),
    )
    rotated = replace(
        unrotated,
        board=replace(
            unrotated.board,
            footprints=(replace(first, rotation_deg=90), second, third),
        ),
    )

    assert "drc.courtyard_overlap" not in {
        issue.code for issue in DrcEngine().check(unrotated).issues
    }
    assert "drc.courtyard_overlap" in {issue.code for issue in DrcEngine().check(rotated).issues}


def test_manual_router_previews_and_commits_one_revision() -> None:
    document = divider_project()
    request = ManualRouteRequest(
        document,
        "VCC",
        (PointNm(mm(4.5), mm(15)), PointNm(mm(4.5), mm(12))),
        expected_revision=document.revision,
    )
    router = ManualRouter()

    preview = router.preview(request)
    commit = router.commit(request)

    assert preview.success
    assert not preview.committed
    assert preview.document.revision == document.revision
    assert commit.success
    assert commit.committed
    assert commit.document.revision == document.revision + 1
    assert len(commit.document.board.tracks) == len(document.board.tracks) + 1
    assert commit.tracks == preview.tracks


@pytest.mark.parametrize(
    ("points", "revision", "code"),
    [
        ((PointNm(0, 0), PointNm(mm(2), mm(1))), 0, "router.invalid_angle"),
        ((PointNm(0, 0), PointNm(mm(1), 0)), 2, "router.stale_revision"),
    ],
)
def test_manual_router_rejects_invalid_gestures(
    points: tuple[PointNm, ...],
    revision: int,
    code: str,
) -> None:
    document = divider_project()
    result = ManualRouter().preview(
        ManualRouteRequest(
            document,
            "VCC",
            points,
            expected_revision=revision,
        )
    )

    assert not result.success
    assert code in {issue.code for issue in result.issues}
    assert result.document is document
