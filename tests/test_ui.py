from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets

from smd_twin_lab.app import build_window
from smd_twin_lab.models import FaultKind, FaultSpec, Scenario
from smd_twin_lab.ui import (
    ControllerBindings,
    MainWindow,
    build_sample_project,
    run_sample_scenario,
)


@pytest.fixture(scope="module")
def application() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


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
