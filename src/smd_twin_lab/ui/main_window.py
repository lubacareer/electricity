"""Main desktop workspace for project inspection and educational scenarios."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Protocol

from PySide6 import QtCore, QtGui, QtWidgets

from ..eda import (
    CircuitCompiler,
    DcMnaSolver,
    EdaProjectDocument,
    EdaProjectRepository,
    NgspiceCircuitEngine,
)
from ..eda.kicad import (
    ExportReport,
    FabricationManifest,
    FabricationPackager,
    KiCad10Bridge,
)
from ..eda.library import CatalogRefreshReport, LibraryCatalog
from ..history import write_localized_report
from ..localization import LanguageManager, current_language_manager
from ..models import (
    Capability,
    CapabilityStatus,
    Component,
    FaultKind,
    FaultSpec,
    ImportedProject,
    MessageRef,
    Net,
    RunReport,
    Scenario,
)
from ..paths import cache_root
from .board_canvas import BoardCanvas
from .designer import DesignerWorkspace, issue_text
from .sample_data import build_sample_project, run_sample_scenario
from .teaching import TeachingPanel
from .waveform import WaveformView


class ScenarioRunner(Protocol):
    def __call__(self, project: ImportedProject, scenario: Scenario) -> RunReport: ...


ProjectLoader = Callable[[Path], ImportedProject]
ScenarioGate = Callable[[ImportedProject], tuple[bool, str]]

_FAULT_LABELS = {
    FaultKind.NONE: ("main.fault.none", "Nominal (no fault)"),
    FaultKind.COMPONENT_OPEN: ("main.fault.component_open", "Component open"),
    FaultKind.NET_SHORT: ("main.fault.net_short", "Short two nets"),
    FaultKind.WRONG_VALUE: ("main.fault.wrong_value", "Wrong component value"),
    FaultKind.REVERSED_POLARITY: ("main.fault.reversed_polarity", "Reversed polarity"),
    FaultKind.INTERMITTENT: ("main.fault.intermittent", "Intermittent open"),
}


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


class _FunctionTask(QtCore.QRunnable):
    """Run one bounded Designer operation away from the Qt GUI thread."""

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _TaskSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self.operation())
        except Exception:  # noqa: BLE001 - worker errors become user-visible diagnostics
            self.signals.failed.emit(traceback.format_exc(limit=8))
        finally:
            self.signals.finished.emit()


class CapabilityPanel(QtWidgets.QWidget):
    """Compact report that distinguishes missing tools from project errors."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.language_manager = language_manager or current_language_manager()
        self._project: ImportedProject | None = None
        self.labels: dict[str, QtWidgets.QLabel] = {}
        self._field_labels: dict[str, QtWidgets.QLabel] = {}
        self._layout = QtWidgets.QFormLayout(self)
        self._layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        for key in ("geometry", "circuit", "firmware", "hardware"):
            label = QtWidgets.QLabel("Not inspected")
            label.setWordWrap(True)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            title_label = QtWidgets.QLabel()
            self.labels[key] = label
            self._field_labels[key] = title_label
            self._layout.addRow(title_label, label)
        self.diagnostics = QtWidgets.QLabel()
        self.diagnostics.setWordWrap(True)
        self._diagnostics_label = QtWidgets.QLabel()
        self._layout.addRow(self._diagnostics_label, self.diagnostics)
        self.retranslate_ui()

    def _text(
        self,
        message_id: str,
        fallback: str,
        *,
        parameters: dict[str, object] | None = None,
        count: int | None = None,
    ) -> str:
        return self.language_manager.text(
            message_id,
            fallback,
            parameters=parameters,
            count=count,
        )

    @QtCore.Slot()
    def retranslate_ui(self) -> None:
        titles = {
            "geometry": ("main.capability.geometry", "Board geometry"),
            "circuit": ("main.capability.circuit", "Circuit simulation"),
            "firmware": ("main.capability.firmware", "Firmware model"),
            "hardware": ("main.capability.hardware", "Hardware target"),
        }
        for key, (message_id, fallback) in titles.items():
            self._field_labels[key].setText(self._text(message_id, fallback))
        self._diagnostics_label.setText(self._text("main.capability.import_notes", "Import notes"))
        self.set_project(self._project)

    def set_project(self, project: ImportedProject | None) -> None:
        self._project = project
        if project is None:
            for label in self.labels.values():
                label.setText(self._text("main.capability.not_inspected", "Not inspected"))
                label.setStyleSheet("")
            self.diagnostics.setText(self._text("main.capability.no_project", "No project loaded"))
            return
        capabilities = project.capabilities
        for key in self.labels:
            self._set_capability(self.labels[key], getattr(capabilities, key))
        if project.diagnostics:
            counts: dict[str, int] = {}
            for diagnostic in project.diagnostics:
                counts[diagnostic.severity.value] = counts.get(diagnostic.severity.value, 0) + 1
            summaries = []
            for severity, count in sorted(counts.items()):
                fallback = (
                    f"{count} {severity} message" if count == 1 else f"{count} {severity} messages"
                )
                summaries.append(
                    self._text(
                        f"main.diagnostics.count.{severity}",
                        fallback,
                        parameters={"count": count},
                        count=count,
                    )
                )
            self.diagnostics.setText(", ".join(summaries))
        else:
            self.diagnostics.setText(
                self._text("main.capability.no_diagnostics", "No import diagnostics")
            )

    def _set_capability(self, label: QtWidgets.QLabel, capability: Capability) -> None:
        palette = {
            CapabilityStatus.AVAILABLE: (
                self._text("main.capability.status.available", "Available"),
                "#25734b",
                "#ddf6e8",
            ),
            CapabilityStatus.UNAVAILABLE: (
                self._text("main.capability.status.unavailable", "Unavailable"),
                "#795c16",
                "#fff2c7",
            ),
            CapabilityStatus.INVALID: (
                self._text("main.capability.status.invalid", "Invalid"),
                "#8c3434",
                "#ffe1e1",
            ),
        }
        title, background, foreground = palette[capability.status]
        detail = self.language_manager.render(
            getattr(capability, "message_ref", None), capability.detail
        )
        label.setText(f"<b>{escape(title)}</b> — {escape(detail)}")
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
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        bindings = bindings or ControllerBindings(
            initial_project=build_sample_project(),
            scenario_runner=run_sample_scenario,
        )
        self._project_loader = bindings.project_loader
        self._scenario_runner = bindings.scenario_runner or run_sample_scenario
        self._scenario_gate = bindings.scenario_gate
        self.language_manager = language_manager or current_language_manager()
        self.project: ImportedProject | None = None
        self.current_report: RunReport | None = None
        self._active_scenario: Scenario | None = None
        self._jobs: set[_ScenarioTask] = set()
        self._import_jobs: set[_ProjectImportTask] = set()
        self._designer_jobs: set[_FunctionTask] = set()
        self._designer_repository = EdaProjectRepository()
        self._designer_bridge = KiCad10Bridge()
        self._designer_spice = NgspiceCircuitEngine()
        self._designer_catalog = LibraryCatalog(
            cache_root() / "eda-designer" / "kicad-libraries.sqlite3"
        )
        self._designer_library_report: CatalogRefreshReport | None = None
        self._designer_status_message: (
            tuple[str, str, dict[str, object] | None, int | None] | None
        ) = None
        self._designer_status_issue: object | None = None
        self._designer_autosave_timer = QtCore.QTimer(self)
        self._designer_autosave_timer.setSingleShot(True)
        self._designer_autosave_timer.setInterval(1200)
        self._designer_autosave_timer.timeout.connect(self._autosave_designer_document)
        self._test_lab_dock_visibility: dict[QtWidgets.QDockWidget, bool] = {}
        self._scenario_enabled = False
        self._scenario_tooltip_is_default = True
        self._scenario_tooltip_detail = "Run a scenario"
        self._result_mode = "empty"
        self._last_run_error_summary = ""
        self._status_message: tuple[str, str, dict[str, object] | None, int | None, int] | None = (
            None
        )
        self._retranslating = False

        self.setObjectName("main_window")
        self.setWindowTitle("SMD Twin Lab")
        self.resize(1440, 900)
        self.setMinimumSize(1280, 720)
        self._build_actions()
        self._build_workspace()
        self._apply_style()
        self.language_manager.language_changed.connect(self._on_language_changed)
        self.retranslate_ui()
        self._show_status(
            "main.status.ready",
            "Ready — external tools are optional",
        )
        self.set_project(bindings.initial_project or build_sample_project())

    def _text(
        self,
        message_id: str,
        fallback: str,
        *,
        parameters: dict[str, object] | None = None,
        count: int | None = None,
    ) -> str:
        return self.language_manager.text(
            message_id,
            fallback,
            parameters=parameters,
            count=count,
        )

    def _build_actions(self) -> None:
        self.open_action = QtGui.QAction("Open project…", self)
        self.open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self._choose_project)
        self.open_design_action = QtGui.QAction("Open editable design…", self)
        self.open_design_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+O"))
        self.open_design_action.triggered.connect(self._choose_design)
        self.recover_design_action = QtGui.QAction("Recover autosave…", self)
        self.recover_design_action.triggered.connect(self._recover_designer_autosave)
        self.save_report_action = QtGui.QAction("Save report…", self)
        self.save_report_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        self.save_report_action.setEnabled(False)
        self.save_report_action.triggered.connect(self._choose_report_path)
        self.exit_action = QtGui.QAction("Exit", self)
        self.exit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        self.file_menu = self.menuBar().addMenu("File")
        self.file_menu.addAction(self.open_action)
        self.file_menu.addAction(self.open_design_action)
        self.file_menu.addAction(self.recover_design_action)
        self.file_menu.addAction(self.save_report_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)

        self.language_menu = self.menuBar().addMenu("Language")
        self.language_action_group = QtGui.QActionGroup(self)
        self.language_action_group.setExclusive(True)
        self.language_actions: dict[str, QtGui.QAction] = {}
        for spec in self.language_manager.available_languages:
            action = QtGui.QAction(spec.native_name, self)
            action.setCheckable(True)
            action.setData(spec.code)
            action.triggered.connect(
                lambda checked=False, code=spec.code: self._select_language(code, checked)
            )
            self.language_action_group.addAction(action)
            self.language_menu.addAction(action)
            self.language_actions[spec.code] = action

        self.project_toolbar = self.addToolBar("Project")
        self.project_toolbar.setMovable(False)
        self.project_toolbar.addAction(self.open_action)
        self.project_toolbar.addAction(self.save_report_action)

    def _build_workspace(self) -> None:
        self.project_title = QtWidgets.QLabel("No project")
        self.project_title.setObjectName("project_title")
        self.project_title.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.scenario_controls = self._build_scenario_controls()
        self.board_canvas = BoardCanvas(language_manager=self.language_manager)
        self.board_canvas.setObjectName("board_canvas")
        self.board_canvas.component_selected.connect(self._select_component_in_tree)

        self.results_tabs = QtWidgets.QTabWidget()
        self.waveform_view = WaveformView(language_manager=self.language_manager)
        self.waveform_view.setObjectName("waveform_view")
        self.results_tabs.addTab(self.waveform_view, "Waveforms")
        self.run_summary_widget = self._build_run_summary()
        self.firmware_panel = self._build_firmware_panel()
        self.results_tabs.addTab(self.run_summary_widget, "Run report")
        self.results_tabs.addTab(self.firmware_panel, "Firmware & timeline")

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.board_group = QtWidgets.QGroupBox("Board view")
        board_layout = QtWidgets.QVBoxLayout(self.board_group)
        board_layout.addWidget(self.board_canvas)
        splitter.addWidget(self.board_group)
        splitter.addWidget(self.results_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes((390, 260))

        self.test_lab_workspace = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(self.test_lab_workspace)
        central_layout.addWidget(self.project_title)
        central_layout.addWidget(self.scenario_controls)
        central_layout.addWidget(splitter, 1)

        self.designer_workspace = DesignerWorkspace(language_manager=self.language_manager)
        self.designer_workspace.save_requested.connect(self._save_designer_document)
        self.designer_workspace.new_project_requested.connect(self._new_designer_project)
        self.designer_workspace.simulation_requested.connect(self._simulate_designer_document)
        self.designer_workspace.export_requested.connect(self._export_designer_document)
        self.designer_workspace.validation_requested.connect(
            lambda _document: self._clear_designer_status_reference()
        )
        self.designer_workspace.document_changed.connect(self._schedule_designer_autosave)
        self.designer_workspace.library_refresh_requested.connect(self._refresh_designer_libraries)
        self.designer_workspace.library_search_requested.connect(self._search_designer_libraries)

        self.workspace_tabs = QtWidgets.QTabWidget()
        self.workspace_tabs.setObjectName("workspace_tabs")
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.addTab(self.test_lab_workspace, "Test Lab")
        self.workspace_tabs.addTab(self.designer_workspace, "PCB Designer")
        self.workspace_tabs.currentChanged.connect(self._workspace_changed)
        self.setCentralWidget(self.workspace_tabs)

        self._build_browser_dock()
        self._build_inspector_dock()
        self._build_teaching_dock()
        self._workspace_changed(self.workspace_tabs.currentIndex())

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
        for kind in FaultKind:
            self.fault_combo.addItem(kind.value, kind)

        self.reference_combo = QtWidgets.QComboBox()
        self.reference_combo.setObjectName("fault_reference_combo")
        self.net_a_combo = QtWidgets.QComboBox()
        self.net_b_combo = QtWidgets.QComboBox()
        self.value_spin = QtWidgets.QDoubleSpinBox()
        self.value_spin.setRange(0.001, 1.0e12)
        self.value_spin.setValue(47_000.0)
        self.value_spin.setDecimals(3)
        self.value_spin.setSuffix(" Ω")
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
        self.temperature_label = QtWidgets.QLabel("Temperature")
        self.fault_label = QtWidgets.QLabel("Fault")
        self.component_label = QtWidgets.QLabel("Component")
        self.net_a_label = QtWidgets.QLabel("Net A")
        self.net_b_label = QtWidgets.QLabel("Net B")
        self.resistance_label = QtWidgets.QLabel("Resistance")
        self.start_label = QtWidgets.QLabel("Start")
        self.duration_label = QtWidgets.QLabel("Duration")
        layout.addWidget(self.temperature_label, 0, 0)
        layout.addWidget(self.temperature_spin, 0, 1)
        layout.addWidget(self.fault_label, 0, 2)
        layout.addWidget(self.fault_combo, 0, 3)
        layout.addWidget(self.component_label, 0, 4)
        layout.addWidget(self.reference_combo, 0, 5)
        layout.addWidget(self.net_a_label, 1, 0)
        layout.addWidget(self.net_a_combo, 1, 1)
        layout.addWidget(self.net_b_label, 1, 2)
        layout.addWidget(self.net_b_combo, 1, 3)
        layout.addWidget(self.resistance_label, 1, 4)
        layout.addWidget(self.value_spin, 1, 5)
        layout.addWidget(self.start_label, 2, 0)
        layout.addWidget(self.start_spin, 2, 1)
        layout.addWidget(self.duration_label, 2, 2)
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
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.timeline_table.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.timeline_table.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        self.firmware_state_group = QtWidgets.QGroupBox("Firmware state")
        state_layout = QtWidgets.QVBoxLayout(self.firmware_state_group)
        state_layout.addWidget(self.firmware_state_label)
        self.uart_group = QtWidgets.QGroupBox("UART")
        uart_layout = QtWidgets.QVBoxLayout(self.uart_group)
        uart_layout.addWidget(self.uart_output)
        left = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        left.addWidget(self.firmware_state_group)
        left.addWidget(self.uart_group)
        left.setStretchFactor(1, 1)
        self.timeline_group = QtWidgets.QGroupBox("Timeline")
        timeline_layout = QtWidgets.QVBoxLayout(self.timeline_group)
        timeline_layout.addWidget(self.timeline_table)
        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(self.timeline_group)
        split.setStretchFactor(1, 2)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(split)
        return widget

    def _build_browser_dock(self) -> None:
        self.browser_dock = QtWidgets.QDockWidget("Project browser", self)
        self.browser_dock.setObjectName("project_browser_dock")
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
        self.browser_dock.setWidget(browser_widget)
        self.browser_dock.setMinimumWidth(240)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self.browser_dock)

    def _build_inspector_dock(self) -> None:
        self.inspector_dock = QtWidgets.QDockWidget("Inspector", self)
        self.inspector_dock.setObjectName("inspector_dock")
        self.inspector_tabs = QtWidgets.QTabWidget()
        self.inspector = QtWidgets.QTextBrowser()
        self.inspector.setObjectName("item_inspector")
        self.inspector.setHtml("<h3>Selection</h3><p>Select a component or net.</p>")
        self.capability_panel = CapabilityPanel(language_manager=self.language_manager)
        self.capability_panel.setObjectName("capability_panel")
        self.inspector_tabs.addTab(self.inspector, "Component / net")
        self.inspector_tabs.addTab(self.capability_panel, "Capabilities")
        self.inspector_dock.setWidget(self.inspector_tabs)
        self.inspector_dock.setMinimumWidth(330)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)

    def _build_teaching_dock(self) -> None:
        self.teaching_dock = QtWidgets.QDockWidget("Learn while testing", self)
        self.teaching_dock.setObjectName("teaching_dock")
        self.teaching_panel = TeachingPanel(language_manager=self.language_manager)
        self.teaching_panel.setObjectName("teaching_panel")
        self.teaching_dock.setWidget(self.teaching_panel)
        self.teaching_dock.setMinimumWidth(330)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.teaching_dock)
        self.tabifyDockWidget(self.inspector_dock, self.teaching_dock)

    def set_project(self, project: ImportedProject) -> None:
        self.project = project
        self.current_report = None
        self._active_scenario = None
        self._render_project_title()
        self.board_canvas.set_project(project)
        self.capability_panel.set_project(project)
        self._populate_browser(project)
        self._populate_scenario_targets(project)
        self._clear_results()
        self.save_report_action.setEnabled(False)
        if self._scenario_gate is None:
            self._scenario_enabled = True
            self._scenario_tooltip_is_default = True
            detail = "Run a scenario"
        else:
            self._scenario_enabled, detail = self._scenario_gate(project)
            self._scenario_tooltip_is_default = False
        self._scenario_tooltip_detail = detail
        self.run_button.setEnabled(self._scenario_enabled and not self._import_jobs)
        self.run_button.setToolTip(self._scenario_tooltip_text())
        self.project_changed.emit(project)
        self._show_status(
            "main.status.loaded_project",
            "Loaded {project}",
            parameters={"project": project.name},
            timeout=5000,
        )

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
        self.run_button.setText(self._text("main.scenario.running", "Running…"))
        self.run_progress.setRange(0, 0)
        self._show_status(
            "main.status.running_scenario",
            "Running {scenario}",
            parameters={"scenario": scenario.name},
        )
        self.teaching_panel.show_topic(
            "simulation",
            "The scenario runs outside the UI thread. Compare its traces with nominal behavior.",
            context_ref=MessageRef("main.teaching.scenario_worker"),
        )
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def set_run_report(self, report: object) -> None:
        if not isinstance(report, RunReport):
            self._show_run_error(
                self._text(
                    "main.error.invalid_report",
                    "The controller returned an invalid report object.",
                )
            )
            return
        self.current_report = report
        self._result_mode = "report"
        self.save_report_action.setEnabled(
            self.workspace_tabs.currentWidget() is self.test_lab_workspace
        )
        self.waveform_view.set_signals(report.signals)
        self._render_report_summary(report)
        self._render_firmware(report)
        if self._active_scenario is not None:
            self._select_scenario_target(self._active_scenario)
        self.results_tabs.setCurrentIndex(0)
        self.report_changed.emit(report)
        self._show_status(
            "main.status.scenario_complete.passed"
            if report.passed
            else "main.status.scenario_complete.attention",
            "Scenario complete: passed" if report.passed else "Scenario complete: needs attention",
            timeout=8000,
        )

    def save_report(self, path: Path) -> bool:
        if self.current_report is None:
            return False
        try:
            write_localized_report(
                path,
                self.current_report,
                self.language_manager.current_language,
            )
        except OSError as exc:
            self._show_status(
                "main.status.save_failed",
                "Could not save report: {error}",
                parameters={"error": str(exc)},
                timeout=10000,
            )
            return False
        self._show_status(
            "main.status.report_saved",
            "Saved report to {path}",
            parameters={"path": str(path)},
            timeout=8000,
        )
        return True

    def _populate_browser(self, project: ImportedProject) -> None:
        self.project_tree.clear()
        self._component_root = QtWidgets.QTreeWidgetItem(
            ("Components", str(len(project.components)))
        )
        self._component_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
        for component in sorted(project.components, key=lambda item: item.reference):
            item = QtWidgets.QTreeWidgetItem(
                (component.reference, component.value or component.footprint)
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "component")
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, component.reference)
            self._component_root.addChild(item)
        self._net_root = QtWidgets.QTreeWidgetItem(("Nets", str(len(project.nets))))
        self._net_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
        for net in sorted(project.nets, key=lambda item: item.name.casefold()):
            pin_count = len(net.pins)
            item = QtWidgets.QTreeWidgetItem(
                (
                    net.name,
                    self._text(
                        "main.count.pins",
                        f"{pin_count} pin" if pin_count == 1 else f"{pin_count} pins",
                        parameters={"count": pin_count},
                        count=pin_count,
                    ),
                )
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "net")
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, net.name)
            item.setData(1, QtCore.Qt.ItemDataRole.UserRole, pin_count)
            self._net_root.addChild(item)
        self.project_tree.addTopLevelItems((self._component_root, self._net_root))
        self._component_root.setExpanded(True)
        self._net_root.setExpanded(True)
        self._retranslate_browser_tree()

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
            name=_FAULT_LABELS[kind][1],
            temperature_c=self.temperature_spin.value(),
            fault=fault,
            language=self.language_manager.current_language,
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
                context_ref=MessageRef("main.teaching.change_one_fault"),
            )

    def _selected_fault_kind(self) -> FaultKind:
        value = self.fault_combo.currentData()
        return value if isinstance(value, FaultKind) else FaultKind(str(value))

    @QtCore.Slot(QtWidgets.QTreeWidgetItem, QtWidgets.QTreeWidgetItem)
    def _inspect_tree_item(
        self,
        current: QtWidgets.QTreeWidgetItem | None,
        previous: QtWidgets.QTreeWidgetItem | None,
        update_teaching: bool = True,
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
                self._show_component(component, update_teaching=update_teaching)
                self.board_canvas.select_reference(component.reference)
        elif kind == "net":
            net = next((item for item in self.project.nets if item.name == name), None)
            if net is not None:
                self._show_net(net, update_teaching=update_teaching)

    def _show_component(self, component: Component, *, update_teaching: bool = True) -> None:
        fields = "".join(
            f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
            for key, value in sorted(component.fields.items())
        )
        nets = ", ".join(escape(net) for net in component.nets) or self._text(
            "main.common.unknown", "Unknown"
        )
        placement = (
            f"{component.x_mm:.2f}, {component.y_mm:.2f} mm"
            if component.x_mm is not None and component.y_mm is not None
            else self._text("main.inspector.not_placed", "Not placed")
        )
        side = self._text(
            f"main.component_side.{component.side.value}",
            component.side.value,
        )
        assembly = self._text(
            "main.inspector.assembly.smd" if component.is_smd else "main.inspector.assembly.other",
            "SMD" if component.is_smd else "through-hole or unknown",
        )
        bom_status = self._text(
            "main.inspector.in_bom" if component.in_bom else "main.inspector.not_in_bom",
            "in BOM" if component.in_bom else "not in BOM",
        )
        self.inspector.setHtml(
            f"<h2>{escape(component.reference)}</h2>"
            "<table cellspacing='6'>"
            f"<tr><th>{escape(self._text('main.inspector.value', 'Value'))}</th>"
            f"<td>{escape(component.value or '—')}</td></tr>"
            f"<tr><th>{escape(self._text('main.inspector.footprint', 'Footprint'))}</th>"
            f"<td>{escape(component.footprint or '—')}</td></tr>"
            f"<tr><th>{escape(self._text('main.inspector.side', 'Side'))}</th>"
            f"<td>{escape(side)}</td></tr>"
            f"<tr><th>{escape(self._text('main.inspector.placement', 'Placement'))}</th>"
            f"<td>{escape(placement)}</td></tr>"
            f"<tr><th>{escape(self._text('main.inspector.rotation', 'Rotation'))}</th>"
            f"<td>{component.rotation_deg:.1f}°</td></tr>"
            f"<tr><th>{escape(self._text('main.inspector.nets', 'Nets'))}</th>"
            f"<td>{nets}</td></tr>{fields}</table>"
            f"<p><b>{escape(self._text('main.inspector.assembly', 'Assembly'))}:</b> "
            f"{escape(assembly)}, {escape(bom_status)}.</p>"
        )
        if update_teaching:
            teaching_context = (
                "{reference}: connect its visible placement to its nets and simulated behavior."
            )
            self.teaching_panel.show_topic(
                "aoi",
                teaching_context,
                context_ref=MessageRef(
                    "main.teaching.component_context",
                    {"reference": component.reference},
                ),
            )

    def _show_net(self, net: Net, *, update_teaching: bool = True) -> None:
        pin_word = self._text("main.inspector.pin", "pin")
        pins = "".join(
            f"<li><b>{escape(pin.reference)}</b> {escape(pin_word)} {escape(pin.pin)}</li>"
            for pin in net.pins
        )
        pin_count = len(net.pins)
        connected_pins = self._text(
            "main.inspector.connected_pins",
            f"{pin_count} connected pin" if pin_count == 1 else f"{pin_count} connected pins",
            parameters={"count": pin_count},
            count=pin_count,
        )
        no_pin_data = self._text("main.inspector.no_pin_data", "No pin data")
        net_explanation = self._text(
            "main.inspector.net_explanation",
            "A net is an electrical connection. AOI sees joints; simulation predicts how "
            "the connection changes voltage and current.",
        )
        self.inspector.setHtml(
            f"<h2>{escape(self._text('main.inspector.net', 'Net'))} {escape(net.name)}</h2>"
            f"<p>{escape(connected_pins)}</p>"
            f"<ul>{pins or f'<li>{escape(no_pin_data)}</li>'}</ul>"
            f"<p>{escape(net_explanation)}</p>"
        )
        if update_teaching:
            teaching_context = (
                "Inspect {net} as both a physical copper connection and a simulated signal."
            )
            self.teaching_panel.show_topic(
                "simulation",
                teaching_context,
                context_ref=MessageRef(
                    "main.teaching.net_context",
                    {"net": net.name},
                ),
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
            title, color = (
                self._text("main.report.infrastructure_error", "INFRASTRUCTURE ERROR"),
                "#9b5f18",
            )
        elif report.passed:
            title, color = self._text("main.report.pass", "PASS"), "#25734b"
        else:
            title, color = (
                self._text("main.report.fail_investigate", "FAIL / INVESTIGATE"),
                "#8c3434",
            )
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
        explanation_refs = getattr(report, "explanation_refs", ())
        rendered_explanations = []
        for index, fallback in enumerate(report.explanations):
            message_ref = explanation_refs[index] if index < len(explanation_refs) else None
            rendered_explanations.append(self.language_manager.render(message_ref, fallback))
        explanations = "".join(f"<li>{escape(text)}</li>" for text in rendered_explanations)
        diagnostic_items = []
        for item in report.diagnostics:
            severity = self._text(
                f"main.diagnostic_severity.{item.severity.value}",
                item.severity.value,
            )
            message = self.language_manager.render(getattr(item, "message_ref", None), item.message)
            diagnostic_items.append(
                f"<li><b>{escape(severity)}</b> {escape(item.code)} — {escape(message)}</li>"
            )
        diagnostics = "".join(diagnostic_items)
        state = self._firmware_state_text(report.firmware_state.value)
        none = escape(self._text("main.common.none", "None"))
        no_explanation = escape(self._text("main.report.no_explanation", "No explanation supplied"))
        no_diagnostics = escape(self._text("main.report.no_diagnostics", "No diagnostics"))
        self.report_summary.setHtml(
            f"<h2>{escape(report.scenario_id)}</h2>"
            f"<p>{escape(self._text('main.report.run', 'Run'))} "
            f"<code>{escape(report.run_id)}</code> • "
            f"{escape(self._text('main.report.state', 'State'))}: "
            f"<b>{escape(state)}</b></p>"
            f"<h3>{escape(self._text('main.report.measurements', 'Measurements'))}</h3>"
            f"<table cellspacing='6'>{measurements or f'<tr><td>{none}</td></tr>'}</table>"
            f"<h3>{escape(self._text('main.report.outputs', 'Outputs'))}</h3>"
            f"<table cellspacing='6'>{outputs or f'<tr><td>{none}</td></tr>'}</table>"
            f"<h3>{escape(self._text('main.report.meaning', 'What this means'))}</h3>"
            f"<ul>{explanations or f'<li>{no_explanation}</li>'}</ul>"
            f"<h3>{escape(self._text('main.report.diagnostics', 'Diagnostics'))}</h3>"
            f"<ul>{diagnostics or f'<li>{no_diagnostics}</li>'}</ul>"
        )

    def _render_firmware(self, report: RunReport) -> None:
        self._render_firmware_state(report)
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
            display_kind, display_message = self._timeline_event_text(event, kind, message)
            self.timeline_table.addTopLevelItem(
                QtWidgets.QTreeWidgetItem((time_text, display_kind, display_message))
            )
            if kind.casefold() == "uart":
                uart_lines.append(message)
        self.uart_output.setPlainText("\n".join(uart_lines))

    def _timeline_event_text(
        self,
        event: dict[str, object],
        kind: str,
        message: str,
    ) -> tuple[str, str]:
        normalized_kind = kind.casefold()
        kind_labels = {
            "state": ("main.timeline.kind.state", "State"),
            "fault": ("main.timeline.kind.fault", "Fault"),
            "circuit_complete": ("main.timeline.kind.circuit", "Circuit"),
            "firmware_state": ("main.timeline.kind.firmware", "Firmware"),
            "assertion": ("main.timeline.kind.check", "Check"),
            "engine": ("main.timeline.kind.engine", "Engine"),
            "uart": ("main.timeline.kind.uart", "UART"),
        }
        label = kind_labels.get(normalized_kind)
        display_kind = self._text(*label) if label is not None else kind

        if normalized_kind == "uart":
            return display_kind, message
        if normalized_kind == "state":
            if message == "Power-on self-test":
                return display_kind, self._text(
                    "main.timeline.power_on_self_test",
                    "Power-on self-test",
                )
            if message in {"NORMAL", "ALARM", "SENSOR_FAULT"}:
                return display_kind, self._firmware_state_text(message)
        if normalized_kind == "fault":
            normalized_fault = message.strip().replace(" ", "_").casefold()
            for fault_kind in FaultKind:
                if normalized_fault == fault_kind.value:
                    return display_kind, self._fault_kind_text(fault_kind)
        if normalized_kind == "circuit_complete":
            success = event.get("success")
            result = self._boolean_result_text(success)
            engine = str(event.get("engine", "—"))
            return display_kind, self._text(
                "main.timeline.circuit_result",
                "{engine}: {result}",
                parameters={"engine": engine, "result": result},
            )
        if normalized_kind == "firmware_state":
            previous = self._firmware_state_text(str(event.get("previous_state", "—")))
            current = self._firmware_state_text(str(event.get("state", "—")))
            return display_kind, self._text(
                "main.timeline.state_transition",
                "{previous} → {current}",
                parameters={"previous": previous, "current": current},
            )
        if normalized_kind == "assertion":
            expected = self._firmware_state_text(str(event.get("expected_state", "—")))
            observed = self._firmware_state_text(str(event.get("observed_state", "—")))
            result = self._boolean_result_text(event.get("passed"))
            return display_kind, self._text(
                "main.timeline.assertion_result",
                "Expected {expected}; observed {observed}; {result}",
                parameters={
                    "expected": expected,
                    "observed": observed,
                    "result": result,
                },
            )
        if normalized_kind == "engine" and "version" in event:
            return display_kind, self._text(
                "main.timeline.engine_version",
                "Version: {version}",
                parameters={"version": str(event["version"])},
            )
        return display_kind, message

    def _boolean_result_text(self, value: object) -> str:
        if value is True:
            return self._text("main.timeline.passed", "passed")
        if value is False:
            return self._text("main.timeline.failed", "failed")
        return str(value)

    def _render_firmware_state(self, report: RunReport) -> None:
        self.firmware_state_label.setText(self._firmware_state_text(report.firmware_state.value))
        state_color = "#25734b" if report.passed else "#8c3434"
        self.firmware_state_label.setStyleSheet(
            f"background: {state_color}; color: white; font-size: 18px; "
            "font-weight: 700; padding: 9px; border-radius: 4px"
        )

    def _clear_results(self) -> None:
        self.current_report = None
        self._result_mode = "empty"
        self._last_run_error_summary = ""
        self.waveform_view.set_signals(())
        self._render_empty_report()
        self.firmware_state_label.setText(self._text("main.firmware.not_run", "NOT RUN"))
        self.firmware_state_label.setStyleSheet("")
        self.uart_output.clear()
        self.timeline_table.clear()

    def _render_empty_report(self) -> None:
        self.report_banner.setText(self._text("main.report.no_run", "No run yet"))
        self.report_banner.setStyleSheet("background: #354052; color: #dfe8f2; border-radius: 5px")
        title = self._text("main.report.start_nominal", "Start with nominal")
        guidance = self._text(
            "main.report.baseline_help",
            "A baseline makes each later fault easier to understand and compare.",
        )
        self.report_summary.setHtml(f"<h3>{escape(title)}</h3><p>{escape(guidance)}</p>")

    def _render_run_error(self) -> None:
        self.report_banner.setText(self._text("main.report.run_error", "RUN ERROR"))
        self.report_banner.setStyleSheet(
            "background: #8c3434; color: white; font-weight: 700; border-radius: 5px"
        )
        title = self._text("main.error.scenario_incomplete", "The scenario did not complete")
        guidance = self._text(
            "main.error.project_remains_open",
            "The project remains open. Check the capability report and external tool paths.",
        )
        self.report_summary.setHtml(
            f"<h3>{escape(title)}</h3>"
            f"<p>{escape(self._last_run_error_summary)}</p>"
            f"<p>{escape(guidance)}</p>"
        )

    @QtCore.Slot(int)
    def _workspace_changed(self, index: int) -> None:
        designer_active = self.workspace_tabs.widget(index) is self.designer_workspace
        docks = (self.browser_dock, self.inspector_dock, self.teaching_dock)
        if designer_active:
            self._test_lab_dock_visibility = {dock: not dock.isHidden() for dock in docks}
            for dock in docks:
                dock.hide()
        else:
            for dock in docks:
                dock.setVisible(self._test_lab_dock_visibility.get(dock, True))
        self.project_toolbar.setVisible(not designer_active)
        self.save_report_action.setEnabled(not designer_active and self.current_report is not None)

    @QtCore.Slot(object)
    def _schedule_designer_autosave(self, _document: EdaProjectDocument) -> None:
        self._clear_designer_status_reference()
        if self.designer_workspace.is_dirty:
            self._designer_autosave_timer.start()

    @QtCore.Slot()
    def _autosave_designer_document(self) -> None:
        document = self.designer_workspace.document()
        if not self.designer_workspace.is_dirty:
            return
        self._start_designer_task(
            lambda: self._designer_repository.autosave(document),
            lambda _path: None,
        )

    @QtCore.Slot()
    def _refresh_designer_libraries(self) -> None:
        self.designer_workspace.set_library_indexing()
        self._start_designer_task(
            self._designer_catalog.refresh,
            self._show_designer_library_refresh,
            lambda details: self.designer_workspace.set_library_error(
                (details.strip().splitlines()[-1] if details.strip() else "Unknown catalog error"),
                enable_refresh=False,
            ),
        )

    def _show_designer_library_refresh(self, outcome: object) -> None:
        if not isinstance(outcome, CatalogRefreshReport):
            self.designer_workspace.set_library_error("Unsupported catalog result")
            return
        self._designer_library_report = outcome
        self._search_designer_libraries(self.designer_workspace.palette_filter.text())

    @QtCore.Slot(str)
    def _search_designer_libraries(self, query: str) -> None:
        if self._designer_library_report is None:
            return
        try:
            results = self._designer_catalog.search(query, limit=160)
        except Exception as error:  # noqa: BLE001 - catalog failures are user-visible
            self.designer_workspace.set_library_error(str(error))
            return
        self.designer_workspace.set_library_results(
            results,
            self._designer_library_report,
        )

    @QtCore.Slot()
    def _choose_design(self) -> None:
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self._text("main.dialog.open_design.title", "Open editable PCB design"),
            "",
            self._text("main.dialog.open_design.filter", "SMD EDA designs (*.smdeda)"),
        )
        if not filename:
            return
        if not self._confirm_discard_designer_changes():
            return
        self._load_designer_document(Path(filename))

    def _load_designer_document(self, path: Path, *, attach_path: bool = True) -> bool:
        try:
            document = self._designer_repository.load(path)
        except Exception as error:  # noqa: BLE001 - corrupt packages are user-visible
            QtWidgets.QMessageBox.warning(
                self,
                self._text("main.dialog.open_design.failed_title", "Could not open design"),
                str(error),
            )
            return False
        self.designer_workspace.load_document(document)
        self._clear_designer_status_reference()
        self.designer_workspace.set_document_path(path if attach_path else None)
        self.workspace_tabs.setCurrentWidget(self.designer_workspace)
        return True

    @QtCore.Slot(str)
    def _new_designer_project(self, template_id: str) -> None:
        if self._confirm_discard_designer_changes():
            self.designer_workspace.new_project(template_id)

    @QtCore.Slot()
    def _recover_designer_autosave(self) -> None:
        autosaves = self._designer_repository.list_autosaves()
        if not autosaves:
            QtWidgets.QMessageBox.information(
                self,
                self._text("main.dialog.recover_design.empty_title", "No autosaves found"),
                self._text(
                    "main.dialog.recover_design.empty_message",
                    "No PCB Designer autosaves are available yet.",
                ),
            )
            return
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self._text("main.dialog.recover_design.title", "Recover PCB Designer autosave"),
            str(self._designer_repository.autosave_root),
            self._text("main.dialog.open_design.filter", "SMD EDA designs (*.smdeda)"),
        )
        if filename and self._confirm_discard_designer_changes():
            self._load_designer_document(Path(filename), attach_path=False)

    def _confirm_discard_designer_changes(self) -> bool:
        if not self.designer_workspace.is_dirty:
            return True
        choice = QtWidgets.QMessageBox.warning(
            self,
            self._text("main.dialog.unsaved_design.title", "Unsaved PCB design"),
            self._text(
                "main.dialog.unsaved_design.message",
                "Save your PCB Designer changes before continuing?",
            ),
            (
                QtWidgets.QMessageBox.StandardButton.Save
                | QtWidgets.QMessageBox.StandardButton.Discard
                | QtWidgets.QMessageBox.StandardButton.Cancel
            ),
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if choice == QtWidgets.QMessageBox.StandardButton.Save:
            return self._save_designer_document(self.designer_workspace.document())
        return choice == QtWidgets.QMessageBox.StandardButton.Discard

    @QtCore.Slot(object)
    def _save_designer_document(self, document: EdaProjectDocument) -> bool:
        destination = self.designer_workspace.document_path
        if destination is None:
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                self._text("main.dialog.save_design.title", "Save editable PCB design"),
                f"{document.name}.smdeda",
                self._text("main.dialog.save_design.filter", "SMD EDA designs (*.smdeda)"),
            )
            if not filename:
                return False
            destination = Path(filename)
            if destination.suffix.casefold() != ".smdeda":
                destination = destination.with_suffix(".smdeda")
        try:
            saved = self._designer_repository.save(document, destination)
        except Exception as error:  # noqa: BLE001 - filesystem failures are user-visible
            QtWidgets.QMessageBox.warning(
                self,
                self._text("main.dialog.save_design.failed_title", "Could not save design"),
                str(error),
            )
            return False
        self.designer_workspace.mark_saved(saved)
        self._clear_designer_status_reference()
        return True

    @QtCore.Slot(object)
    def _simulate_designer_document(self, document: EdaProjectDocument) -> None:
        self._clear_designer_status_reference()
        project_id = document.project_id
        revision = document.revision

        def operation() -> object:
            circuit = CircuitCompiler().compile(document)
            if self._designer_spice.available:
                return self._designer_spice.run(circuit)
            return DcMnaSolver().solve(circuit)

        self.designer_workspace.simulate_action.setEnabled(False)
        self._start_designer_task(
            operation,
            lambda result: self._show_designer_simulation(result, project_id, revision),
        )

    def _show_designer_simulation(
        self,
        result: object,
        project_id: str,
        revision: int,
    ) -> None:
        current = self.designer_workspace.document()
        if current.project_id != project_id or current.revision != revision:
            self._set_designer_status(
                "main.designer.result_stale",
                "Design changed while the calculation ran; the stale result was discarded.",
            )
            return
        if not bool(getattr(result, "success", False)):
            issues = getattr(result, "issues", ())
            if issues:
                self._set_designer_issue_status(issues[-1])
            else:
                self._set_designer_status(
                    "main.designer.simulation_failed", "Circuit simulation failed"
                )
            return
        voltages = dict(getattr(result, "node_voltages", ()))
        probe = (
            "VOUT"
            if "VOUT" in voltages
            else next(
                (name for name in voltages if name.upper() not in {"0", "GND", "GROUND"}),
                "GND",
            )
        )
        engine = str(getattr(result, "engine", "internal MNA"))
        self._set_designer_status(
            "main.designer.simulation_result",
            "{engine}: {probe} = {voltage} V",
            parameters={
                "engine": engine,
                "probe": probe,
                "voltage": f"{voltages.get(probe, 0.0):.6g}",
            },
        )

    @QtCore.Slot(object)
    def _export_designer_document(self, document: EdaProjectDocument) -> None:
        self._clear_designer_status_reference()
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self._text(
                "main.dialog.export_design.title",
                "Select a new or empty folder for the KiCad project",
            ),
        )
        if not selected:
            return
        destination = Path(selected)
        project_id = document.project_id
        revision = document.revision

        def operation() -> tuple[ExportReport, FabricationManifest | None]:
            report = self._designer_bridge.export_new(document, destination)
            manifest = None
            if report.success and report.generated is not None and report.validation is not None:
                manifest = FabricationPackager(self._designer_bridge).build(
                    report.generated,
                    report.validation,
                    destination / "manufacturing",
                )
            return report, manifest

        self.designer_workspace.export_action.setEnabled(False)
        self._start_designer_task(
            operation,
            lambda outcome: self._show_designer_export(outcome, project_id, revision),
        )

    def _show_designer_export(
        self,
        outcome: object,
        project_id: str,
        revision: int,
    ) -> None:
        current = self.designer_workspace.document()
        if current.project_id != project_id or current.revision != revision:
            self._set_designer_status(
                "main.designer.result_stale",
                "Design changed while the calculation ran; the stale result was discarded.",
            )
            return
        if not isinstance(outcome, tuple) or len(outcome) != 2:
            self._show_designer_error(
                self._text("main.designer.export_failed", "KiCad verification failed")
            )
            return
        report, manifest = outcome
        if (
            not isinstance(report, ExportReport)
            or not report.success
            or not isinstance(manifest, FabricationManifest)
        ):
            diagnostics = getattr(report, "diagnostics", ())
            detail = (
                diagnostics[-1].message
                if diagnostics
                else self._text("main.designer.export_failed", "KiCad verification failed")
            )
            self._set_designer_raw_status(str(detail))
            QtWidgets.QMessageBox.warning(
                self,
                self._text("main.dialog.export_design.failed_title", "Could not export design"),
                str(detail),
            )
            return
        file_count = len(getattr(manifest, "files", ()))
        message = self._text(
            "main.designer.export_complete",
            "KiCad verification passed; {count} manufacturing files were created.",
            parameters={"count": file_count},
            count=file_count,
        )
        self._set_designer_status(
            "main.designer.export_complete",
            "KiCad verification passed; {count} manufacturing files were created.",
            parameters={"count": file_count},
            count=file_count,
        )
        QtWidgets.QMessageBox.information(
            self,
            self._text("main.dialog.export_design.complete_title", "Design export complete"),
            message,
        )

    def _start_designer_task(
        self,
        operation: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None] | None = None,
    ) -> None:
        task = _FunctionTask(operation)
        self._designer_jobs.add(task)
        task.signals.succeeded.connect(succeeded)
        task.signals.failed.connect(failed or self._show_designer_error)
        task.signals.finished.connect(lambda: self._finish_designer_task(task))
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(str)
    def _show_designer_error(self, details: str) -> None:
        summary = (
            details.strip().splitlines()[-1]
            if details.strip()
            else self._text("main.designer.operation_failed", "Designer operation failed")
        )
        self._set_designer_raw_status(summary)

    def _clear_designer_status_reference(self) -> None:
        self._designer_status_message = None
        self._designer_status_issue = None

    def _set_designer_status(
        self,
        message_id: str,
        fallback: str,
        *,
        parameters: dict[str, object] | None = None,
        count: int | None = None,
    ) -> None:
        self._designer_status_message = (message_id, fallback, parameters, count)
        self._designer_status_issue = None
        self.designer_workspace.status_label.setToolTip("")
        self.designer_workspace.set_status_text(
            self._text(
                message_id,
                fallback,
                parameters=parameters,
                count=count,
            )
        )

    def _set_designer_raw_status(self, message: str) -> None:
        self._designer_status_message = None
        self._designer_status_issue = None
        self.designer_workspace.status_label.setToolTip("")
        self.designer_workspace.set_status_text(message)

    def _set_designer_issue_status(self, issue: object) -> None:
        self._designer_status_message = None
        self._designer_status_issue = issue
        self.designer_workspace.set_status_text(issue_text(issue, self.language_manager))
        self.designer_workspace.status_label.setToolTip(str(getattr(issue, "message", "")))

    def _finish_designer_task(self, task: _FunctionTask) -> None:
        self._designer_jobs.discard(task)
        if not self._designer_jobs:
            self.designer_workspace.simulate_action.setEnabled(True)
            self.designer_workspace.export_action.setEnabled(True)
            self.designer_workspace.refresh_libraries_action.setEnabled(True)

    @QtCore.Slot()
    def _choose_project(self) -> None:
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self._text("main.dialog.open_project.title", "Open KiCad or normalized project"),
            "",
            (
                self._text(
                    "main.dialog.open_project.supported",
                    "Supported projects (*.kicad_pro project.json *.smdtwin)",
                )
                + ";;"
                + self._text("main.dialog.open_project.kicad", "KiCad projects (*.kicad_pro)")
                + ";;"
                + self._text(
                    "main.dialog.open_project.normalized",
                    "Normalized bundles (project.json *.smdtwin)",
                )
            ),
        )
        if not filename:
            return
        if self._project_loader is None:
            QtWidgets.QMessageBox.information(
                self,
                self._text(
                    "main.dialog.importer_unconfigured.title",
                    "Importer not configured",
                ),
                self._text(
                    "main.dialog.importer_unconfigured.message",
                    "This build is running the sample workspace. Project import can be connected "
                    "without changing the UI.",
                ),
            )
            return
        task = _ProjectImportTask(self._project_loader, Path(filename))
        self._import_jobs.add(task)
        task.signals.succeeded.connect(self.set_project)
        task.signals.failed.connect(self._show_import_error)
        task.signals.finished.connect(lambda: self._finish_import(task))
        self.open_action.setEnabled(False)
        self.run_button.setEnabled(False)
        self._show_status(
            "main.status.importing_project",
            "Importing project with KiCad in the background...",
        )
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(str)
    def _show_import_error(self, details: str) -> None:
        summary = (
            details.strip().splitlines()[-1]
            if details.strip()
            else self._text("main.error.unknown_import", "Unknown import error")
        )
        QtWidgets.QMessageBox.warning(
            self,
            self._text("main.dialog.import_failed.title", "Could not import project"),
            summary,
        )
        self._show_raw_status(summary, 12000)

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
            self._text("main.dialog.save_report.title", "Save run report"),
            f"{self.current_report.run_id}.json",
            self._text("main.dialog.save_report.filter", "JSON report (*.json)"),
        )
        if filename and not self.save_report(Path(filename)):
            QtWidgets.QMessageBox.warning(
                self,
                self._text("main.dialog.save_report.failed_title", "Could not save report"),
                self.statusBar().currentMessage(),
            )

    @QtCore.Slot(str)
    def _show_run_error(self, details: str) -> None:
        summary = (
            details.strip().splitlines()[-1]
            if details.strip()
            else self._text("main.error.unknown_worker", "Unknown worker error")
        )
        self._result_mode = "error"
        self._last_run_error_summary = summary
        self._render_run_error()
        self.results_tabs.setCurrentIndex(1)
        self._show_raw_status(summary, 12000)

    def _finish_task(self, task: _ScenarioTask) -> None:
        self._jobs.discard(task)
        self.run_button.setText(self._text("main.scenario.run", "Run scenario"))
        self.run_button.setEnabled(self.project is not None and self._scenario_enabled)
        self.run_progress.setRange(0, 1)
        self.run_progress.setValue(1 if self.current_report else 0)

    @QtCore.Slot(str)
    def _select_language(self, code: str, checked: bool = True) -> None:
        if checked and not self.language_manager.set_language(code):
            for available_code, action in self.language_actions.items():
                action.setChecked(available_code == self.language_manager.current_language)

    @QtCore.Slot(str)
    def _on_language_changed(self, _code: str) -> None:
        self.retranslate_ui()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.LanguageChange and hasattr(self, "language_menu"):
            self.retranslate_ui()

    @QtCore.Slot()
    def retranslate_ui(self) -> None:
        if self._retranslating or not hasattr(self, "language_menu"):
            return
        self._retranslating = True
        try:
            self.file_menu.setTitle(self._text("main.menu.file", "File"))
            self.language_menu.setTitle(self._text("main.menu.language", "Language"))
            self.open_action.setText(self._text("main.action.open_project", "Open project…"))
            self.open_design_action.setText(
                self._text("main.action.open_design", "Open editable design…")
            )
            self.recover_design_action.setText(
                self._text("main.action.recover_design", "Recover autosave…")
            )
            self.save_report_action.setText(self._text("main.action.save_report", "Save report…"))
            self.exit_action.setText(self._text("main.action.exit", "Exit"))
            self.project_toolbar.setWindowTitle(self._text("main.toolbar.project", "Project"))
            for code, action in self.language_actions.items():
                action.setChecked(code == self.language_manager.current_language)

            self.scenario_controls.setTitle(self._text("main.scenario.title", "Scenario"))
            self.temperature_label.setText(self._text("main.scenario.temperature", "Temperature"))
            self.fault_label.setText(self._text("main.scenario.fault", "Fault"))
            self.component_label.setText(self._text("main.scenario.component", "Component"))
            self.net_a_label.setText(self._text("main.scenario.net_a", "Net A"))
            self.net_b_label.setText(self._text("main.scenario.net_b", "Net B"))
            self.resistance_label.setText(self._text("main.scenario.resistance", "Resistance"))
            self.start_label.setText(self._text("main.scenario.start", "Start"))
            self.duration_label.setText(self._text("main.scenario.duration", "Duration"))
            self.value_spin.setToolTip(
                self._text(
                    "main.scenario.resistance_tooltip",
                    "Replacement resistance in ohms",
                )
            )
            for kind, (message_id, fallback) in _FAULT_LABELS.items():
                index = self.fault_combo.findData(kind)
                if index >= 0:
                    self.fault_combo.setItemText(index, self._text(message_id, fallback))
            self.run_button.setText(
                self._text(
                    "main.scenario.running" if self._jobs else "main.scenario.run",
                    "Running…" if self._jobs else "Run scenario",
                )
            )
            self.run_button.setToolTip(self._scenario_tooltip_text())

            self.board_group.setTitle(self._text("main.board_view", "Board view"))
            self.workspace_tabs.setTabText(
                self.workspace_tabs.indexOf(self.test_lab_workspace),
                self._text("main.workspace.test_lab", "Test Lab"),
            )
            self.workspace_tabs.setTabText(
                self.workspace_tabs.indexOf(self.designer_workspace),
                self._text("main.workspace.designer", "PCB Designer"),
            )
            self.results_tabs.setTabText(0, self._text("main.tab.waveforms", "Waveforms"))
            self.results_tabs.setTabText(1, self._text("main.tab.run_report", "Run report"))
            self.results_tabs.setTabText(
                2,
                self._text("main.tab.firmware_timeline", "Firmware & timeline"),
            )
            self.firmware_state_group.setTitle(
                self._text("main.firmware.state_group", "Firmware state")
            )
            self.uart_group.setTitle("UART")
            self.timeline_group.setTitle(self._text("main.firmware.timeline", "Timeline"))
            self.uart_output.setPlaceholderText(
                self._text(
                    "main.firmware.uart_placeholder",
                    "UART messages will appear here",
                )
            )
            self.timeline_table.setHeaderLabels(
                (
                    self._text("main.timeline.time", "Time"),
                    self._text("main.timeline.type", "Type"),
                    self._text("main.timeline.event", "Event"),
                )
            )

            self.browser_dock.setWindowTitle(
                self._text("main.dock.project_browser", "Project browser")
            )
            self.browser_filter.setPlaceholderText(
                self._text(
                    "main.browser.filter_placeholder",
                    "Filter components and nets…",
                )
            )
            self.project_tree.setHeaderLabels(
                (
                    self._text("main.browser.item", "Item"),
                    self._text("main.browser.value_pins", "Value / pins"),
                )
            )
            self.inspector_dock.setWindowTitle(self._text("main.dock.inspector", "Inspector"))
            self.inspector_tabs.setTabText(
                0, self._text("main.inspector.component_net", "Component / net")
            )
            self.inspector_tabs.setTabText(
                1, self._text("main.inspector.capabilities", "Capabilities")
            )
            self.teaching_dock.setWindowTitle(self._text("main.dock.learn", "Learn while testing"))

            self.capability_panel.retranslate_ui()
            for child in (
                self.board_canvas,
                self.waveform_view,
                self.teaching_panel,
                self.designer_workspace,
            ):
                retranslate = getattr(child, "retranslate_ui", None)
                if callable(retranslate):
                    retranslate()
            if self._designer_status_message is not None:
                message_id, fallback, parameters, count = self._designer_status_message
                self.designer_workspace.set_status_text(
                    self._text(
                        message_id,
                        fallback,
                        parameters=parameters,
                        count=count,
                    )
                )
            elif self._designer_status_issue is not None:
                self.designer_workspace.set_status_text(
                    issue_text(self._designer_status_issue, self.language_manager)
                )

            self._render_project_title()
            self._retranslate_browser_tree()
            self._retranslate_inspector()
            if self.current_report is not None:
                self._render_report_summary(self.current_report)
                self._render_firmware(self.current_report)
            elif self._result_mode == "error":
                self._render_run_error()
            else:
                self._render_empty_report()
                self.firmware_state_label.setText(self._text("main.firmware.not_run", "NOT RUN"))

            if self._status_message is not None and self.statusBar().currentMessage():
                message_id, fallback, parameters, count, timeout = self._status_message
                parameters = self._localized_status_parameters(message_id, parameters)
                self.statusBar().showMessage(
                    self._text(
                        message_id,
                        fallback,
                        parameters=parameters,
                        count=count,
                    ),
                    timeout,
                )
        finally:
            self._retranslating = False

    def _render_project_title(self) -> None:
        if self.project is None:
            self.project_title.setText(self._text("main.project.no_project", "No project"))
            return
        component_count = len(self.project.components)
        net_count = len(self.project.nets)
        components = self._text(
            "main.count.components",
            f"{component_count} component"
            if component_count == 1
            else f"{component_count} components",
            parameters={"count": component_count},
            count=component_count,
        )
        nets = self._text(
            "main.count.nets",
            f"{net_count} net" if net_count == 1 else f"{net_count} nets",
            parameters={"count": net_count},
            count=net_count,
        )
        variant = self._text("main.project.variant", "Variant")
        self.project_title.setText(
            self._text(
                "main.project.summary",
                "{project}  •  {variant_label}: {variant}  •  {components} / {nets}",
                parameters={
                    "project": self.project.name,
                    "variant_label": variant,
                    "variant": self.project.variant,
                    "components": components,
                    "nets": nets,
                },
            )
        )

    def _retranslate_browser_tree(self) -> None:
        if not hasattr(self, "_component_root") or self.project is None:
            return
        self._component_root.setText(0, self._text("main.browser.components", "Components"))
        self._net_root.setText(0, self._text("main.browser.nets", "Nets"))
        for index in range(self._net_root.childCount()):
            item = self._net_root.child(index)
            pin_count = item.data(1, QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(pin_count, int):
                continue
            item.setText(
                1,
                self._text(
                    "main.count.pins",
                    f"{pin_count} pin" if pin_count == 1 else f"{pin_count} pins",
                    parameters={"count": pin_count},
                    count=pin_count,
                ),
            )
        self._filter_browser(self.browser_filter.text())

    def _retranslate_inspector(self) -> None:
        current = self.project_tree.currentItem()
        if current is not None and self.project is not None:
            self._inspect_tree_item(current, current, update_teaching=False)
            return
        selection = self._text("main.inspector.selection", "Selection")
        prompt = self._text("main.inspector.select_prompt", "Select a component or net.")
        self.inspector.setHtml(f"<h3>{escape(selection)}</h3><p>{escape(prompt)}</p>")

    def _firmware_state_text(self, value: str) -> str:
        fallbacks = {
            "NORMAL": "NORMAL",
            "ALARM": "ALARM",
            "SENSOR_FAULT": "SENSOR FAULT",
        }
        return self._text(f"main.firmware_state.{value.casefold()}", fallbacks.get(value, value))

    def _fault_kind_text(self, kind: FaultKind) -> str:
        message_id, fallback = _FAULT_LABELS[kind]
        return self._text(message_id, fallback)

    def _scenario_tooltip_text(self) -> str:
        if self._scenario_tooltip_is_default:
            return self._text("main.scenario.run_tooltip", "Run a scenario")
        known_messages = {
            (
                "This project can be inspected, but no supported circuit/firmware plugin "
                "is configured for it."
            ): "main.scenario.gate.no_plugin",
            "Circuit simulation is unavailable for this project.": (
                "main.scenario.gate.circuit_unavailable"
            ),
            "Firmware simulation is unavailable for this project.": (
                "main.scenario.gate.firmware_unavailable"
            ),
            "Run the validated sensor/status reference model.": (
                "main.scenario.gate.reference_model"
            ),
        }
        message_id = known_messages.get(self._scenario_tooltip_detail)
        if message_id is None:
            return self._scenario_tooltip_detail
        return self._text(message_id, self._scenario_tooltip_detail)

    def _show_status(
        self,
        message_id: str,
        fallback: str,
        *,
        parameters: dict[str, object] | None = None,
        count: int | None = None,
        timeout: int = 0,
    ) -> None:
        self._status_message = (message_id, fallback, parameters, count, timeout)
        parameters = self._localized_status_parameters(message_id, parameters)
        self.statusBar().showMessage(
            self._text(
                message_id,
                fallback,
                parameters=parameters,
                count=count,
            ),
            timeout,
        )

    def _localized_status_parameters(
        self,
        message_id: str,
        parameters: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if message_id != "main.status.running_scenario" or self._active_scenario is None:
            return parameters
        localized = dict(parameters or {})
        localized["scenario"] = self._fault_kind_text(self._active_scenario.fault.kind)
        return localized

    def _show_raw_status(self, message: str, timeout: int = 0) -> None:
        self._status_message = None
        self.statusBar().showMessage(message, timeout)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt API
        if not self._confirm_discard_designer_changes():
            event.ignore()
            return
        self._designer_autosave_timer.stop()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QDialog { background: #eef2f6; color: #1b2b3c; }
            QWidget { color: #1b2b3c; }
            QMenuBar { background: #f7f9fb; color: #1b2b3c;
                       border-bottom: 1px solid #c9d2dd; }
            QMenuBar::item { background: transparent; color: #1b2b3c; padding: 6px 10px; }
            QMenuBar::item:selected, QMenuBar::item:pressed { background: #dce6ef; }
            QMenu { background: white; color: #1b2b3c; border: 1px solid #aebac7; }
            QMenu::item { color: #1b2b3c; padding: 6px 24px 6px 10px; }
            QMenu::item:selected { background: #1769aa; color: white; }
            QMenu::item:disabled { color: #596978; }
            QToolBar { background: #eef2f6; color: #1b2b3c;
                       border-bottom: 1px solid #c9d2dd; spacing: 3px; }
            QToolButton { background: transparent; color: #1b2b3c; padding: 5px 8px; }
            QToolButton:hover { background: #dce6ef; }
            QToolButton:disabled { color: #596978; }
            QStatusBar { background: #eef2f6; color: #33475b; }
            QLabel#project_title { font-size: 17px; font-weight: 650; color: #1b2b3c;
                                   padding: 4px; }
            QLabel { color: #1b2b3c; }
            QGroupBox { color: #1b2b3c; font-weight: 600; border: 1px solid #c9d2dd;
                        border-radius: 6px; margin-top: 8px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QPushButton { background: #f7f9fb; color: #1b2b3c; border: 1px solid #aebac7;
                          border-radius: 4px; padding: 6px 12px; }
            QPushButton:hover { background: #e5edf4; }
            QPushButton:disabled { background: #e2e8ee; color: #596978;
                                   border-color: #c5ced8; }
            QPushButton#run_scenario_button { background: #1769aa; color: white; font-weight: 650;
                                              border: 0; border-radius: 4px; min-height: 25px; }
            QPushButton#run_scenario_button:disabled { background: #9aabba; color: #23384b; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: white; color: #1b2b3c; border: 1px solid #aebac7;
                border-radius: 3px; padding: 3px 5px; selection-background-color: #1769aa;
                selection-color: white;
            }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
            QDoubleSpinBox:disabled { background: #e2e8ee; color: #596978;
                                      border-color: #c5ced8; }
            QComboBox QAbstractItemView { background: white; color: #1b2b3c;
                                          selection-background-color: #1769aa;
                                          selection-color: white; }
            QTreeWidget, QTextBrowser, QPlainTextEdit { background: white; color: #1b2b3c;
                                                        border: 1px solid #c9d2dd;
                                                        selection-background-color: #1769aa;
                                                        selection-color: white; }
            QHeaderView::section { background: #e5ebf1; color: #1b2b3c;
                                   border: 0; border-right: 1px solid #c9d2dd;
                                   border-bottom: 1px solid #c9d2dd; padding: 4px; }
            QTabBar::tab { background: #e5ebf1; color: #1b2b3c;
                           border: 1px solid #c9d2dd; padding: 5px 10px; }
            QTabBar::tab:selected { background: white; color: #102235; }
            QDockWidget { color: #1b2b3c; }
            QDockWidget::title { background: #d7e1eb; color: #1b2b3c;
                                 padding: 6px; font-weight: 600; }
            QToolTip { background: white; color: #1b2b3c; border: 1px solid #8795a5; }
            """
        )
