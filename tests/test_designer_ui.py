from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from smd_twin_lab.eda.connectivity import ErcIssue, SchematicCompiler
from smd_twin_lab.eda.library import (
    CatalogRefreshReport,
    LibraryKind,
    LibraryPartSummary,
)
from smd_twin_lab.eda.model import IssueSeverity, PointNm, mm
from smd_twin_lab.localization import LanguageManager
from smd_twin_lab.ui.board_canvas import BoardView
from smd_twin_lab.ui.designer import (
    DesignerWorkspace,
    PcbEditorView,
    SchematicEditorView,
    issue_text,
)


@pytest.fixture(scope="module")
def application() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def language_manager(
    application: QtWidgets.QApplication,
    tmp_path: Path,
) -> LanguageManager:
    settings = QtCore.QSettings(
        str(tmp_path / "designer-settings.ini"),
        QtCore.QSettings.Format.IniFormat,
    )
    manager = LanguageManager(
        application,
        settings=settings,
        system_ui_languages=("en-US",),
    )
    yield manager
    manager.set_language("en")
    manager.close()


@pytest.fixture
def workspace(
    application: QtWidgets.QApplication,
    language_manager: LanguageManager,
) -> DesignerWorkspace:
    widget = DesignerWorkspace(language_manager=language_manager)
    widget.resize(1200, 720)
    widget.show()
    application.processEvents()
    yield widget
    widget.close()


def test_designer_uses_dedicated_editors_and_creates_templates(
    workspace: DesignerWorkspace,
) -> None:
    assert isinstance(workspace.schematic_view, SchematicEditorView)
    assert isinstance(workspace.pcb_view, PcbEditorView)
    assert not isinstance(workspace.schematic_view, BoardView)
    assert not isinstance(workspace.pcb_view, BoardView)
    assert workspace.tabs.count() == 2
    assert workspace.document().teaching.template_id == "blank"

    workspace.new_project("divider")

    document = workspace.document()
    assert document.teaching.template_id == "divider"
    assert [symbol.reference for symbol in document.schematic.symbols] == ["V1", "R1", "R2"]
    assert len(document.board.footprints) == 3
    assert document.board.outline[0] == document.board.outline[-1]
    assert workspace.undo_stack.isClean()


def test_placing_component_and_drag_move_are_single_undo_commands(
    workspace: DesignerWorkspace,
    application: QtWidgets.QApplication,
) -> None:
    workspace.new_project("blank")
    workspace.add_component("resistor", PointNm(mm(20), mm(15)))

    assert workspace.undo_stack.count() == 1
    assert workspace.is_dirty
    symbol = workspace.document().schematic.symbols[0]
    assert symbol.reference == "R1"
    assert len(workspace.document().board.footprints) == 1

    workspace.undo_stack.undo()
    assert workspace.document().schematic.symbols == ()
    assert workspace.document().board.footprints == ()
    assert not workspace.is_dirty
    workspace.undo_stack.redo()

    symbol = workspace.document().schematic.symbols[0]
    original = symbol.position
    before_drag_count = workspace.undo_stack.count()
    item = next(
        scene_item
        for scene_item in workspace.schematic_view.scene().items()
        if scene_item.data(0) == symbol.symbol_id
    )
    start = workspace.schematic_view.mapFromScene(item.scenePos())
    destination_in_view = start + QtCore.QPoint(48, 32)
    QtTest.QTest.mousePress(
        workspace.schematic_view.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        start,
    )
    QtTest.QTest.mouseMove(workspace.schematic_view.viewport(), destination_in_view, 20)
    QtTest.QTest.mouseRelease(
        workspace.schematic_view.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        destination_in_view,
    )
    application.processEvents()

    assert workspace.undo_stack.count() == before_drag_count + 1
    assert workspace.document().schematic.symbols[0].position != original
    workspace.undo_stack.undo()
    assert workspace.document().schematic.symbols[0].position == original


