from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from smd_twin_lab.app import build_window, create_application
from smd_twin_lab.eda import DcMnaSolver, EdaProjectRepository, NgspiceCircuitEngine
from smd_twin_lab.localization import current_language_manager
from smd_twin_lab.models import FaultKind, FaultSpec, Scenario
from smd_twin_lab.ui import (
    ControllerBindings,
    MainWindow,
    build_sample_project,
    run_sample_scenario,
)


@pytest.fixture(scope="module")
def application(tmp_path_factory: pytest.TempPathFactory) -> QtWidgets.QApplication:
    settings = QtCore.QSettings(
        str(tmp_path_factory.mktemp("ui-settings") / "settings.ini"),
        QtCore.QSettings.Format.IniFormat,
    )
    return create_application(
        [],
        settings=settings,
        system_languages=("en-US",),
    )


def _contrast_ratio(foreground: QtGui.QColor, background: QtGui.QColor) -> float:
    def relative_luminance(color: QtGui.QColor) -> float:
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = tuple(
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        )
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_light_theme_has_readable_text_contrast(
    application: QtWidgets.QApplication,
) -> None:
    palette = application.palette()
    pairs = (
        (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Window),
        (QtGui.QPalette.ColorRole.Text, QtGui.QPalette.ColorRole.Base),
        (QtGui.QPalette.ColorRole.ButtonText, QtGui.QPalette.ColorRole.Button),
        (QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QPalette.ColorRole.Base),
        (QtGui.QPalette.ColorRole.Link, QtGui.QPalette.ColorRole.Base),
        (QtGui.QPalette.ColorRole.LinkVisited, QtGui.QPalette.ColorRole.Base),
    )
    for foreground_role, background_role in pairs:
        assert (
            _contrast_ratio(
                palette.color(foreground_role),
                palette.color(background_role),
            )
            >= 4.5
        )

    window = build_window()
    try:
        stylesheet = window.styleSheet()
        for selector in ("QMenuBar", "QToolBar", "QDockWidget::title", "QLineEdit"):
            assert selector in stylesheet
    finally:
        window.close()


def test_headless_main_window_has_useful_sample_state(application: QtWidgets.QApplication) -> None:
    window = build_window()
    try:
        assert window.project is not None
        assert window.project.name == "USB Sensor/Status Board"
        assert window.project_tree.topLevelItemCount() == 2
        assert window.project_tree.topLevelItem(0).childCount() == len(window.project.components)
        assert "Available" in window.capability_panel.labels["geometry"].text()
        assert window.findChild(QtWidgets.QWidget, "board_canvas") is not None
        assert window.run_button.isEnabled()
    finally:
        window.close()


def test_designer_is_separate_and_runs_circuit_only_simulation(
    application: QtWidgets.QApplication,
    tmp_path: Path,
) -> None:
    window = build_window()
    try:
        imported_project = window.project
        window._designer_spice = NgspiceCircuitEngine(tmp_path / "missing-ngspice.exe")
        window.designer_workspace.new_project("divider")
        window.workspace_tabs.setCurrentWidget(window.designer_workspace)

        assert window.workspace_tabs.count() == 2
        assert window.workspace_tabs.currentWidget() is window.designer_workspace
        assert window.project is imported_project
        assert window.browser_dock.isHidden()
        assert not window.save_report_action.isEnabled()

        window.designer_workspace.validate_design()
        assert window.designer_workspace.validation_list.property("errorCount") == 0
        window.designer_workspace.simulate_design()
        for _ in range(300):
            application.processEvents()
            if not window._designer_jobs:
                break
            QtTest.QTest.qWait(10)

        assert not window._designer_jobs
        assert "VOUT = 1.65 V" in window.designer_workspace.status_label.text()
        assert "firmware" not in window.designer_workspace.status_label.text().casefold()

        window.workspace_tabs.setCurrentWidget(window.test_lab_workspace)
        assert window.project is imported_project
        assert not window.browser_dock.isHidden()
    finally:
        window.close()


