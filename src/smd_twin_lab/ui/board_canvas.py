"""Zoomable component placement views for both PCB sides."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..localization import LanguageManager, current_language_manager
from ..models import Component, ComponentSide, ImportedProject

try:
    from PySide6.QtSvgWidgets import QGraphicsSvgItem
except ImportError:  # pragma: no cover - depends on the Qt wheel split
    QGraphicsSvgItem = None  # type: ignore[assignment,misc]


_REFERENCE_ROLE = 1


class BoardView(QtWidgets.QGraphicsView):
    """Render normalized placement data without requiring KiCad at runtime."""

    component_selected = QtCore.Signal(str)

    def __init__(
        self,
        side: ComponentSide,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.side = side
        self.language_manager = language_manager or current_language_manager()
        self._project: ImportedProject | None = None
        self._component_items: dict[str, QtWidgets.QGraphicsRectItem] = {}
        self._components: dict[str, Component] = {}
        self._empty_text_item: QtWidgets.QGraphicsTextItem | None = None
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QtGui.QColor("#141b25"))
        self.scene().selectionChanged.connect(self._emit_selection)

    def set_project(self, project: ImportedProject | None) -> None:
        self._project = project
        self.scene().clear()
        self._component_items.clear()
        self._components.clear()
        self._empty_text_item = None
        if project is None:
            self._draw_empty_state()
            return

        geometry = project.geometry
        width = max(1.0, geometry.max_x_mm - geometry.min_x_mm)
        height = max(1.0, geometry.max_y_mm - geometry.min_y_mm)
        board_rect = QtCore.QRectF(geometry.min_x_mm, geometry.min_y_mm, width, height)
        self._add_optional_svg(project, board_rect)
        outline_path = QtGui.QPainterPath()
        outline_path.addRoundedRect(board_rect, 1.6, 1.6)
        outline = self.scene().addPath(
            outline_path,
            QtGui.QPen(QtGui.QColor("#7ad7a8"), 0.55),
            QtGui.QBrush(QtGui.QColor(35, 73, 61, 110)),
        )
        outline.setZValue(-5.0)

        for component in project.components:
            if not self._belongs_on_view(component):
                continue
            self._add_component(component, project)

        self.scene().setSceneRect(board_rect.adjusted(-5.0, -5.0, 5.0, 5.0))
        self.fit_board()

    def fit_board(self) -> None:
        if not self.scene().items():
            return
        self.resetTransform()
        self.fitInView(self.scene().sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def select_reference(self, reference: str) -> None:
        self.scene().blockSignals(True)
        try:
            for name, item in self._component_items.items():
                item.setSelected(name == reference)
        finally:
            self.scene().blockSignals(False)
        item = self._component_items.get(reference)
        if item is not None:
            self.centerOn(item)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802 - Qt API
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        projected = self.transform().m11() * factor
        if 0.08 <= projected <= 80.0:
            self.scale(factor, factor)
        event.accept()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.fit_board()

    def _belongs_on_view(self, component: Component) -> bool:
        return component.side in {self.side, ComponentSide.UNKNOWN}

    def _display_x(self, x_mm: float, project: ImportedProject) -> float:
        if self.side is ComponentSide.BACK:
            geometry = project.geometry
            return geometry.max_x_mm - (x_mm - geometry.min_x_mm)
        return x_mm

    def _display_y(self, y_mm: float, project: ImportedProject) -> float:
        geometry = project.geometry
        return geometry.max_y_mm - (y_mm - geometry.min_y_mm)

    def _add_component(self, component: Component, project: ImportedProject) -> None:
        if component.x_mm is None or component.y_mm is None:
            return
        width, height = _component_size(component)
        x_pos = self._display_x(component.x_mm, project)
        y_pos = self._display_y(component.y_mm, project)
        rect = QtCore.QRectF(-width / 2.0, -height / 2.0, width, height)
        item = self.scene().addRect(
            rect,
            QtGui.QPen(QtGui.QColor("#d7ecff"), 0.35),
            QtGui.QBrush(QtGui.QColor("#397ab8") if component.is_smd else QtGui.QColor("#8467a9")),
        )
        item.setPos(x_pos, y_pos)
        rotation = -component.rotation_deg
        if self.side is ComponentSide.BACK:
            rotation = component.rotation_deg
        item.setRotation(rotation)
        item.setToolTip(_component_tooltip(component, self.language_manager))
        item.setData(_REFERENCE_ROLE, component.reference)
        item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        item.setZValue(2.0)
        self._component_items[component.reference] = item
        self._components[component.reference] = component

        label = QtWidgets.QGraphicsSimpleTextItem(component.reference, item)
        label.setBrush(QtGui.QBrush(QtGui.QColor("white")))
        font = label.font()
        font.setPointSizeF(2.4)
        font.setBold(True)
        label.setFont(font)
        bounds = label.boundingRect()
        label.setPos(-bounds.width() / 2.0, -bounds.height() / 2.0)
        label.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def _add_optional_svg(self, project: ImportedProject, board_rect: QtCore.QRectF) -> None:
        if QGraphicsSvgItem is None:
            return
        candidate = (
            project.geometry.top_preview_path
            if self.side is ComponentSide.FRONT
            else project.geometry.bottom_preview_path
        )
        candidate = candidate or project.geometry.outline_path
        if not candidate:
            return
        path = Path(candidate)
        if not path.is_absolute() and project.source_dir:
            path = Path(project.source_dir) / path
        if not path.is_file() or path.suffix.lower() != ".svg":
            return
        svg_item = QGraphicsSvgItem(str(path))
        bounds = svg_item.boundingRect()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        scale = min(board_rect.width() / bounds.width(), board_rect.height() / bounds.height())
        svg_item.setScale(scale)
        svg_item.setPos(board_rect.topLeft())
        svg_item.setOpacity(0.55)
        svg_item.setZValue(-10.0)
        self.scene().addItem(svg_item)

    def _draw_empty_state(self) -> None:
        text = self.scene().addText("")
        text.setDefaultTextColor(QtGui.QColor("#aebdce"))
        self._empty_text_item = text
        self.retranslate_ui()
        self.scene().setSceneRect(text.boundingRect().adjusted(-20, -20, 20, 20))

    def retranslate_ui(self) -> None:
        if self._empty_text_item is not None:
            self._empty_text_item.setPlainText(
                self.language_manager.text(
                    "board.empty",
                    "Open a project to inspect board placement",
                )
            )
            self.scene().setSceneRect(
                self._empty_text_item.boundingRect().adjusted(-20, -20, 20, 20)
            )
        for reference, item in self._component_items.items():
            component = self._components.get(reference)
            if component is not None:
                item.setToolTip(_component_tooltip(component, self.language_manager))

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.LanguageChange:
            self.retranslate_ui()

    @QtCore.Slot()
    def _emit_selection(self) -> None:
        for item in self.scene().selectedItems():
            reference = item.data(_REFERENCE_ROLE)
            if reference:
                self.component_selected.emit(str(reference))
                return


class BoardCanvas(QtWidgets.QWidget):
    """Tabbed top/bottom board viewer with explicit fit controls."""

    component_selected = QtCore.Signal(str)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.language_manager = language_manager or current_language_manager()
        self.setMinimumHeight(250)
        self.tabs = QtWidgets.QTabWidget()
        self.top_view = BoardView(ComponentSide.FRONT, language_manager=self.language_manager)
        self.bottom_view = BoardView(ComponentSide.BACK, language_manager=self.language_manager)
        self.tabs.addTab(self.top_view, "")
        self.tabs.addTab(self.bottom_view, "")
        self.top_view.component_selected.connect(self.component_selected)
        self.bottom_view.component_selected.connect(self.component_selected)

        self.fit_button = QtWidgets.QPushButton()
        self.fit_button.clicked.connect(self._fit_current)
        self.hint = QtWidgets.QLabel()
        self.hint.setStyleSheet("color: #526273")
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.hint)
        controls.addStretch(1)
        controls.addWidget(self.fit_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.tabs, 1)
        self.retranslate_ui()

    def set_project(self, project: ImportedProject | None) -> None:
        self.top_view.set_project(project)
        self.bottom_view.set_project(project)

    def select_reference(self, reference: str) -> None:
        self.top_view.select_reference(reference)
        self.bottom_view.select_reference(reference)

    def retranslate_ui(self) -> None:
        self.tabs.setTabText(0, self.language_manager.text("board.side.top", "Top"))
        self.tabs.setTabText(
            1,
            self.language_manager.text("board.side.bottom", "Bottom (mirrored)"),
        )
        self.fit_button.setText(self.language_manager.text("board.fit", "Fit board"))
        self.fit_button.setToolTip(
            self.language_manager.text(
                "board.fit.tooltip",
                "Reset zoom to show the complete board",
            )
        )
        self.hint.setText(
            self.language_manager.text(
                "board.navigation_hint",
                "Wheel: zoom  •  Drag: pan  •  Click: inspect",
            )
        )
        self.top_view.retranslate_ui()
        self.bottom_view.retranslate_ui()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.LanguageChange:
            self.retranslate_ui()

    @QtCore.Slot()
    def _fit_current(self) -> None:
        view = self.tabs.currentWidget()
        if isinstance(view, BoardView):
            view.fit_board()


def _component_size(component: Component) -> tuple[float, float]:
    footprint = component.footprint.lower()
    if "qfn" in footprint or "bga" in footprint:
        return (8.0, 8.0)
    if "connector" in footprint or component.reference.startswith("J"):
        return (7.0, 5.0)
    if component.is_smd:
        return (5.3, 2.8)
    return (6.0, 4.0)


def _component_tooltip(component: Component, language_manager: LanguageManager) -> str:
    nets = ", ".join(component.nets) or language_manager.text(
        "board.component.no_net_data", "No net data"
    )
    footprint = component.footprint or language_manager.text(
        "board.component.unknown_footprint", "Unknown footprint"
    )
    side = language_manager.text(
        f"component.side.{component.side.value}",
        {
            ComponentSide.FRONT: "Front",
            ComponentSide.BACK: "Back",
            ComponentSide.UNKNOWN: "Unknown",
        }[component.side],
    )
    return language_manager.text(
        "board.component.tooltip",
        "{reference}  {value}\n{footprint}\nSide: {side}  •  Nets: {nets}",
        parameters={
            "reference": component.reference,
            "value": component.value,
            "footprint": footprint,
            "side": side,
            "nets": nets,
        },
    )