def test_non_square_symbol_and_footprint_render_their_90_degree_rotation(
    workspace: DesignerWorkspace,
) -> None:
    def assert_reference_text_is_upright(
        item: QtWidgets.QGraphicsItem,
        view: QtWidgets.QGraphicsView,
    ) -> None:
        text = next(
            child
            for child in item.childItems()
            if isinstance(child, QtWidgets.QGraphicsSimpleTextItem)
        )
        transform = text.deviceTransform(view.viewportTransform())
        origin = transform.map(QtCore.QPointF())
        x_axis = transform.map(QtCore.QPointF(1, 0))
        assert x_axis.x() > origin.x()
        assert x_axis.y() == pytest.approx(origin.y())

    workspace.new_project("divider")
    symbol = next(item for item in workspace.document().schematic.symbols if item.reference == "R1")
    symbol_item = next(
        item
        for item in workspace.schematic_view.scene().items()
        if item.data(0) == symbol.symbol_id
    )
    symbol_bounds = symbol_item.sceneBoundingRect()

    workspace.select_item(symbol.symbol_id, "symbol")
    workspace.rotate_selection()

    rotated_symbol = next(
        item
        for item in workspace.document().schematic.symbols
        if item.symbol_id == symbol.symbol_id
    )
    symbol_item = next(
        item
        for item in workspace.schematic_view.scene().items()
        if item.data(0) == symbol.symbol_id
    )
    assert rotated_symbol.rotation_deg == 90
    assert symbol_item.rotation() == -90
    assert symbol_item.sceneBoundingRect().width() == pytest.approx(symbol_bounds.height())
    assert symbol_item.sceneBoundingRect().height() == pytest.approx(symbol_bounds.width())
    assert_reference_text_is_upright(symbol_item, workspace.schematic_view)
    rendered_pins = {
        (
            round(child.mapToScene(QtCore.QPointF()).x(), 6),
            round(child.mapToScene(QtCore.QPointF()).y(), 6),
        )
        for child in symbol_item.childItems()
        if isinstance(child, QtWidgets.QGraphicsEllipseItem)
    }
    expected_pins = {
        (
            round(rotated_symbol.pin_position(pin).x_nm / 1_000_000, 6),
            round(-rotated_symbol.pin_position(pin).y_nm / 1_000_000, 6),
        )
        for pin in rotated_symbol.pins
    }
    assert rendered_pins == expected_pins

    footprint = next(
        item for item in workspace.document().board.footprints if item.reference == "R1"
    )
    footprint_item = next(
        item
        for item in workspace.pcb_view.scene().items()
        if item.data(0) == footprint.footprint_id
    )
    footprint_bounds = footprint_item.sceneBoundingRect()

    footprint_item.setSelected(True)
    assert workspace._selected_id == footprint.footprint_id
    workspace.rotate_selection()

    rotated_footprint = next(
        item
        for item in workspace.document().board.footprints
        if item.footprint_id == footprint.footprint_id
    )
    footprint_item = next(
        item
        for item in workspace.pcb_view.scene().items()
        if item.data(0) == footprint.footprint_id
    )
    assert rotated_footprint.rotation_deg == 90
    assert footprint_item.rotation() == -90
    assert footprint_item.sceneBoundingRect().width() == pytest.approx(footprint_bounds.height())
    assert footprint_item.sceneBoundingRect().height() == pytest.approx(footprint_bounds.width())
    assert_reference_text_is_upright(footprint_item, workspace.pcb_view)
    rendered_pads = {
        (
            round(child.mapToScene(QtCore.QPointF()).x(), 6),
            round(child.mapToScene(QtCore.QPointF()).y(), 6),
        )
        for child in footprint_item.childItems()
        if isinstance(child, QtWidgets.QGraphicsRectItem)
    }
    expected_pads = {
        (
            round(rotated_footprint.pad_position(pad).x_nm / 1_000_000, 6),
            round(-rotated_footprint.pad_position(pad).y_nm / 1_000_000, 6),
        )
        for pad in rotated_footprint.pads
    }
    assert rendered_pads == expected_pads


