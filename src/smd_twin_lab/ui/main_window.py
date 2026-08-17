"""Main desktop workspace for project inspection and educational scenarios."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Protocol

from PySide6 import QtCore, QtGui, QtWidgets

from ..models import (
    Capability,
    CapabilityStatus,
    Component,
    FaultKind,
    FaultSpec,
    ImportedProject,
    Net,
    RunReport,
    Scenario,
)
from .board_canvas import BoardCanvas
from .sample_data import build_sample_project, run_sample_scenario
from .teaching import TeachingPanel
from .waveform import WaveformView


class ScenarioRunner(Protocol):
    def __call__(self, project: ImportedProject, scenario: Scenario) -> RunReport: ...


ProjectLoader = Callable[[Path], ImportedProject]
ScenarioGate = Callable[[ImportedProject], tuple[bool, str]]


@dataclass(slots=True)
class ControllerBindings:
    """Runtime integrations supplied by the application composition root."""

    initial_project: ImportedProject | None = None
    project_loader: ProjectLoader | None = None
    scenario_runner: ScenarioRunner | object | None = None
    scenario_gate: ScenarioGate | None = None


class _TaskSignals(QtCore.QObject):
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()


class _ScenarioTask(QtCore.QRunnable):
    def __init__(
        self,
        runner: ScenarioRunner | object,
        project: ImportedProject,
        scenario: Scenario,
    ) -> None:
        super().__init__()
        self.runner = runner
        self.project = project
        self.scenario = scenario
        self.signals = _TaskSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            if callable(self.runner):
                report = self.runner(self.project, self.scenario)
            else:
                run_method = getattr(self.runner, "run", None)
                if not callable(run_method):
                    raise TypeError(
                        "Scenario runner must be callable or expose run(project, scenario)"
                    )
                report = run_method(self.project, self.scenario)
            if not isinstance(report, RunReport):
                raise TypeError("Scenario runner returned an unsupported result")
            self.signals.succeeded.emit(report)
        except Exception:  # noqa: BLE001 - worker errors become user-visible diagnostics
            self.signals.failed.emit(traceback.format_exc(limit=8))
        finally:
            self.signals.finished.emit()


class _ProjectImportTask(QtCore.QRunnable):
    def __init__(self, loader: ProjectLoader, path: Path) -> None:
        super().__init__()
        self.loader = loader
        self.path = path
        self.signals = _TaskSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            project = self.loader(self.path)
            if not isinstance(project, ImportedProject):
                raise TypeError("Project loader returned an unsupported result")
            self.signals.succeeded.emit(project)
        except Exception:  # noqa: BLE001 - worker errors become user-visible diagnostics
            self.signals.failed.emit(traceback.format_exc(limit=8))
        finally:
            self.signals.finished.emit()


class CapabilityPanel(QtWidgets.QWidget):
    """Compact report that distinguishes missing tools from project errors."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.labels: dict[str, QtWidgets.QLabel] = {}
        layout = QtWidgets.QFormLayout(self)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, title in (
            ("geometry", "Board geometry"),
            ("circuit", "Circuit simulation"),
            ("firmware", "Firmware model"),
            ("hardware", "Hardware target"),
        ):
            label = QtWidgets.QLabel("Not inspected")
            label.setWordWrap(True)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.labels[key] = label
            layout.addRow(title, label)
        self.diagnostics = QtWidgets.QLabel()
        self.diagnostics.setWordWrap(True)
        layout.addRow("Import notes", self.diagnostics)

    def set_project(self, project: ImportedProject | None) -> None:
        if project is None:
            for label in self.labels.values():
                label.setText("Not inspected")
                label.setStyleSheet("")
            self.diagnostics.setText("No project loaded")
            return
        capabilities = project.capabilities
        for key in self.labels:
            self._set_capability(self.labels[key], getattr(capabilities, key))
        if project.diagnostics:
            counts: dict[str, int] = {}
            for diagnostic in project.diagnostics:
                counts[diagnostic.severity.value] = counts.get(diagnostic.severity.value, 0) + 1
            self.diagnostics.setText(
                ", ".join(f"{count} {severity}" for severity, count in sorted(counts.items()))
            )
        else:
            self.diagnostics.setText("No import diagnostics")

    @staticmethod
    def _set_capability(label: QtWidgets.QLabel, capability: Capability) -> None:
        palette = {
            CapabilityStatus.AVAILABLE: ("Available", "#25734b", "#ddf6e8"),
            CapabilityStatus.UNAVAILABLE: ("Unavailable", "#795c16", "#fff2c7"),
            CapabilityStatus.INVALID: ("Invalid", "#8c3434", "#ffe1e1"),
        }
        title, background, foreground = palette[capability.status]
        label.setText(f"<b>{escape(title)}</b> — {escape(capability.detail)}")
        label.setStyleSheet(
            f"background: {background}; color: {foreground}; padding: 5px; border-radius: 4px"
        )