def test_nominal_report_updates_waveforms_state_and_summary(
    application: QtWidgets.QApplication,
) -> None:
    project = build_sample_project()
    window = MainWindow(
        ControllerBindings(initial_project=project, scenario_runner=run_sample_scenario)
    )
    scenario = Scenario(
        scenario_id="nominal-test",
        name="Nominal",
        temperature_c=25.0,
        fault=FaultSpec(FaultKind.NONE),
    )
    try:
        report = run_sample_scenario(project, scenario)
        window.set_run_report(report)
        assert window.current_report is report
        assert window.report_banner.text() == "PASS"
        assert window.firmware_state_label.text() == "NORMAL"
        assert len(window.waveform_view.signals) == 3
        assert "sensor_final_v" in window.report_summary.toPlainText()
        assert "STATE=NORMAL" in window.uart_output.toPlainText()
        assert "{'time_s'" not in window.uart_output.toPlainText()
        assert window.timeline_table.topLevelItemCount() >= 3
        assert window.save_report_action.isEnabled()
    finally:
        window.close()


def test_report_can_be_saved_without_dialog(
    application: QtWidgets.QApplication, tmp_path: Path
) -> None:
    project = build_sample_project()
    window = MainWindow(ControllerBindings(initial_project=project))
    scenario = Scenario("save-test", "Nominal", 25.0, FaultSpec(FaultKind.NONE))
    output = tmp_path / "report.json"
    try:
        window.set_run_report(run_sample_scenario(project, scenario))
        assert window.save_report(output)
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["scenario_id"] == "save-test"
        assert payload["passed"] is True
    finally:
        window.close()


def test_real_catalog_switch_preserves_state_and_localizes_report(
    application: QtWidgets.QApplication,
    tmp_path: Path,
) -> None:
    manager = current_language_manager()
    assert manager.set_language("en")
    project = build_sample_project()
    window = MainWindow(
        ControllerBindings(initial_project=project, scenario_runner=run_sample_scenario),
        language_manager=manager,
    )
    try:
        window.temperature_spin.setValue(31.5)
        window.browser_filter.setText("R2")
        window.fault_combo.setCurrentIndex(window.fault_combo.findData(FaultKind.COMPONENT_OPEN))
        window.reference_combo.setCurrentIndex(window.reference_combo.findText("R2"))
        scenario = window._scenario_from_controls()
        window._active_scenario = scenario
        window._show_status(
            "main.status.running_scenario",
            "Running {scenario}",
            parameters={"scenario": scenario.name},
        )
        assert manager.set_language("ru")
        application.processEvents()
        assert window.statusBar().currentMessage() == ("Выполняется сценарий Обрыв компонента")
        assert manager.set_language("en")
        application.processEvents()
        report = run_sample_scenario(project, scenario)
        window.set_run_report(report)
        window.results_tabs.setCurrentIndex(2)
        window.teaching_panel.topic_combo.setCurrentIndex(
            window.teaching_panel.topic_combo.findData("faults")
        )

        project_identity = window.project
        report_identity = window.current_report
        signals = window.waveform_view.signals
        uart = window.uart_output.toPlainText()

        assert manager.set_language("ru")
        application.processEvents()

        assert window.file_menu.title() == "Файл"
        assert window.language_menu.title() == "Язык"
        assert f"{len(project.components)} компонентов" in window.project_title.text()
        assert window.board_canvas.tabs.tabText(0) == "Верхняя"
        assert window.temperature_spin.value() == 31.5
        assert window.browser_filter.text() == "R2"
        assert window._selected_fault_kind() is FaultKind.COMPONENT_OPEN
        assert window.reference_combo.currentText() == "R2"
        assert window.results_tabs.currentIndex() == 2
        assert window.teaching_panel.topic_combo.currentData() == "faults"
        assert window.value_spin.suffix() == " Ω"
        assert window.project is project_identity
        assert window.current_report is report_identity
        assert window.waveform_view.signals == signals
        assert window.uart_output.toPlainText() == uart
        assert "STATE=SENSOR_FAULT" in uart
        assert window.firmware_state_label.text() == "НЕИСПРАВНОСТЬ ДАТЧИКА"
        assert "Вход АЦП" in window.report_summary.toPlainText()
        assert "Задавайте неисправности явно" in window.teaching_panel.browser.toPlainText()

        timeline = [
            (
                window.timeline_table.topLevelItem(index).text(1),
                window.timeline_table.topLevelItem(index).text(2),
            )
            for index in range(window.timeline_table.topLevelItemCount())
        ]
        assert ("Состояние", "Самопроверка при включении") in timeline
        assert ("Неисправность", "Обрыв компонента") in timeline
        assert ("Состояние", "НЕИСПРАВНОСТЬ ДАТЧИКА") in timeline

        output = tmp_path / "russian-report.json"
        assert window.save_report(output)
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["language"] == "ru"
        assert payload["firmware_state"] == "SENSOR_FAULT"
        assert "sensor_final_v" in payload["measurements"]
        assert "АЦП" in payload["explanations"][0]
        assert any(
            event.get("kind") == "uart" and str(event.get("message", "")).startswith("ADC=")
            for event in payload["timeline"]
        )

        assert manager.set_language("en")
        application.processEvents()
        assert window.file_menu.title() == "File"
        assert window.temperature_spin.value() == 31.5
        assert window.browser_filter.text() == "R2"
        assert window.results_tabs.currentIndex() == 2
        assert window.teaching_panel.topic_combo.currentData() == "faults"
        assert window.current_report is report_identity
        assert "What this means" in window.report_summary.toPlainText()
    finally:
        manager.set_language("en")
        window.close()