def test_wire_and_45_degree_route_gestures_are_undoable(
    workspace: DesignerWorkspace,
    application: QtWidgets.QApplication,
) -> None:
    workspace.new_project("divider")
    initial_wires = workspace.document().schematic.wires
    initial_tracks = workspace.document().board.tracks
    workspace._set_active_tool("wire")
    wire_start = workspace.schematic_view.mapFromScene(QtCore.QPointF(5, -5))
    wire_end = workspace.schematic_view.mapFromScene(QtCore.QPointF(15, -5))
    QtTest.QTest.mouseClick(
        workspace.schematic_view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=wire_start
    )
    QtTest.QTest.mouseClick(
        workspace.schematic_view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=wire_end
    )
    application.processEvents()
    assert len(workspace.document().schematic.wires) == len(initial_wires) + 1
    assert workspace.undo_stack.count() == 1

    workspace._set_active_tool("route")
    route_start = workspace.pcb_view.mapFromScene(QtCore.QPointF(10, -10))
    route_end = workspace.pcb_view.mapFromScene(QtCore.QPointF(17, -15))
    QtTest.QTest.mouseClick(
        workspace.pcb_view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=route_start
    )
    QtTest.QTest.mouseClick(
        workspace.pcb_view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=route_end
    )
    application.processEvents()
    track = workspace.document().board.tracks[-1]
    assert abs(track.end.x_nm - track.start.x_nm) == abs(track.end.y_nm - track.start.y_nm)
    assert track.net in {"GND", "VIN", "VOUT"}
    assert workspace.undo_stack.count() == 2

    workspace.undo_stack.undo()
    assert workspace.document().board.tracks == initial_tracks
    workspace.undo_stack.undo()
    assert workspace.document().schematic.wires == initial_wires


def test_selection_populates_inspector_and_actions_emit_document(
    workspace: DesignerWorkspace,
) -> None:
    workspace.new_project("divider")
    symbol = workspace.document().schematic.symbols[1]
    workspace.select_item(symbol.symbol_id, "symbol")

    values = {
        workspace.properties_panel.topLevelItem(index).text(1)
        for index in range(workspace.properties_panel.topLevelItemCount())
    }
    assert {"R1", "10k"}.issubset(values)

    validations: list[object] = []
    simulations: list[object] = []
    exports: list[object] = []
    saves: list[object] = []
    workspace.validation_requested.connect(validations.append)
    workspace.simulation_requested.connect(simulations.append)
    workspace.export_requested.connect(exports.append)
    workspace.save_requested.connect(saves.append)

    workspace.validate_design()
    workspace.simulate_design()
    workspace.request_export()
    workspace.save_action.trigger()

    assert validations == [workspace.document()]
    assert simulations == [workspace.document()]
    assert exports == [workspace.document()]
    assert saves == [workspace.document()]
    assert workspace.validation_list.property("errorCount") == 0


def test_language_switch_preserves_editor_state(
    workspace: DesignerWorkspace,
    language_manager: LanguageManager,
    application: QtWidgets.QApplication,
) -> None:
    workspace.new_project("divider")
    workspace.palette_filter.setText("R")
    workspace.tabs.setCurrentWidget(workspace.pcb_view)
    workspace._set_active_tool("route")
    footprint = workspace.document().board.footprints[0]
    workspace.select_item(footprint.footprint_id, "footprint")
    workspace.add_track(PointNm(mm(4), mm(4)), PointNm(mm(12), mm(4)))
    workspace.pcb_view.scale(1.2, 1.2)

    document_identity = workspace.document()
    undo_count = workspace.undo_stack.count()
    selection_id = workspace._selected_id
    transform = QtGui.QTransform(workspace.pcb_view.transform())

    assert language_manager.set_language("ru")
    application.processEvents()

    assert workspace.tabs.tabText(0) == "Принципиальная схема"
    assert workspace.tabs.tabText(1) == "Печатная плата"
    assert workspace.mode_combo.currentText() == "Обучение"
    assert workspace.undo_action.text() == "Отменить: Добавить дорожку"
    assert workspace.document() is document_identity
    assert workspace.undo_stack.count() == undo_count
    assert workspace.palette_filter.text() == "R"
    assert workspace.tabs.currentWidget() is workspace.pcb_view
    assert workspace.pcb_view.active_tool == "route"
    assert selection_id != footprint.footprint_id
    assert workspace._selected_id == selection_id
    assert workspace.pcb_view.transform() == transform
    assert "Два одинаковых резистора" in workspace.teaching_browser.toPlainText()

    assert language_manager.set_language("en")
    application.processEvents()
    assert workspace.tabs.tabText(0) == "Schematic"
    assert workspace.document() is document_identity
    assert workspace.undo_stack.count() == undo_count


def test_designer_styles_use_dark_text_on_light_surfaces(workspace: DesignerWorkspace) -> None:
    stylesheet = workspace.styleSheet()
    assert "color: #102a43" in stylesheet
    assert "background: #ffffff" in stylesheet
    assert "color: #40566b" in stylesheet
    assert workspace.schematic_view.backgroundBrush().color() == QtGui.QColor("#f7f9fc")
    assert workspace.pcb_view.backgroundBrush().color() == QtGui.QColor("#ecf4ee")