class MainWindow(QtWidgets.QMainWindow):
    """Inspect a normalized PCB project and run injected scenarios asynchronously."""

    project_changed = QtCore.Signal(object)
    report_changed = QtCore.Signal(object)

    def __init__(
        self,
        bindings: ControllerBindings | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        bindings = bindings or ControllerBindings(
            initial_project=build_sample_project(),
            scenario_runner=run_sample_scenario,
        )
        self._project_loader = bindings.project_loader
        self._scenario_runner = bindings.scenario_runner or run_sample_scenario
        self._scenario_gate = bindings.scenario_gate
        self.project: ImportedProject | None = None
        self.current_report: RunReport | None = None
        self._active_scenario: Scenario | None = None
        self._jobs: set[_ScenarioTask] = set()
        self._import_jobs: set[_ProjectImportTask] = set()
        self._scenario_enabled = False

        self.setObjectName("main_window")
        self.setWindowTitle("SMD Twin Lab")
        self.resize(1440, 900)
        self.setMinimumSize(980, 650)
        self._build_actions()
        self._build_workspace()
        self._apply_style()
        self.statusBar().showMessage("Ready — external tools are optional")
        self.set_project(bindings.initial_project or build_sample_project())

    def _build_actions(self) -> None:
        self.open_action = QtGui.QAction("Open project…", self)
        self.open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self._choose_project)
        self.save_report_action = QtGui.QAction("Save report…", self)
        self.save_report_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        self.save_report_action.setEnabled(False)
        self.save_report_action.triggered.connect(self._choose_report_path)
        self.exit_action = QtGui.QAction("Exit", self)
        self.exit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.save_report_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        toolbar = self.addToolBar("Project")
        toolbar.setMovable(False)
        toolbar.addAction(self.open_action)
        toolbar.addAction(self.save_report_action)

    def _build_workspace(self) -> None:
        self.project_title = QtWidgets.QLabel("No project")
        self.project_title.setObjectName("project_title")
        self.project_title.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.scenario_controls = self._build_scenario_controls()
        self.board_canvas = BoardCanvas()
        self.board_canvas.setObjectName("board_canvas")
        self.board_canvas.component_selected.connect(self._select_component_in_tree)

        self.results_tabs = QtWidgets.QTabWidget()
        self.waveform_view = WaveformView()
        self.waveform_view.setObjectName("waveform_view")
        self.results_tabs.addTab(self.waveform_view, "Waveforms")
        self.results_tabs.addTab(self._build_run_summary(), "Run report")
        self.results_tabs.addTab(self._build_firmware_panel(), "Firmware & timeline")

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        board_group = QtWidgets.QGroupBox("Board view")
        board_layout = QtWidgets.QVBoxLayout(board_group)
        board_layout.addWidget(self.board_canvas)
        splitter.addWidget(board_group)
        splitter.addWidget(self.results_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes((390, 260))

        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.addWidget(self.project_title)
        central_layout.addWidget(self.scenario_controls)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self._build_browser_dock()
        self._build_inspector_dock()
        self._build_teaching_dock()

    def _build_scenario_controls(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Scenario")
        self.temperature_spin = QtWidgets.QDoubleSpinBox()
        self.temperature_spin.setObjectName("temperature_spin")
        self.temperature_spin.setRange(-55.0, 150.0)
        self.temperature_spin.setValue(25.0)
        self.temperature_spin.setSuffix(" °C")
        self.temperature_spin.setDecimals(1)

        self.fault_combo = QtWidgets.QComboBox()
        self.fault_combo.setObjectName("fault_combo")
        labels = {
            FaultKind.NONE: "Nominal (no fault)",
            FaultKind.COMPONENT_OPEN: "Component open",
            FaultKind.NET_SHORT: "Short two nets",
            FaultKind.WRONG_VALUE: "Wrong component value",
            FaultKind.REVERSED_POLARITY: "Reversed polarity",
            FaultKind.INTERMITTENT: "Intermittent open",
        }
        for kind in FaultKind:
            self.fault_combo.addItem(labels[kind], kind)

        self.reference_combo = QtWidgets.QComboBox()
        self.reference_combo.setObjectName("fault_reference_combo")
        self.net_a_combo = QtWidgets.QComboBox()
        self.net_b_combo = QtWidgets.QComboBox()
        self.value_spin = QtWidgets.QDoubleSpinBox()
        self.value_spin.setRange(0.001, 1.0e12)
        self.value_spin.setValue(47_000.0)
        self.value_spin.setDecimals(3)
        self.value_spin.setSuffix(" ohm")
        self.value_spin.setToolTip("Replacement resistance in ohms")
        self.start_spin = QtWidgets.QDoubleSpinBox()
        self.start_spin.setRange(0.0, 3600.0)
        self.start_spin.setValue(0.035)
        self.start_spin.setSuffix(" s")
        self.start_spin.setDecimals(4)
        self.duration_spin = QtWidgets.QDoubleSpinBox()
        self.duration_spin.setRange(0.0001, 3600.0)
        self.duration_spin.setValue(0.02)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setDecimals(4)

        self.run_button = QtWidgets.QPushButton("Run scenario")
        self.run_button.setObjectName("run_scenario_button")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.run_scenario)
        self.run_progress = QtWidgets.QProgressBar()
        self.run_progress.setRange(0, 1)
        self.run_progress.setValue(0)
        self.run_progress.setTextVisible(False)
        self.run_progress.setMaximumWidth(90)

        layout = QtWidgets.QGridLayout(group)
        layout.addWidget(QtWidgets.QLabel("Temperature"), 0, 0)
        layout.addWidget(self.temperature_spin, 0, 1)
        layout.addWidget(QtWidgets.QLabel("Fault"), 0, 2)
        layout.addWidget(self.fault_combo, 0, 3)
        layout.addWidget(QtWidgets.QLabel("Component"), 0, 4)
        layout.addWidget(self.reference_combo, 0, 5)
        layout.addWidget(QtWidgets.QLabel("Net A"), 1, 0)
        layout.addWidget(self.net_a_combo, 1, 1)
        layout.addWidget(QtWidgets.QLabel("Net B"), 1, 2)
        layout.addWidget(self.net_b_combo, 1, 3)
        layout.addWidget(QtWidgets.QLabel("Resistance"), 1, 4)
        layout.addWidget(self.value_spin, 1, 5)
        layout.addWidget(QtWidgets.QLabel("Start"), 2, 0)
        layout.addWidget(self.start_spin, 2, 1)
        layout.addWidget(QtWidgets.QLabel("Duration"), 2, 2)
        layout.addWidget(self.duration_spin, 2, 3)
        layout.addWidget(self.run_progress, 2, 4)
        layout.addWidget(self.run_button, 2, 5)
        layout.setColumnStretch(3, 1)
        self.fault_combo.currentIndexChanged.connect(self._update_fault_controls)
        self._update_fault_controls()
        return group

    def _build_run_summary(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        self.report_banner = QtWidgets.QLabel("No run yet")
        self.report_banner.setObjectName("report_banner")
        self.report_banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.report_banner.setMinimumHeight(38)
        self.report_summary = QtWidgets.QTextBrowser()
        self.report_summary.setObjectName("report_summary")
        self.report_summary.setHtml(
            "<h3>Ready for a scenario</h3>"
            "<p>Run nominal first, then introduce one fault at a time.</p>"
        )
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(self.report_banner)
        layout.addWidget(self.report_summary, 1)
        return widget

    def _build_firmware_panel(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        self.firmware_state_label = QtWidgets.QLabel("NOT RUN")
        self.firmware_state_label.setObjectName("firmware_state_label")
        self.firmware_state_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.uart_output = QtWidgets.QPlainTextEdit()
        self.uart_output.setObjectName("uart_output")
        self.uart_output.setReadOnly(True)
        self.uart_output.setPlaceholderText("UART messages will appear here")
        self.timeline_table = QtWidgets.QTreeWidget()
        self.timeline_table.setObjectName("timeline_table")
        self.timeline_table.setHeaderLabels(("Time", "Type", "Event"))
        self.timeline_table.setRootIsDecorated(False)
        self.timeline_table.setAlternatingRowColors(True)
        self.timeline_table.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        state_group = QtWidgets.QGroupBox("Firmware state")
        state_layout = QtWidgets.QVBoxLayout(state_group)
        state_layout.addWidget(self.firmware_state_label)
        uart_group = QtWidgets.QGroupBox("UART")
        uart_layout = QtWidgets.QVBoxLayout(uart_group)
        uart_layout.addWidget(self.uart_output)
        left = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        left.addWidget(state_group)
        left.addWidget(uart_group)
        left.setStretchFactor(1, 1)
        timeline_group = QtWidgets.QGroupBox("Timeline")
        timeline_layout = QtWidgets.QVBoxLayout(timeline_group)
        timeline_layout.addWidget(self.timeline_table)
        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(timeline_group)
        split.setStretchFactor(1, 2)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(split)
        return widget

    def _build_browser_dock(self) -> None:
        dock = QtWidgets.QDockWidget("Project browser", self)
        dock.setObjectName("project_browser_dock")
        browser_widget = QtWidgets.QWidget()
        self.browser_filter = QtWidgets.QLineEdit()
        self.browser_filter.setPlaceholderText("Filter components and nets…")
        self.browser_filter.setClearButtonEnabled(True)
        self.browser_filter.textChanged.connect(self._filter_browser)
        self.project_tree = QtWidgets.QTreeWidget()
        self.project_tree.setObjectName("project_tree")
        self.project_tree.setHeaderLabels(("Item", "Value / pins"))
        self.project_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.project_tree.header().setStretchLastSection(True)
        self.project_tree.currentItemChanged.connect(self._inspect_tree_item)
        layout = QtWidgets.QVBoxLayout(browser_widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.browser_filter)
        layout.addWidget(self.project_tree)
        dock.setWidget(browser_widget)
        dock.setMinimumWidth(240)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_inspector_dock(self) -> None:
        dock = QtWidgets.QDockWidget("Inspector", self)
        dock.setObjectName("inspector_dock")
        tabs = QtWidgets.QTabWidget()
        self.inspector = QtWidgets.QTextBrowser()
        self.inspector.setObjectName("item_inspector")
        self.inspector.setHtml("<h3>Selection</h3><p>Select a component or net.</p>")
        self.capability_panel = CapabilityPanel()
        self.capability_panel.setObjectName("capability_panel")
        tabs.addTab(self.inspector, "Component / net")
        tabs.addTab(self.capability_panel, "Capabilities")
        dock.setWidget(tabs)
        dock.setMinimumWidth(330)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _build_teaching_dock(self) -> None:
        dock = QtWidgets.QDockWidget("Learn while testing", self)
        dock.setObjectName("teaching_dock")
        self.teaching_panel = TeachingPanel()
        self.teaching_panel.setObjectName("teaching_panel")
        dock.setWidget(self.teaching_panel)
        dock.setMinimumWidth(330)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.tabifyDockWidget(self.findChild(QtWidgets.QDockWidget, "inspector_dock"), dock)

    def set_project(self, project: ImportedProject) -> None:
        self.project = project
        self.current_report = None
        self._active_scenario = None
        self.project_title.setText(
            f"{project.name}  •  Variant: {project.variant}  •  "
            f"{len(project.components)} components / {len(project.nets)} nets"
        )
        self.board_canvas.set_project(project)
        self.capability_panel.set_project(project)
        self._populate_browser(project)
        self._populate_scenario_targets(project)
        self._clear_results()
        self.save_report_action.setEnabled(False)
        if self._scenario_gate is None:
            self._scenario_enabled, detail = True, "Run a scenario"
        else:
            self._scenario_enabled, detail = self._scenario_gate(project)
        self.run_button.setEnabled(self._scenario_enabled and not self._import_jobs)
        self.run_button.setToolTip(detail)
        self.project_changed.emit(project)
        self.statusBar().showMessage(f"Loaded {project.name}", 5000)

    @QtCore.Slot()
    def run_scenario(self) -> None:
        if self.project is None or self._jobs or self._import_jobs or not self._scenario_enabled:
            return
        scenario = self._scenario_from_controls()
        self._active_scenario = scenario
        self._select_scenario_target(scenario)
        task = _ScenarioTask(self._scenario_runner, self.project, scenario)
        self._jobs.add(task)
        task.signals.succeeded.connect(self.set_run_report)
        task.signals.failed.connect(self._show_run_error)
        task.signals.finished.connect(lambda: self._finish_task(task))
        self.run_button.setEnabled(False)
        self.run_button.setText("Running…")
        self.run_progress.setRange(0, 0)
        self.statusBar().showMessage(f"Running {scenario.name}")
        self.teaching_panel.show_topic(
            "simulation",
            "The scenario runs outside the UI thread. Compare its traces with nominal behavior.",
        )
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def set_run_report(self, report: object) -> None:
        if not isinstance(report, RunReport):
            self._show_run_error("The controller returned an invalid report object.")
            return
        self.current_report = report
        self.save_report_action.setEnabled(True)
        self.waveform_view.set_signals(report.signals)
        self._render_report_summary(report)
        self._render_firmware(report)
        if self._active_scenario is not None:
            self._select_scenario_target(self._active_scenario)
        self.results_tabs.setCurrentIndex(0)
        self.report_changed.emit(report)
        status = "passed" if report.passed else "needs attention"
        self.statusBar().showMessage(f"Scenario complete: {status}", 8000)

    def save_report(self, path: Path) -> bool:
        if self.current_report is None:
            return False
        try:
            self.current_report.write_json(path)
        except OSError as exc:
            self.statusBar().showMessage(f"Could not save report: {exc}", 10000)
            return False
        self.statusBar().showMessage(f"Saved report to {path}", 8000)
        return True

    def _populate_browser(self, project: ImportedProject) -> None:
        self.project_tree.clear()
        component_root = QtWidgets.QTreeWidgetItem(("Components", str(len(project.components))))
        component_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
        for component in sorted(project.components, key=lambda item: item.reference):
            item = QtWidgets.QTreeWidgetItem(
                (component.reference, component.value or component.footprint)
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "component")
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, component.reference)
            component_root.addChild(item)
        net_root = QtWidgets.QTreeWidgetItem(("Nets", str(len(project.nets))))
        net_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
        for net in sorted(project.nets, key=lambda item: item.name.casefold()):
            item = QtWidgets.QTreeWidgetItem((net.name, f"{len(net.pins)} pins"))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "net")
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, net.name)
            net_root.addChild(item)
        self.project_tree.addTopLevelItems((component_root, net_root))
        component_root.setExpanded(True)
        net_root.setExpanded(True)

    def _populate_scenario_targets(self, project: ImportedProject) -> None:
        selected_reference = self.reference_combo.currentText()
        self.reference_combo.clear()
        self.reference_combo.addItems(component.reference for component in project.components)
        index = self.reference_combo.findText(selected_reference)
        if index < 0:
            for preferred in ("RT1", "R2"):
                index = self.reference_combo.findText(preferred)
                if index >= 0:
                    break
        if index >= 0:
            self.reference_combo.setCurrentIndex(index)
        net_names = [net.name for net in project.nets]
        self.net_a_combo.clear()
        self.net_b_combo.clear()
        self.net_a_combo.addItems(net_names)
        self.net_b_combo.addItems(net_names)
        sensor_index = self.net_a_combo.findText("ADC_SENSE")
        if sensor_index < 0:
            sensor_index = self.net_a_combo.findText("SENSOR")
        ground_index = self.net_b_combo.findText("GND")
        if sensor_index >= 0:
            self.net_a_combo.setCurrentIndex(sensor_index)
        if ground_index >= 0:
            self.net_b_combo.setCurrentIndex(ground_index)
        elif len(net_names) > 1:
            self.net_b_combo.setCurrentIndex(1)

    def _select_scenario_target(self, scenario: Scenario) -> None:
        if scenario.fault.reference:
            self.board_canvas.select_reference(scenario.fault.reference)
            self._select_component_in_tree(scenario.fault.reference)
        elif scenario.fault.net_a:
            self._select_net_in_tree(scenario.fault.net_a)

    def _scenario_from_controls(self) -> Scenario:
        kind = self._selected_fault_kind()
        component_fault = kind in {
            FaultKind.COMPONENT_OPEN,
            FaultKind.WRONG_VALUE,
            FaultKind.REVERSED_POLARITY,
            FaultKind.INTERMITTENT,
        }
        fault = FaultSpec(
            kind=kind,
            reference=self.reference_combo.currentText() if component_fault else None,
            net_a=self.net_a_combo.currentText() if kind == FaultKind.NET_SHORT else None,
            net_b=self.net_b_combo.currentText() if kind == FaultKind.NET_SHORT else None,
            value=self.value_spin.value() if kind == FaultKind.WRONG_VALUE else None,
            start_s=self.start_spin.value() if kind == FaultKind.INTERMITTENT else None,
            duration_s=self.duration_spin.value() if kind == FaultKind.INTERMITTENT else None,
        )
        target = fault.reference or fault.net_a or "baseline"
        return Scenario(
            scenario_id=f"ui-{kind.value}-{target}",
            name=self.fault_combo.currentText(),
            temperature_c=self.temperature_spin.value(),
            fault=fault,
        )

    @QtCore.Slot()
    def _update_fault_controls(self) -> None:
        kind = self._selected_fault_kind()
        component_fault = kind in {
            FaultKind.COMPONENT_OPEN,
            FaultKind.WRONG_VALUE,
            FaultKind.REVERSED_POLARITY,
            FaultKind.INTERMITTENT,
        }
        self.reference_combo.setEnabled(component_fault)
        self.net_a_combo.setEnabled(kind == FaultKind.NET_SHORT)
        self.net_b_combo.setEnabled(kind == FaultKind.NET_SHORT)
        self.value_spin.setEnabled(kind == FaultKind.WRONG_VALUE)
        self.start_spin.setEnabled(kind == FaultKind.INTERMITTENT)
        self.duration_spin.setEnabled(kind == FaultKind.INTERMITTENT)
        if kind != FaultKind.NONE and hasattr(self, "teaching_panel"):
            self.teaching_panel.show_topic(
                "faults",
                "Run nominal first. Change one fault at a time so the cause remains observable.",
            )

    def _selected_fault_kind(self) -> FaultKind:
        value = self.fault_combo.currentData()
        return value if isinstance(value, FaultKind) else FaultKind(str(value))

    @QtCore.Slot(QtWidgets.QTreeWidgetItem, QtWidgets.QTreeWidgetItem)
    def _inspect_tree_item(
        self,
        current: QtWidgets.QTreeWidgetItem | None,
        previous: QtWidgets.QTreeWidgetItem | None,
    ) -> None:
        del previous
        if current is None or self.project is None:
            return
        kind = current.data(0, QtCore.Qt.ItemDataRole.UserRole)
        name = current.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
        if kind == "component":
            component = next(
                (item for item in self.project.components if item.reference == name), None
            )
            if component is not None:
                self._show_component(component)
                self.board_canvas.select_reference(component.reference)
        elif kind == "net":
            net = next((item for item in self.project.nets if item.name == name), None)
            if net is not None:
                self._show_net(net)

    def _show_component(self, component: Component) -> None:
        fields = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in sorted(component.fields.items())
        )
        nets = ", ".join(escape(net) for net in component.nets) or "Unknown"
        placement = (
            f"{component.x_mm:.2f}, {component.y_mm:.2f} mm"
            if component.x_mm is not None and component.y_mm is not None
            else "Not placed"
        )
        self.inspector.setHtml(
            f"<h2>{escape(component.reference)}</h2>"
            "<table cellspacing='6'>"
            f"<tr><th>Value</th><td>{escape(component.value or '—')}</td></tr>"
            f"<tr><th>Footprint</th><td>{escape(component.footprint or '—')}</td></tr>"
            f"<tr><th>Side</th><td>{escape(component.side.value)}</td></tr>"
            f"<tr><th>Placement</th><td>{escape(placement)}</td></tr>"
            f"<tr><th>Rotation</th><td>{component.rotation_deg:.1f}°</td></tr>"
            f"<tr><th>Nets</th><td>{nets}</td></tr>{fields}</table>"
            f"<p><b>Assembly:</b> {'SMD' if component.is_smd else 'through-hole or unknown'}, "
            f"{'in BOM' if component.in_bom else 'not in BOM'}.</p>"
        )
        self.teaching_panel.show_topic(
            "aoi",
            f"{component.reference}: connect its visible placement to its nets and "
            "simulated behavior.",
        )

    def _show_net(self, net: Net) -> None:
        pins = "".join(
            f"<li><b>{escape(pin.reference)}</b> pin {escape(pin.pin)}</li>" for pin in net.pins
        )
        self.inspector.setHtml(
            f"<h2>Net {escape(net.name)}</h2>"
            f"<p>{len(net.pins)} connected pin(s)</p><ul>{pins or '<li>No pin data</li>'}</ul>"
            "<p>A net is an electrical connection. AOI sees joints; simulation predicts how "
            "the connection changes voltage and current.</p>"
        )
        self.teaching_panel.show_topic(
            "simulation",
            f"Inspect {net.name} as both a physical copper connection and a simulated signal.",
        )

    @QtCore.Slot(str)
    def _select_component_in_tree(self, reference: str) -> None:
        root = self.project_tree.topLevelItem(0)
        if root is None:
            return
        for index in range(root.childCount()):
            item = root.child(index)
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) == reference:
                self.project_tree.setCurrentItem(item)
                return

    def _select_net_in_tree(self, name: str) -> None:
        root = self.project_tree.topLevelItem(1)
        if root is None:
            return
        for index in range(root.childCount()):
            item = root.child(index)
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) == name:
                self.project_tree.setCurrentItem(item)
                return

    @QtCore.Slot(str)
    def _filter_browser(self, text: str) -> None:
        query = text.strip().casefold()
        for root_index in range(self.project_tree.topLevelItemCount()):
            root = self.project_tree.topLevelItem(root_index)
            visible_children = 0
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                matches = not query or query in f"{child.text(0)} {child.text(1)}".casefold()
                child.setHidden(not matches)
                visible_children += int(matches)
            root.setHidden(bool(query) and visible_children == 0)

    def _render_report_summary(self, report: RunReport) -> None:
        if report.infrastructure_error:
            title, color = "INFRASTRUCTURE ERROR", "#9b5f18"
        elif report.passed:
            title, color = "PASS", "#25734b"
        else:
            title, color = "FAIL / INVESTIGATE", "#8c3434"
        self.report_banner.setText(title)
        self.report_banner.setStyleSheet(
            f"background: {color}; color: white; font-weight: 700; border-radius: 5px"
        )
        measurements = "".join(
            f"<tr><th>{escape(name)}</th><td>{value:.6g}</td></tr>"
            for name, value in sorted(report.measurements.items())
        )
        outputs = "".join(
            f"<tr><th>{escape(name)}</th><td>{escape(str(value))}</td></tr>"
            for name, value in sorted(report.outputs.items())
        )
        explanations = "".join(f"<li>{escape(text)}</li>" for text in report.explanations)
        diagnostics = "".join(
            f"<li><b>{escape(item.severity.value)}</b> {escape(item.code)} — "
            f"{escape(item.message)}</li>"
            for item in report.diagnostics
        )
        self.report_summary.setHtml(
            f"<h2>{escape(report.scenario_id)}</h2>"
            f"<p>Run <code>{escape(report.run_id)}</code> • State: "
            f"<b>{escape(report.firmware_state.value)}</b></p>"
            "<h3>Measurements</h3><table cellspacing='6'>"
            f"{measurements or '<tr><td>None</td></tr>'}</table>"
            f"<h3>Outputs</h3><table cellspacing='6'>{outputs or '<tr><td>None</td></tr>'}</table>"
            f"<h3>What this means</h3><ul>{explanations or '<li>No explanation supplied</li>'}</ul>"
            f"<h3>Diagnostics</h3><ul>{diagnostics or '<li>No diagnostics</li>'}</ul>"
        )

    def _render_firmware(self, report: RunReport) -> None:
        self.firmware_state_label.setText(report.firmware_state.value)
        state_color = "#25734b" if report.passed else "#8c3434"
        self.firmware_state_label.setStyleSheet(
            f"background: {state_color}; color: white; font-size: 18px; "
            "font-weight: 700; padding: 9px; border-radius: 4px"
        )
        self.timeline_table.clear()
        uart_lines: list[str] = []
        for event in report.timeline:
            time_value = event.get("time_s", event.get("time", "—"))
            if isinstance(time_value, (int, float)):
                time_text = f"{time_value:.6g} s"
            else:
                time_text = str(time_value)
            kind = str(event.get("kind", event.get("type", "event")))
            message = str(event.get("message", event.get("line", event.get("value", event))))
            self.timeline_table.addTopLevelItem(
                QtWidgets.QTreeWidgetItem((time_text, kind, message))
            )
            if kind.casefold() == "uart":
                uart_lines.append(message)
        self.uart_output.setPlainText("\n".join(uart_lines))

    def _clear_results(self) -> None:
        self.current_report = None
        self.waveform_view.set_signals(())
        self.report_banner.setText("No run yet")
        self.report_banner.setStyleSheet("background: #354052; color: #dfe8f2; border-radius: 5px")
        self.report_summary.setHtml(
            "<h3>Start with nominal</h3>"
            "<p>A baseline makes each later fault easier to understand and compare.</p>"
        )
        self.firmware_state_label.setText("NOT RUN")
        self.firmware_state_label.setStyleSheet("")
        self.uart_output.clear()
        self.timeline_table.clear()

    @QtCore.Slot()
    def _choose_project(self) -> None:
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open KiCad or normalized project",
            "",
            (
                "Supported projects (*.kicad_pro project.json *.smdtwin);;"
                "KiCad projects (*.kicad_pro);;Normalized bundles (project.json *.smdtwin)"
            ),
        )
        if not filename:
            return
        if self._project_loader is None:
            QtWidgets.QMessageBox.information(
                self,
                "Importer not configured",
                "This build is running the sample workspace. Project import can be connected "
                "without changing the UI.",
            )
            return
        task = _ProjectImportTask(self._project_loader, Path(filename))
        self._import_jobs.add(task)
        task.signals.succeeded.connect(self.set_project)
        task.signals.failed.connect(self._show_import_error)
        task.signals.finished.connect(lambda: self._finish_import(task))
        self.open_action.setEnabled(False)
        self.run_button.setEnabled(False)
        self.statusBar().showMessage("Importing project with KiCad in the background...")
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(str)
    def _show_import_error(self, details: str) -> None:
        summary = details.strip().splitlines()[-1] if details.strip() else "Unknown import error"
        QtWidgets.QMessageBox.warning(self, "Could not import project", summary)
        self.statusBar().showMessage(summary, 12000)

    def _finish_import(self, task: _ProjectImportTask) -> None:
        self._import_jobs.discard(task)
        self.open_action.setEnabled(True)
        self.run_button.setEnabled(self.project is not None and self._scenario_enabled)

    @QtCore.Slot()
    def _choose_report_path(self) -> None:
        if self.current_report is None:
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save run report",
            f"{self.current_report.run_id}.json",
            "JSON report (*.json)",
        )
        if filename and not self.save_report(Path(filename)):
            QtWidgets.QMessageBox.warning(
                self, "Could not save report", self.statusBar().currentMessage()
            )

    @QtCore.Slot(str)
    def _show_run_error(self, details: str) -> None:
        summary = details.strip().splitlines()[-1] if details.strip() else "Unknown worker error"
        self.report_banner.setText("RUN ERROR")
        self.report_banner.setStyleSheet(
            "background: #8c3434; color: white; font-weight: 700; border-radius: 5px"
        )
        self.report_summary.setHtml(
            "<h3>The scenario did not complete</h3>"
            f"<p>{escape(summary)}</p>"
            "<p>The project remains open. Check the capability report and external tool paths.</p>"
        )
        self.results_tabs.setCurrentIndex(1)
        self.statusBar().showMessage(summary, 12000)

    def _finish_task(self, task: _ScenarioTask) -> None:
        self._jobs.discard(task)
        self.run_button.setText("Run scenario")
        self.run_button.setEnabled(self.project is not None and self._scenario_enabled)
        self.run_progress.setRange(0, 1)
        self.run_progress.setValue(1 if self.current_report else 0)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #eef2f6; }
            QLabel#project_title { font-size: 17px; font-weight: 650; color: #1b2b3c;
                                   padding: 4px; }
            QGroupBox { font-weight: 600; border: 1px solid #c9d2dd; border-radius: 6px;
                        margin-top: 8px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QPushButton { padding: 6px 12px; }
            QPushButton#run_scenario_button { background: #1769aa; color: white; font-weight: 650;
                                              border: 0; border-radius: 4px; min-height: 25px; }
            QPushButton#run_scenario_button:disabled { background: #8ca5ba; }
            QTreeWidget, QTextBrowser, QPlainTextEdit { background: white;
                                                        border: 1px solid #c9d2dd; }
            QDockWidget::title { background: #dde5ed; padding: 6px; font-weight: 600; }
            """
        )