def test_scenario_runner_completes_off_ui_thread(
    application: QtWidgets.QApplication,
) -> None:
    project = build_sample_project()
    window = MainWindow(
        ControllerBindings(initial_project=project, scenario_runner=run_sample_scenario)
    )
    event_loop = QtCore.QEventLoop()
    window.report_changed.connect(event_loop.quit)
    QtCore.QTimer.singleShot(3000, event_loop.quit)
    try:
        window.run_scenario()
        assert not window.run_button.isEnabled()
        event_loop.exec()
        application.processEvents()
        assert window.current_report is not None
        assert window.current_report.passed
        assert window.run_button.isEnabled()
    finally:
        window.close()


def test_fault_controls_target_and_highlight_the_thermistor(
    application: QtWidgets.QApplication,
) -> None:
    project = build_sample_project()
    window = MainWindow(
        ControllerBindings(initial_project=project, scenario_runner=run_sample_scenario)
    )
    try:
        assert window.reference_combo.currentText() == "R2"
        assert window.value_spin.value() == 47_000.0
        fault_index = window.fault_combo.findData(FaultKind.COMPONENT_OPEN)
        window.fault_combo.setCurrentIndex(fault_index)

        scenario = window._scenario_from_controls()
        window._active_scenario = scenario
        window.set_run_report(run_sample_scenario(project, scenario))

        current = window.project_tree.currentItem()
        assert current is not None
        assert current.text(0) == "R2"
        selected = window.board_canvas.top_view.scene().selectedItems()
        assert any(item.data(1) == "R2" for item in selected)
    finally:
        window.close()