def test_installed_library_results_are_searchable_but_honestly_gated(
    workspace: DesignerWorkspace,
) -> None:
    part = LibraryPartSummary(
        identifier="Device:R",
        kind=LibraryKind.SYMBOL,
        library="Device",
        name="R",
        source_path="C:/KiCad/Device.kicad_sym",
    )
    report = CatalogRefreshReport(symbol_count=22_784, footprint_count=15_418, library_count=378)

    workspace.set_library_results((part,), report)
    workspace.palette_filter.setText("Device:R")

    visible = [
        workspace.palette_list.item(index)
        for index in range(workspace.palette_list.count())
        if not workspace.palette_list.item(index).isHidden()
    ]
    assert len(visible) == 1
    assert visible[0].text() == "KiCad SYM  ·  Device:R"
    assert visible[0].data(QtCore.Qt.ItemDataRole.UserRole) is None
    workspace.palette_list.setCurrentItem(visible[0])
    assert not workspace.place_button.isEnabled()
    assert "placement is blocked" in visible[0].toolTip()
    assert "22784 symbols" in workspace.library_status.text()


def test_placement_snaps_and_schematic_wiring_updates_board_nets(
    workspace: DesignerWorkspace,
) -> None:
    workspace.new_project("blank")
    workspace.add_component(
        "voltage_source",
        PointNm(mm(10) + 123_456, mm(20) + 456_789),
    )
    workspace.add_component("resistor", PointNm(mm(25), mm(20)))

    source, resistor = workspace.document().schematic.symbols
    assert source.position == PointNm(mm(10), mm(20))
    assert resistor.position == PointNm(mm(25), mm(20))
    workspace.add_wire(source.pin_position(source.pins[1]), resistor.pin_position(resistor.pins[0]))

    graph = SchematicCompiler().compile(workspace.document())
    assert any(len(net.pins) == 2 for net in graph.nets)
    assert all(
        pad.net for footprint in workspace.document().board.footprints for pad in footprint.pads
    )
    assert workspace.route_net_combo.count() > 0

    workspace.move_symbol(
        resistor.symbol_id,
        resistor.position,
        PointNm(mm(27) + 234_567, mm(21) + 111_111),
    )
    moved = next(
        symbol
        for symbol in workspace.document().schematic.symbols
        if symbol.symbol_id == resistor.symbol_id
    )
    assert moved.position == PointNm(mm(27), mm(21))


def test_russian_retranslates_existing_status_and_validation(
    workspace: DesignerWorkspace,
    language_manager: LanguageManager,
    application: QtWidgets.QApplication,
) -> None:
    workspace.new_project("blank")
    workspace.add_component("resistor", PointNm(mm(20), mm(15)))

    assert language_manager.set_language("ru")
    application.processEvents()

    assert "Размещён компонент R1" in workspace.status_label.text()
    assert language_manager.set_language("en")
    workspace.validate_design()
    assert language_manager.set_language("ru")
    application.processEvents()

    messages = [
        workspace.validation_list.topLevelItem(index).text(0)
        for index in range(workspace.validation_list.topLevelItemCount())
    ]
    assert any(message.startswith("ОШИБКА:") for message in messages)
    assert any("Контур печатной платы" in message for message in messages)
    outline_item = next(
        workspace.validation_list.topLevelItem(index)
        for index in range(workspace.validation_list.topLevelItemCount())
        if "Контур печатной платы" in workspace.validation_list.topLevelItem(index).text(0)
    )
    assert outline_item.toolTip(0) == "Board outline must be a closed polygon"

    assert language_manager.set_language("en")


def test_issue_text_localizes_known_codes_and_preserves_unknown_details(
    language_manager: LanguageManager,
) -> None:
    known = ErcIssue(
        IssueSeverity.ERROR,
        "circuit.missing_ground",
        "Circuit needs the exact technical net GND or 0",
    )
    unknown = ErcIssue(
        IssueSeverity.WARNING,
        "third_party.custom_check",
        "Vendor-specific raw detail",
    )

    assert issue_text(known, language_manager) == "The circuit needs a net labelled GND or 0."
    assert issue_text(unknown, language_manager) == "Vendor-specific raw detail"
    assert language_manager.set_language("ru")
    assert issue_text(known, language_manager) == "Схеме нужна цепь с меткой GND или 0."
    assert issue_text(unknown, language_manager) == "Vendor-specific raw detail"
    assert language_manager.set_language("en")