def test_project_import_runs_off_the_ui_thread(
    application: QtWidgets.QApplication,
    monkeypatch,
    tmp_path: Path,
) -> None:
    initial = build_sample_project()
    loaded = replace(initial, name="Asynchronously loaded project")
    main_thread = threading.get_ident()
    loader_threads: list[int] = []

    def loader(_path: Path):
        loader_threads.append(threading.get_ident())
        return loaded

    window = MainWindow(
        ControllerBindings(
            initial_project=initial,
            project_loader=loader,
            scenario_runner=run_sample_scenario,
        )
    )
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(tmp_path / "board.kicad_pro"), ""),
    )
    event_loop = QtCore.QEventLoop()
    window.project_changed.connect(event_loop.quit)
    QtCore.QTimer.singleShot(3000, event_loop.quit)
    try:
        window._choose_project()
        assert not window.open_action.isEnabled()
        event_loop.exec()
        application.processEvents()

        assert window.project is loaded
        assert loader_threads and loader_threads[0] != main_thread
        assert window.open_action.isEnabled()
    finally:
        window.close()


def test_designer_discards_simulation_result_from_another_document(
    application: QtWidgets.QApplication,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingEngine:
        available = True

        def run(self, circuit):
            started.set()
            assert release.wait(3)
            return DcMnaSolver().solve(circuit)

    window = build_window()
    window._designer_spice = BlockingEngine()
    try:
        window.designer_workspace.new_project("divider")
        window.designer_workspace.simulate_design()
        assert started.wait(3)

        window.designer_workspace.new_project("blank")
        release.set()
        for _ in range(300):
            application.processEvents()
            if not window._designer_jobs:
                break
            QtTest.QTest.qWait(10)

        assert not window._designer_jobs
        assert "stale result was discarded" in window.designer_workspace.status_label.text()
        assert "VOUT" not in window.designer_workspace.status_label.text()
    finally:
        release.set()
        window.close()


def test_test_lab_completion_does_not_enable_report_save_in_designer(
    application: QtWidgets.QApplication,
) -> None:
    project = build_sample_project()
    started = threading.Event()
    release = threading.Event()

    def blocking_runner(current_project, scenario):
        started.set()
        assert release.wait(3)
        return run_sample_scenario(current_project, scenario)

    window = MainWindow(
        ControllerBindings(initial_project=project, scenario_runner=blocking_runner)
    )
    try:
        window.run_scenario()
        assert started.wait(3)
        window.workspace_tabs.setCurrentWidget(window.designer_workspace)
        release.set()
        for _ in range(300):
            application.processEvents()
            if not window._jobs:
                break
            QtTest.QTest.qWait(10)

        assert window.current_report is not None
        assert not window.save_report_action.isEnabled()
    finally:
        release.set()
        window.close()


def test_dirty_designer_requires_confirmation_before_new_project(
    application: QtWidgets.QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = build_window()
    try:
        window.designer_workspace.new_project("divider")
        window.designer_workspace.add_component("resistor")
        original_id = window.designer_workspace.document().project_id

        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            "warning",
            lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        window._new_designer_project("blank")
        assert window.designer_workspace.document().project_id == original_id

        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            "warning",
            lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.Discard,
        )
        window._new_designer_project("blank")
        assert window.designer_workspace.document().project_id != original_id
    finally:
        window.close()


def test_recovered_autosave_is_detached_from_hidden_recovery_path(
    application: QtWidgets.QApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = EdaProjectRepository(autosave_root=tmp_path / "autosaves")
    autosave = repository.autosave(repository.create("Recovered divider", template_id="divider"))
    window = build_window()
    window._designer_repository = repository
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(autosave), ""),
    )
    try:
        window._recover_designer_autosave()
        assert window.designer_workspace.document().name == "Recovered divider"
        assert window.designer_workspace.document_path is None
    finally:
        window.close()


def test_catalog_refresh_stays_disabled_until_all_designer_jobs_finish(
    application: QtWidgets.QApplication,
) -> None:
    window = build_window()
    first = object()
    second = object()
    window._designer_jobs.update((first, second))
    window.designer_workspace.refresh_libraries_action.setEnabled(False)
    try:
        window._finish_designer_task(first)
        assert not window.designer_workspace.refresh_libraries_action.isEnabled()

        window._finish_designer_task(second)
        assert window.designer_workspace.refresh_libraries_action.isEnabled()
    finally:
        window._designer_jobs.clear()
        window.close()
