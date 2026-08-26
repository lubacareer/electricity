"""Learning-first schematic and PCB construction workspace.

This module deliberately does not reuse :class:`BoardView`: imported KiCad
projects remain read-only, while the widgets below edit an ``EdaProjectDocument``.
The first alpha keeps the interaction surface intentionally small, but its
document changes are real, revisioned, and undoable.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Final

from PySide6 import QtCore, QtGui, QtWidgets

from ..eda.connectivity import SchematicCompiler
from ..eda.library import CatalogRefreshReport, LibraryPartSummary
from ..eda.model import (
    BoardFootprint,
    BoardPad,
    BoardTrack,
    CopperLayer,
    EdaProjectDocument,
    PointNm,
    SchematicPin,
    SchematicSymbol,
    SchematicWire,
    mm,
    new_id,
)
from ..eda.pcb import BoardSynchronizer, DrcEngine
from ..eda.templates import blank_project, divider_project
from ..localization import LanguageManager, current_language_manager

NM_PER_SCENE_UNIT: Final = 1_000_000
PART_MIME: Final = "application/x-smd-twin-part"
ITEM_ID_ROLE: Final = 0
ITEM_KIND_ROLE: Final = 1


_MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "designer.title": ("PCB Designer", "Редактор печатных плат"),
    "designer.project.untitled": ("Untitled board", "Безымянная плата"),
    "designer.project.blank": ("Blank project", "Пустой проект"),
    "designer.project.divider": ("3.3 V voltage divider", "Делитель напряжения 3,3 В"),
    "designer.mode.label": ("Mode", "Режим"),
    "designer.mode.learning": ("Learning", "Обучение"),
    "designer.mode.advanced": ("Advanced", "Расширенный"),
    "designer.new": ("New", "Создать"),
    "designer.new.blank": ("Blank project", "Пустой проект"),
    "designer.new.divider": ("Voltage-divider template", "Шаблон делителя напряжения"),
    "designer.save": ("Save design", "Сохранить проект"),
    "designer.undo": ("Undo", "Отменить"),
    "designer.redo": ("Redo", "Повторить"),
    "designer.delete": ("Delete selection", "Удалить выбранное"),
    "designer.rotate": ("Rotate 90°", "Повернуть на 90°"),
    "designer.fit": ("Fit drawing", "Показать весь чертёж"),
    "designer.validate": ("Run checks", "Запустить проверки"),
    "designer.simulate": ("Simulate", "Моделировать"),
    "designer.export": ("Verify in KiCad…", "Проверить в KiCad…"),
    "designer.palette.title": ("Components", "Компоненты"),
    "designer.palette.filter": ("Filter components…", "Фильтр компонентов…"),
    "designer.palette.add": ("Place component", "Разместить компонент"),
    "designer.palette.help": (
        "Drag a component onto the schematic, or select one and press Place.",
        "Перетащите компонент на схему или выберите его и нажмите «Разместить».",
    ),
    "designer.library.refresh": (
        "Index KiCad libraries",
        "Индексировать библиотеки KiCad",
    ),
    "designer.library.refresh_tooltip": (
        "Build a read-only search index of the installed KiCad libraries.",
        "Создать доступный только для чтения индекс установленных библиотек KiCad.",
    ),
    "designer.library.not_indexed": (
        "Built-in learning parts are ready. Index KiCad to search installed parts.",
        "Учебные компоненты готовы. Проиндексируйте KiCad для поиска установленных компонентов.",
    ),
    "designer.library.indexing": (
        "Indexing installed KiCad libraries…",
        "Индексирование установленных библиотек KiCad…",
    ),
    "designer.library.ready": (
        "{symbols} symbols and {footprints} footprints are searchable.",
        "Доступно для поиска: символов — {symbols}, посадочных мест — {footprints}.",
    ),
    "designer.library.failed": (
        "KiCad library indexing failed: {detail}",
        "Не удалось проиндексировать библиотеки KiCad: {detail}",
    ),
    "designer.library.browse_only": (
        "{identifier} is searchable, but placement is blocked until this construct is supported.",
        "{identifier} доступен для поиска, но размещение заблокировано до поддержки "
        "этой конструкции.",
    ),
    "designer.part.resistor": ("Resistor", "Резистор"),
    "designer.part.voltage_source": ("DC voltage source", "Источник постоянного напряжения"),
    "designer.part.led": ("LED (visual model)", "Светодиод (визуальная модель)"),
    "designer.part.ground": ("Ground", "Земля"),
    "designer.tab.schematic": ("Schematic", "Принципиальная схема"),
    "designer.tab.pcb": ("PCB", "Печатная плата"),
    "designer.tool.select": ("Select / move", "Выбор / перемещение"),
    "designer.tool.wire": ("Wire", "Провод"),
    "designer.tool.route": ("Route track", "Провести дорожку"),
    "designer.route.net": ("Route net", "Цепь дорожки"),
    "designer.inspector.title": ("Inspector", "Инспектор"),
    "designer.inspector.property": ("Property", "Свойство"),
    "designer.inspector.value": ("Value", "Значение"),
    "designer.inspector.empty": (
        "Select a symbol, footprint, wire, or track.",
        "Выберите символ, посадочное место, провод или дорожку.",
    ),
    "designer.property.reference": ("Reference", "Обозначение"),
    "designer.property.component_value": ("Component value", "Значение компонента"),
    "designer.property.kind": ("Kind", "Тип"),
    "designer.property.position": ("Position", "Положение"),
    "designer.property.footprint": ("Footprint", "Посадочное место"),
    "designer.property.layer": ("Layer", "Слой"),
    "designer.property.net": ("Net", "Цепь"),
    "designer.property.width": ("Width", "Ширина"),
    "designer.checks.title": ("Design checks", "Проверки проекта"),
    "designer.checks.ready": (
        "Run checks to inspect schematic connectivity and the board outline.",
        "Запустите проверки соединений схемы и контура печатной платы.",
    ),
    "designer.checks.clean": ("No errors found in this alpha check.", "Ошибок не найдено."),
    "designer.checks.outline": (
        "PCB outline is not closed or has fewer than three corners.",
        "Контур печатной платы не замкнут или содержит меньше трёх углов.",
    ),
    "designer.checks.requested": ("Checks completed", "Проверки завершены"),
    "designer.severity.error": ("ERROR", "ОШИБКА"),
    "designer.severity.warning": ("WARNING", "ПРЕДУПРЕЖДЕНИЕ"),
    "designer.severity.info": ("INFO", "ИНФОРМАЦИЯ"),
    "designer.issue.erc.conflicting_labels": (
        "A net contains conflicting labels.",
        "Одна цепь содержит противоречащие друг другу метки.",
    ),
    "designer.issue.erc.dangling_pin": (
        "A required pin is not connected.",
        "Обязательный вывод не подключён.",
    ),
    "designer.issue.erc.driver_conflict": (
        "A net has more than one output driver.",
        "К цепи подключено более одного выхода.",
    ),
    "designer.issue.erc.duplicate_reference": (
        "A schematic reference is used more than once.",
        "Обозначение на схеме использовано более одного раза.",
    ),
    "designer.issue.erc.unattached_label": (
        "A net label is not attached to a wire or pin.",
        "Метка цепи не присоединена к проводу или выводу.",
    ),
    "designer.issue.erc.wire_without_pin": (
        "A wire is not attached to a component pin.",
        "Провод не присоединён к выводу компонента.",
    ),
    "designer.issue.pcb.footprint_unassigned": (
        "A schematic component has no assigned footprint.",
        "Компоненту на схеме не назначено посадочное место.",
    ),
    "designer.issue.pcb.footprint_pin_missing": (
        "A schematic pin is absent from its footprint.",
        "Вывод схемы отсутствует в посадочном месте.",
    ),
    "designer.issue.pcb.footprint_snapshot_missing": (
        "The selected footprint snapshot is missing.",
        "Снимок выбранного посадочного места отсутствует.",
    ),
    "designer.issue.pcb.footprint_snapshot_invalid": (
        "The selected footprint snapshot is invalid.",
        "Снимок выбранного посадочного места некорректен.",
    ),
    "designer.issue.drc.stale_revision": (
        "The design changed before the check completed.",
        "Проект изменился до завершения проверки.",
    ),
    "designer.issue.drc.outline_open": (
        "The PCB outline must be a closed polygon.",
        "Контур печатной платы должен быть замкнутым многоугольником.",
    ),
    "designer.issue.drc.outline_zero_length": (
        "The PCB outline contains a zero-length edge.",
        "Контур печатной платы содержит ребро нулевой длины.",
    ),
    "designer.issue.drc.outline_self_intersection": (
        "The PCB outline crosses itself.",
        "Контур печатной платы пересекает сам себя.",
    ),
    "designer.issue.drc.track_too_narrow": (
        "A track is narrower than the active design rule.",
        "Дорожка уже, чем разрешает активное правило проектирования.",
    ),
    "designer.issue.drc.track_angle": (
        "A track does not use a 45 or 90 degree segment.",
        "Сегмент дорожки проложен не под углом 45° или 90°.",
    ),
    "designer.issue.drc.via_too_small": (
        "A via diameter is below the active design rule.",
        "Диаметр переходного отверстия меньше, чем разрешает активное правило.",
    ),
    "designer.issue.drc.via_drill_too_small": (
        "A via drill is below the active design rule.",
        "Отверстие перехода меньше, чем разрешает активное правило.",
    ),
    "designer.issue.drc.copper_clearance": (
        "Copper items violate the minimum clearance.",
        "Между медными объектами нарушен минимальный зазор.",
    ),
    "designer.issue.drc.copper_outside_board": (
        "Copper lies outside the PCB outline.",
        "Медь выходит за контур печатной платы.",
    ),
    "designer.issue.drc.copper_to_edge": (
        "Copper is too close to the PCB edge.",
        "Медь расположена слишком близко к краю печатной платы.",
    ),
    "designer.issue.drc.courtyard_overlap": (
        "Component courtyards overlap.",
        "Границы размещения компонентов перекрываются.",
    ),
    "designer.issue.drc.duplicate_reference": (
        "A PCB reference is used more than once.",
        "Обозначение на печатной плате использовано более одного раза.",
    ),
    "designer.issue.drc.footprint_missing": (
        "A schematic component has no PCB footprint.",
        "У компонента схемы нет посадочного места на печатной плате.",
    ),
    "designer.issue.drc.pad_missing": (
        "A schematic pin has no matching PCB pad.",
        "Для вывода схемы нет соответствующей контактной площадки.",
    ),
    "designer.issue.drc.pad_net_mismatch": (
        "A PCB pad is assigned to the wrong net.",
        "Контактная площадка назначена неверной цепи.",
    ),
    "designer.issue.drc.wrong_net_termination": (
        "A track terminates on copper from another net.",
        "Дорожка заканчивается на меди другой цепи.",
    ),
    "designer.issue.drc.unrouted_net": (
        "A PCB net still has disconnected groups.",
        "В цепи печатной платы остались несоединённые группы.",
    ),
    "designer.issue.router.stale_revision": (
        "The design changed before the route could be applied.",
        "Проект изменился до применения маршрута.",
    ),
    "designer.issue.router.too_few_points": (
        "A route needs at least two points.",
        "Маршруту нужны как минимум две точки.",
    ),
    "designer.issue.router.net_missing": (
        "Select a net before routing.",
        "Перед трассировкой выберите цепь.",
    ),
    "designer.issue.router.net_unknown": (
        "The selected net is not present on the PCB.",
        "Выбранная цепь отсутствует на печатной плате.",
    ),
    "designer.issue.router.layer_unavailable": (
        "The selected copper layer is not in the PCB stackup.",
        "Выбранный слой меди отсутствует в стеке печатной платы.",
    ),
    "designer.issue.router.width_too_small": (
        "The route width is below the active design rule.",
        "Ширина маршрута меньше, чем разрешает активное правило.",
    ),
    "designer.issue.router.zero_length": (
        "A route contains a zero-length segment.",
        "Маршрут содержит сегмент нулевой длины.",
    ),
    "designer.issue.router.invalid_angle": (
        "A manual route does not use 45 or 90 degree segments.",
        "Сегменты ручной трассировки должны иметь угол 45° или 90°.",
    ),
    "designer.issue.circuit.unsupported_analysis": (
        "The selected circuit analysis is not supported.",
        "Выбранный вид анализа схемы не поддерживается.",
    ),
    "designer.issue.circuit.conflicting_ground": (
        "Ground symbols or labels refer to different nets.",
        "Символы или метки земли указывают на разные цепи.",
    ),
    "designer.issue.circuit.missing_ground": (
        "The circuit needs a net labelled GND or 0.",
        "Схеме нужна цепь с меткой GND или 0.",
    ),
    "designer.issue.circuit.unsupported_symbol": (
        "A component has no supported DC model.",
        "Для компонента нет поддерживаемой модели постоянного тока.",
    ),
    "designer.issue.circuit.pin_count": (
        "A component has the wrong pin count for its DC model.",
        "Число выводов компонента не соответствует его модели постоянного тока.",
    ),
    "designer.issue.circuit.invalid_component": (
        "A component cannot be compiled into the circuit model.",
        "Компонент нельзя преобразовать в модель схемы.",
    ),
    "designer.issue.circuit.invalid_resistance": (
        "A resistance value must be positive.",
        "Значение сопротивления должно быть положительным.",
    ),
    "designer.issue.circuit.invalid_short_nets": (
        "A short fault needs two distinct existing nets.",
        "Для короткого замыкания нужны две разные существующие цепи.",
    ),
    "designer.issue.circuit.fault_target": (
        "The selected fault target is not a modelled resistor.",
        "Выбранная цель неисправности не является моделируемым резистором.",
    ),
    "designer.issue.circuit.invalid_fault_value": (
        "A wrong-value fault needs a positive finite resistance.",
        "Для неверного номинала нужно положительное конечное сопротивление.",
    ),
    "designer.issue.circuit.singular": (
        "The circuit cannot be solved; check for floating or contradictory nets.",
        "Схему нельзя решить; проверьте плавающие или противоречащие цепи.",
    ),
    "designer.learning.title": ("Learn while designing", "Обучение во время проектирования"),
    "designer.learning.blank": (
        "Start with a voltage-divider template if this is your first circuit. "
        "Place parts on the schematic, connect pins with Wire, then arrange the "
        "matching footprints on the PCB.",
        "Если это ваша первая схема, начните с шаблона делителя напряжения. "
        "Разместите компоненты на схеме, соедините выводы инструментом «Провод», "
        "затем расположите соответствующие посадочные места на печатной плате.",
    ),
    "designer.learning.divider": (
        "Two equal 10 kΩ resistors divide 3.3 V to 1.65 V. Run checks before "
        "simulation; labels join nets even when no line is drawn between them.",
        "Два одинаковых резистора по 10 кОм делят 3,3 В до 1,65 В. Перед "
        "моделированием запустите проверки; одинаковые метки соединяют цепи, "
        "даже если линия между ними не нарисована.",
    ),
    "designer.advanced.note": (
        "Alpha scope: two copper layers, straight 45°/90° tracks, and basic "
        "connectivity checks. Advanced vias, zones, and push-and-shove routing "
        "will arrive in later milestones.",
        "Возможности альфа-версии: два медных слоя, прямые дорожки под углом "
        "45°/90° и базовые проверки соединений. Расширенные переходные отверстия, "
        "зоны и трассировка push-and-shove появятся позже.",
    ),
    "designer.status.ready": ("Designer ready", "Редактор готов"),
    "designer.status.placed": ("Placed {reference}", "Размещён компонент {reference}"),
    "designer.status.wired": ("Wire added", "Провод добавлен"),
    "designer.status.routed": ("Track added on {net}", "Добавлена дорожка цепи {net}"),
    "designer.status.simulation": (
        "Simulation request sent",
        "Запрос на моделирование отправлен",
    ),
    "designer.status.export": (
        "KiCad verification request sent",
        "Запрос на проверку в KiCad отправлен",
    ),
    "designer.status.saved": ("Design marked as saved", "Проект отмечен как сохранённый"),
    "designer.command.place": ("Place {reference}", "Разместить {reference}"),
    "designer.command.move_symbol": ("Move symbol", "Переместить символ"),
    "designer.command.move_footprint": (
        "Move footprint",
        "Переместить посадочное место",
    ),
    "designer.command.add_wire": ("Add wire", "Добавить провод"),
    "designer.command.add_track": ("Add track", "Добавить дорожку"),
    "designer.command.rotate": ("Rotate item", "Повернуть элемент"),
    "designer.command.delete": ("Delete item", "Удалить элемент"),
    "designer.command.mode": ("Change designer mode", "Изменить режим редактора"),
    "designer.symbol": ("Symbol", "Символ"),
    "designer.footprint": ("Footprint", "Посадочное место"),
    "designer.wire": ("Wire", "Провод"),
    "designer.track": ("Track", "Дорожка"),
}


def issue_text(
    issue: object,
    language_manager: LanguageManager | None = None,
) -> str:
    """Render a stable first-party issue summary, preserving unknown messages."""

    raw_message = str(getattr(issue, "message", ""))
    code = getattr(issue, "code", None)
    if not isinstance(code, str):
        return raw_message
    message_id = f"designer.issue.{code}"
    localized = _MESSAGES.get(message_id)
    if localized is None:
        return raw_message
    english, russian = localized
    manager = language_manager or current_language_manager()
    translated = manager.text(message_id, english)
    if manager.current_language == "ru" and translated == english:
        return russian
    return translated


def _scene_point(point: PointNm) -> QtCore.QPointF:
    """Map canonical X-right/Y-up coordinates to Qt's Y-down scene."""

    return QtCore.QPointF(point.x_nm / NM_PER_SCENE_UNIT, -point.y_nm / NM_PER_SCENE_UNIT)


def _model_point(point: QtCore.QPointF) -> PointNm:
    return PointNm(round(point.x() * NM_PER_SCENE_UNIT), round(-point.y() * NM_PER_SCENE_UNIT))


def _snap_point(point: PointNm, grid_nm: int) -> PointNm:
    return PointNm(
        round(point.x_nm / grid_nm) * grid_nm,
        round(point.y_nm / grid_nm) * grid_nm,
    )


def _format_position(point: PointNm) -> str:
    return f"{point.x_nm / NM_PER_SCENE_UNIT:.2f}, {point.y_nm / NM_PER_SCENE_UNIT:.2f} mm"


class ComponentPalette(QtWidgets.QListWidget):
    """Small drag source whose payload is a stable component kind."""

    def startDrag(self, supported_actions: QtCore.Qt.DropAction) -> None:  # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        part_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(part_id, str):
            return
        mime_data = QtCore.QMimeData()
        mime_data.setData(PART_MIME, part_id.encode("utf-8"))
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(QtCore.Qt.DropAction.CopyAction)


class _MoveableDesignItem(QtWidgets.QGraphicsRectItem):
    def __init__(
        self,
        rect: QtCore.QRectF,
        *,
        item_id: str,
        item_kind: str,
        move_callback: object,
        snap_nm: int,
    ) -> None:
        super().__init__(rect)
        self.setData(ITEM_ID_ROLE, item_id)
        self.setData(ITEM_KIND_ROLE, item_kind)
        self.setFlags(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self._move_callback = move_callback
        self._snap_nm = snap_nm
        self._press_position = QtCore.QPointF()

    def mousePressEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        self._press_position = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        snapped = _snap_point(_model_point(self.pos()), self._snap_nm)
        self.setPos(_scene_point(snapped))
        if self.pos() != self._press_position and callable(self._move_callback):
            self._move_callback(
                str(self.data(ITEM_ID_ROLE)),
                _model_point(self._press_position),
                snapped,
            )


class _DesignView(QtWidgets.QGraphicsView):
    component_dropped = QtCore.Signal(str, object)
    item_selected = QtCore.Signal(str, str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setAcceptDrops(True)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.RubberBandDrag)
        self.setBackgroundBrush(QtGui.QColor("#f7f9fc"))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scene().selectionChanged.connect(self._selection_changed)
        self._tool = "select"
        self._gesture_start: PointNm | None = None
        self.resetTransform()
        self.scale(8.0, 8.0)
        self.setSceneRect(-10.0, -70.0, 140.0, 90.0)

    @property
    def active_tool(self) -> str:
        return self._tool

    def set_active_tool(self, tool: str) -> None:
        self._tool = tool
        self._gesture_start = None
        self.viewport().setCursor(
            QtCore.Qt.CursorShape.CrossCursor
            if tool != "select"
            else QtCore.Qt.CursorShape.ArrowCursor
        )

    def fit_drawing(self) -> None:
        bounds = self.scene().itemsBoundingRect().adjusted(-4.0, -4.0, 4.0, 4.0)
        if bounds.isValid() and not bounds.isEmpty():
            self.fitInView(bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def center_scene_point(self) -> QtCore.QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 1.0 <= self.transform().m11() * factor <= 80.0:
            self.scale(factor, factor)
        event.accept()

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasFormat(PART_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:  # noqa: N802
        if event.mimeData().hasFormat(PART_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(PART_MIME):
            super().dropEvent(event)
            return
        part_id = bytes(event.mimeData().data(PART_MIME)).decode("utf-8")
        self.component_dropped.emit(
            part_id,
            _snap_point(
                _model_point(self.mapToScene(event.position().toPoint())),
                mm(1),
            ),
        )
        event.acceptProposedAction()

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:  # noqa: N802
        super().drawBackground(painter, rect)
        painter.save()
        fine_pen = QtGui.QPen(QtGui.QColor("#d7e0e8"), 0)
        coarse_pen = QtGui.QPen(QtGui.QColor("#aebdca"), 0)
        left = int(rect.left()) - 1
        right = int(rect.right()) + 1
        top = int(rect.top()) - 1
        bottom = int(rect.bottom()) + 1
        for x in range(left, right + 1):
            painter.setPen(coarse_pen if x % 5 == 0 else fine_pen)
            painter.drawLine(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
        for y in range(top, bottom + 1):
            painter.setPen(coarse_pen if y % 5 == 0 else fine_pen)
            painter.drawLine(QtCore.QLineF(rect.left(), y, rect.right(), y))
        painter.restore()

    def _selection_changed(self) -> None:
        selected = self.scene().selectedItems()
        if not selected:
            self.item_selected.emit("", "")
            return
        item = selected[0]
        item_id = item.data(ITEM_ID_ROLE)
        item_kind = item.data(ITEM_KIND_ROLE)
        if isinstance(item_id, str) and isinstance(item_kind, str):
            self.item_selected.emit(item_id, item_kind)


class SchematicEditorView(_DesignView):
    """Editable schematic canvas with explicit two-click wiring."""

    symbol_moved = QtCore.Signal(str, object, object)
    wire_requested = QtCore.Signal(object, object)

    def render_document(self, document: EdaProjectDocument, selected_id: str = "") -> None:
        transform = QtGui.QTransform(self.transform())
        center = self.center_scene_point()
        self.scene().clear()
        wire_pen = QtGui.QPen(QtGui.QColor("#087f5b"), 0.35)
        wire_pen.setCosmetic(True)
        for wire in document.schematic.wires:
            path = QtGui.QPainterPath(_scene_point(wire.points[0]))
            for point in wire.points[1:]:
                path.lineTo(_scene_point(point))
            item = self.scene().addPath(path, wire_pen)
            item.setFlags(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            item.setData(ITEM_ID_ROLE, wire.wire_id)
            item.setData(ITEM_KIND_ROLE, "wire")
            item.setZValue(-2)
            item.setSelected(wire.wire_id == selected_id)

        for label in document.schematic.labels:
            position = _scene_point(label.position)
            marker = self.scene().addEllipse(
                position.x() - 0.3,
                position.y() - 0.3,
                0.6,
                0.6,
                QtGui.QPen(QtGui.QColor("#125b9a"), 0),
                QtGui.QBrush(QtGui.QColor("#125b9a")),
            )
            marker.setZValue(2)
            text = self.scene().addSimpleText(label.text)
            text.setBrush(QtGui.QColor("#16324a"))
            text.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            text.setPos(position + QtCore.QPointF(0.5, -0.5))

        for symbol in document.schematic.symbols:
            item = _MoveableDesignItem(
                QtCore.QRectF(-5.0, -3.0, 10.0, 6.0),
                item_id=symbol.symbol_id,
                item_kind="symbol",
                move_callback=self.symbol_moved.emit,
                snap_nm=mm(1),
            )
            item.setPos(_scene_point(symbol.position))
            item.setRotation(-symbol.rotation_deg)
            item.setPen(QtGui.QPen(QtGui.QColor("#1e4d75"), 0.35))
            item.setBrush(QtGui.QColor("#dcecff"))
            item.setToolTip(f"{symbol.reference} · {symbol.value}")
            item.setSelected(symbol.symbol_id == selected_id)
            self.scene().addItem(item)

            for pin in symbol.pins:
                pin_pos = item.mapFromScene(_scene_point(symbol.pin_position(pin)))
                dot = QtWidgets.QGraphicsEllipseItem(-0.45, -0.45, 0.9, 0.9, item)
                dot.setPos(pin_pos)
                dot.setPen(QtGui.QPen(QtGui.QColor("#6b3fa0"), 0))
                dot.setBrush(QtGui.QColor("#6b3fa0"))

            text = QtWidgets.QGraphicsSimpleTextItem(
                f"{symbol.reference}\n{symbol.value}",
                item,
            )
            text.setBrush(QtGui.QColor("#102a43"))
            text.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            text.setPos(-4.0, -2.0)

        self.setTransform(transform)
        self.centerOn(center)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self.active_tool != "wire" or event.button() is not QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = _model_point(self.mapToScene(event.position().toPoint()))
        point = PointNm(round(point.x_nm / mm(1)) * mm(1), round(point.y_nm / mm(1)) * mm(1))
        if self._gesture_start is None:
            self._gesture_start = point
        else:
            start = self._gesture_start
            self._gesture_start = None
            if start != point:
                self.wire_requested.emit(start, point)
        event.accept()


class PcbEditorView(_DesignView):
    """Editable two-layer PCB canvas with a scoped straight-track gesture."""

    footprint_moved = QtCore.Signal(str, object, object)
    route_requested = QtCore.Signal(object, object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(False)
        self.setBackgroundBrush(QtGui.QColor("#ecf4ee"))

    def render_document(self, document: EdaProjectDocument, selected_id: str = "") -> None:
        transform = QtGui.QTransform(self.transform())
        center = self.center_scene_point()
        self.scene().clear()
        if len(document.board.outline) >= 2:
            path = QtGui.QPainterPath(_scene_point(document.board.outline[0]))
            for point in document.board.outline[1:]:
                path.lineTo(_scene_point(point))
            if document.board.outline[-1] != document.board.outline[0]:
                path.lineTo(_scene_point(document.board.outline[0]))
            outline = self.scene().addPath(path, QtGui.QPen(QtGui.QColor("#243b53"), 0.4))
            outline.setZValue(-5)

        pad_positions_by_net: dict[str, list[QtCore.QPointF]] = {}
        for footprint in document.board.footprints:
            for pad in footprint.pads:
                if pad.net:
                    pad_positions_by_net.setdefault(pad.net, []).append(
                        _scene_point(footprint.pad_position(pad))
                    )
        ratsnest_pen = QtGui.QPen(QtGui.QColor("#67788a"), 0)
        ratsnest_pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        for positions in pad_positions_by_net.values():
            for first, second in zip(positions, positions[1:], strict=False):
                item = self.scene().addLine(QtCore.QLineF(first, second), ratsnest_pen)
                item.setZValue(-4)

        for track in document.board.tracks:
            color = "#c92a2a" if track.layer is CopperLayer.FRONT else "#2455a4"
            pen = QtGui.QPen(QtGui.QColor(color), track.width_nm / NM_PER_SCENE_UNIT)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            item = self.scene().addLine(
                QtCore.QLineF(_scene_point(track.start), _scene_point(track.end)), pen
            )
            item.setFlags(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            item.setData(ITEM_ID_ROLE, track.track_id)
            item.setData(ITEM_KIND_ROLE, "track")
            item.setToolTip(f"{track.net} · {track.layer.value}")
            item.setSelected(track.track_id == selected_id)
            item.setZValue(-1)

        for footprint in document.board.footprints:
            width = max(8.0, footprint.courtyard_width_nm / NM_PER_SCENE_UNIT)
            height = max(5.0, footprint.courtyard_height_nm / NM_PER_SCENE_UNIT)
            item = _MoveableDesignItem(
                QtCore.QRectF(-width / 2, -height / 2, width, height),
                item_id=footprint.footprint_id,
                item_kind="footprint",
                move_callback=self.footprint_moved.emit,
                snap_nm=mm(0.5),
            )
            item.setPos(_scene_point(footprint.position))
            item.setRotation(-footprint.rotation_deg)
            item.setPen(QtGui.QPen(QtGui.QColor("#6b5417"), 0.3))
            item.setBrush(QtGui.QColor("#fff1b8"))
            item.setToolTip(f"{footprint.reference} · {footprint.library_id}")
            item.setSelected(footprint.footprint_id == selected_id)
            self.scene().addItem(item)
            for pad in footprint.pads:
                pad_pos = item.mapFromScene(_scene_point(footprint.pad_position(pad)))
                pad_item = QtWidgets.QGraphicsRectItem(
                    -pad.width_nm / NM_PER_SCENE_UNIT / 2,
                    -pad.height_nm / NM_PER_SCENE_UNIT / 2,
                    pad.width_nm / NM_PER_SCENE_UNIT,
                    pad.height_nm / NM_PER_SCENE_UNIT,
                    item,
                )
                pad_item.setPos(pad_pos)
                pad_item.setPen(QtGui.QPen(QtGui.QColor("#704f00"), 0))
                pad_item.setBrush(QtGui.QColor("#d29c00"))
            text = QtWidgets.QGraphicsSimpleTextItem(footprint.reference, item)
            text.setBrush(QtGui.QColor("#332701"))
            text.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            text.setPos(-width / 2, -height / 2)

        self.setTransform(transform)
        self.centerOn(center)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self.active_tool != "route" or event.button() is not QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = _model_point(self.mapToScene(event.position().toPoint()))
        point = PointNm(
            round(point.x_nm / mm(0.5)) * mm(0.5),
            round(point.y_nm / mm(0.5)) * mm(0.5),
        )
        if self._gesture_start is None:
            self._gesture_start = point
        else:
            start = self._gesture_start
            self._gesture_start = None
            end = _snap_45(start, point)
            if start != end:
                self.route_requested.emit(start, end)
        event.accept()


def _snap_45(start: PointNm, end: PointNm) -> PointNm:
    delta_x = end.x_nm - start.x_nm
    delta_y = end.y_nm - start.y_nm
    absolute_x = abs(delta_x)
    absolute_y = abs(delta_y)
    if absolute_y * 2 < absolute_x:
        return PointNm(end.x_nm, start.y_nm)
    if absolute_x * 2 < absolute_y:
        return PointNm(start.x_nm, end.y_nm)
    distance = max(absolute_x, absolute_y)
    return PointNm(
        start.x_nm + (distance if delta_x >= 0 else -distance),
        start.y_nm + (distance if delta_y >= 0 else -distance),
    )


class _DocumentCommand(QtGui.QUndoCommand):
    def __init__(
        self,
        workspace: DesignerWorkspace,
        before: EdaProjectDocument,
        after: EdaProjectDocument,
        message_id: str,
        parameters: dict[str, object] | None = None,
        selected_id: str = "",
    ) -> None:
        super().__init__()
        self._workspace = workspace
        self._before = before
        self._after = after
        self._message_id = message_id
        self._parameters = parameters or {}
        self._selected_id = selected_id
        self.retranslate()

    def retranslate(self) -> None:
        self.setText(self._workspace._text(self._message_id, **self._parameters))

    def redo(self) -> None:
        self._workspace._apply_edit_state(self._after, self._selected_id)

    def undo(self) -> None:
        self._workspace._apply_edit_state(self._before, self._selected_id)


class DesignerWorkspace(QtWidgets.QWidget):
    """A state-preserving schematic/PCB editor intended for a main-window tab."""

    document_changed = QtCore.Signal(object)
    validation_requested = QtCore.Signal(object)
    simulation_requested = QtCore.Signal(object)
    export_requested = QtCore.Signal(object)
    save_requested = QtCore.Signal(object)
    new_project_requested = QtCore.Signal(str)
    selection_changed = QtCore.Signal(str)
    library_refresh_requested = QtCore.Signal()
    library_search_requested = QtCore.Signal(str)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pcb_designer_workspace")
        self.language_manager = language_manager or current_language_manager()
        self._document = blank_project()
        self._document_path: Path | None = None
        self._dirty = False
        self._selected_id = ""
        self._selected_kind = ""
        self._board_synchronizer = BoardSynchronizer()
        self._status_message: tuple[str, dict[str, object]] | None = None
        self._validation_has_run = False
        self._library_results: tuple[LibraryPartSummary, ...] = ()
        self._library_report: CatalogRefreshReport | None = None
        self._build_actions()
        self._build_ui()
        self._connect_signals()
        self.load_document(self._document)
        self.language_manager.language_changed.connect(self._language_changed)
        self.retranslate_ui()

    def _build_actions(self) -> None:
        self.new_blank_action = QtGui.QAction(self)
        self.new_blank_action.setShortcut(QtGui.QKeySequence.StandardKey.New)
        self.new_divider_action = QtGui.QAction(self)
        self.save_action = QtGui.QAction(self)
        self.save_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        self.undo_stack = QtGui.QUndoStack(self)
        self.undo_action = QtGui.QAction(self)
        self.undo_action.setShortcut(QtGui.QKeySequence.StandardKey.Undo)
        self.redo_action = QtGui.QAction(self)
        self.redo_action.setShortcut(QtGui.QKeySequence.StandardKey.Redo)
        self.delete_action = QtGui.QAction(self)
        self.delete_action.setShortcut(QtGui.QKeySequence.StandardKey.Delete)
        self.rotate_action = QtGui.QAction(self)
        self.rotate_action.setShortcut(QtGui.QKeySequence("R"))
        self.fit_action = QtGui.QAction(self)
        self.fit_action.setShortcut(QtGui.QKeySequence("Home"))
        self.validate_action = QtGui.QAction(self)
        self.validate_action.setShortcut(QtGui.QKeySequence("F7"))
        self.simulate_action = QtGui.QAction(self)
        self.simulate_action.setShortcut(QtGui.QKeySequence("F6"))
        self.export_action = QtGui.QAction(self)
        self.refresh_libraries_action = QtGui.QAction(self)

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.toolbar = QtWidgets.QToolBar(self)
        self.toolbar.setObjectName("designer_toolbar")
        self.toolbar.setMovable(False)
        self.new_button = QtWidgets.QToolButton(self.toolbar)
        self.new_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.new_menu = QtWidgets.QMenu(self.new_button)
        self.new_menu.addAction(self.new_blank_action)
        self.new_menu.addAction(self.new_divider_action)
        self.new_button.setMenu(self.new_menu)
        self.toolbar.addWidget(self.new_button)
        self.toolbar.addAction(self.save_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.undo_action)
        self.toolbar.addAction(self.redo_action)
        self.toolbar.addAction(self.delete_action)
        self.toolbar.addAction(self.rotate_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.fit_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.validate_action)
        self.toolbar.addAction(self.simulate_action)
        self.toolbar.addAction(self.export_action)
        root.addWidget(self.toolbar)

        header = QtWidgets.QWidget(self)
        header.setObjectName("designer_header")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        header_layout.setSpacing(8)
        self.project_header = QtWidgets.QLabel(header)
        self.project_header.setObjectName("designer_project_header")
        self.project_header.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header_layout.addWidget(self.project_header, 1)
        self.mode_label = QtWidgets.QLabel(header)
        header_layout.addWidget(self.mode_label)
        self.mode_combo = QtWidgets.QComboBox(header)
        self.mode_combo.setMinimumContentsLength(11)
        header_layout.addWidget(self.mode_combo)
        root.addWidget(header)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.main_splitter.setChildrenCollapsible(False)
        root.addWidget(self.main_splitter, 1)

        palette_container = QtWidgets.QWidget(self.main_splitter)
        palette_layout = QtWidgets.QVBoxLayout(palette_container)
        palette_layout.setContentsMargins(10, 10, 8, 10)
        self.palette_title = QtWidgets.QLabel(palette_container)
        self.palette_title.setObjectName("designer_section_title")
        palette_layout.addWidget(self.palette_title)
        self.palette_filter = QtWidgets.QLineEdit(palette_container)
        self.palette_filter.setClearButtonEnabled(True)
        palette_layout.addWidget(self.palette_filter)
        self.refresh_libraries_button = QtWidgets.QToolButton(palette_container)
        self.refresh_libraries_button.setDefaultAction(self.refresh_libraries_action)
        self.refresh_libraries_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        palette_layout.addWidget(self.refresh_libraries_button)
        self.palette_list = ComponentPalette(palette_container)
        self.palette_list.setDragEnabled(True)
        self.palette_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        palette_layout.addWidget(self.palette_list, 1)
        self.place_button = QtWidgets.QPushButton(palette_container)
        palette_layout.addWidget(self.place_button)
        self.palette_help = QtWidgets.QLabel(palette_container)
        self.palette_help.setWordWrap(True)
        self.palette_help.setObjectName("designer_muted_text")
        palette_layout.addWidget(self.palette_help)
        self.library_status = QtWidgets.QLabel(palette_container)
        self.library_status.setWordWrap(True)
        self.library_status.setObjectName("designer_muted_text")
        palette_layout.addWidget(self.library_status)

        editor_container = QtWidgets.QWidget(self.main_splitter)
        editor_layout = QtWidgets.QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 10, 0, 10)
        editor_layout.setSpacing(6)
        tool_row = QtWidgets.QHBoxLayout()
        self.select_tool = QtWidgets.QToolButton(editor_container)
        self.select_tool.setCheckable(True)
        self.wire_tool = QtWidgets.QToolButton(editor_container)
        self.wire_tool.setCheckable(True)
        self.route_tool = QtWidgets.QToolButton(editor_container)
        self.route_tool.setCheckable(True)
        self.tool_group = QtWidgets.QButtonGroup(self)
        self.tool_group.setExclusive(True)
        for button in (self.select_tool, self.wire_tool, self.route_tool):
            self.tool_group.addButton(button)
            tool_row.addWidget(button)
        self.select_tool.setChecked(True)
        tool_row.addStretch(1)
        self.route_net_label = QtWidgets.QLabel(editor_container)
        tool_row.addWidget(self.route_net_label)
        self.route_net_combo = QtWidgets.QComboBox(editor_container)
        self.route_net_combo.setMinimumWidth(110)
        tool_row.addWidget(self.route_net_combo)
        editor_layout.addLayout(tool_row)
        self.tabs = QtWidgets.QTabWidget(editor_container)
        self.schematic_view = SchematicEditorView(self.tabs)
        self.schematic_view.setObjectName("schematic_editor_view")
        self.pcb_view = PcbEditorView(self.tabs)
        self.pcb_view.setObjectName("pcb_editor_view")
        self.tabs.addTab(self.schematic_view, "")
        self.tabs.addTab(self.pcb_view, "")
        editor_layout.addWidget(self.tabs, 1)

        side_tabs = QtWidgets.QTabWidget(self.main_splitter)
        side_tabs.setDocumentMode(True)
        inspector_container = QtWidgets.QWidget(side_tabs)
        inspector_layout = QtWidgets.QVBoxLayout(inspector_container)
        inspector_layout.setContentsMargins(8, 8, 8, 8)
        self.inspector_hint = QtWidgets.QLabel(inspector_container)
        self.inspector_hint.setWordWrap(True)
        self.inspector_hint.setObjectName("designer_muted_text")
        inspector_layout.addWidget(self.inspector_hint)
        self.properties_panel = QtWidgets.QTreeWidget(inspector_container)
        self.properties_panel.setRootIsDecorated(False)
        self.properties_panel.setAlternatingRowColors(True)
        self.properties_panel.setColumnCount(2)
        inspector_layout.addWidget(self.properties_panel, 1)
        side_tabs.addTab(inspector_container, "")

        checks_container = QtWidgets.QWidget(side_tabs)
        checks_layout = QtWidgets.QVBoxLayout(checks_container)
        checks_layout.setContentsMargins(8, 8, 8, 8)
        self.validation_list = QtWidgets.QTreeWidget(checks_container)
        self.validation_list.setHeaderHidden(True)
        self.validation_list.setRootIsDecorated(False)
        checks_layout.addWidget(self.validation_list)
        side_tabs.addTab(checks_container, "")

        learning_container = QtWidgets.QWidget(side_tabs)
        learning_layout = QtWidgets.QVBoxLayout(learning_container)
        learning_layout.setContentsMargins(8, 8, 8, 8)
        self.teaching_browser = QtWidgets.QTextBrowser(learning_container)
        self.teaching_browser.setOpenExternalLinks(False)
        learning_layout.addWidget(self.teaching_browser)
        side_tabs.addTab(learning_container, "")
        self.side_tabs = side_tabs

        self.main_splitter.setSizes([210, 780, 310])
        self.status_label = QtWidgets.QLabel(self)
        self.status_label.setObjectName("designer_status")
        root.addWidget(self.status_label)
        self._apply_styles()

    def _connect_signals(self) -> None:
        self.new_blank_action.triggered.connect(lambda: self.new_project_requested.emit("blank"))
        self.new_divider_action.triggered.connect(
            lambda: self.new_project_requested.emit("divider")
        )
        self.save_action.triggered.connect(self._request_save)
        self.undo_action.triggered.connect(self._undo)
        self.redo_action.triggered.connect(self._redo)
        self.undo_stack.canUndoChanged.connect(self.undo_action.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.redo_action.setEnabled)
        self.undo_stack.cleanChanged.connect(self._clean_changed)
        self.delete_action.triggered.connect(self.delete_selection)
        self.rotate_action.triggered.connect(self.rotate_selection)
        self.fit_action.triggered.connect(self.fit_active_view)
        self.validate_action.triggered.connect(self.validate_design)
        self.simulate_action.triggered.connect(self.simulate_design)
        self.export_action.triggered.connect(self.request_export)
        self.refresh_libraries_action.triggered.connect(self._request_library_refresh)
        self.palette_filter.textChanged.connect(self._filter_palette)
        self.palette_filter.textChanged.connect(self.library_search_requested)
        self.palette_list.itemSelectionChanged.connect(self._update_place_enabled)
        self.palette_list.itemDoubleClicked.connect(lambda _item: self.place_selected_component())
        self.place_button.clicked.connect(self.place_selected_component)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.select_tool.clicked.connect(lambda: self._set_active_tool("select"))
        self.wire_tool.clicked.connect(lambda: self._set_active_tool("wire"))
        self.route_tool.clicked.connect(lambda: self._set_active_tool("route"))
        self.schematic_view.component_dropped.connect(self.add_component)
        self.schematic_view.symbol_moved.connect(self.move_symbol)
        self.schematic_view.wire_requested.connect(self.add_wire)
        self.pcb_view.footprint_moved.connect(self.move_footprint)
        self.pcb_view.route_requested.connect(self.add_track)
        self.schematic_view.item_selected.connect(self._selection_from_view)
        self.pcb_view.item_selected.connect(self._selection_from_view)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QWidget#pcb_designer_workspace { background: #eef3f7; color: #102a43; }
            QToolBar#designer_toolbar {
                background: #dde7ef; color: #102a43; border: 0;
                border-bottom: 1px solid #a9bac8; spacing: 4px; padding: 5px;
            }
            QToolBar#designer_toolbar QToolButton, QToolBar#designer_toolbar QComboBox {
                color: #102a43; background: #f8fafc; border: 1px solid #8799aa;
                border-radius: 4px; padding: 5px 8px;
            }
            QToolBar#designer_toolbar QToolButton:hover,
            QToolBar#designer_toolbar QToolButton:checked {
                background: #d7eafd; border-color: #2067a0;
            }
            QWidget#designer_header {
                color: #102a43; background: #f8fafc; border-bottom: 1px solid #c2ced8;
            }
            QLabel#designer_project_header {
                color: #102a43; background: transparent;
                font-size: 15px; font-weight: 600; padding: 8px 0;
            }
            QLabel#designer_section_title { color: #102a43; font-weight: 600; }
            QLabel#designer_muted_text { color: #40566b; }
            QLabel#designer_status {
                color: #253f57; background: #dde7ef; border-top: 1px solid #a9bac8;
                padding: 5px 10px;
            }
            QLineEdit, QListWidget, QTreeWidget, QTextBrowser, QComboBox, QTabWidget::pane {
                color: #102a43; background: #ffffff; border: 1px solid #a9bac8;
            }
            QListWidget::item:selected, QTreeWidget::item:selected {
                color: #ffffff; background: #1f5f8b;
            }
            QPushButton {
                color: #ffffff; background: #1f5f8b; border: 1px solid #174969;
                border-radius: 4px; padding: 6px 10px;
            }
            QPushButton:hover { background: #174969; }
            QTabBar::tab { color: #243b53; background: #dfe8f0; padding: 7px 10px; }
            QTabBar::tab:selected { color: #102a43; background: #ffffff; }
            """
        )

    def _text(self, message_id: str, **parameters: object) -> str:
        english, russian = _MESSAGES[message_id]
        translated = self.language_manager.text(
            message_id,
            english,
            parameters=parameters or None,
        )
        if self.language_manager.current_language == "ru" and translated == english:
            try:
                return russian.format(**parameters)
            except (KeyError, ValueError):
                return russian
        return translated

    def document(self) -> EdaProjectDocument:
        return self._document

    @property
    def document_path(self) -> Path | None:
        return self._document_path

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def set_document_path(self, path: Path | None) -> None:
        self._document_path = path.resolve() if path is not None else None
        self._update_project_header()

    def mark_saved(self, path: Path | None = None) -> None:
        if path is not None:
            self.set_document_path(path)
        self.undo_stack.setClean()
        self._dirty = False
        self._set_status("designer.status.saved")
        self._update_project_header()

    def new_project(self, template_id: str = "blank") -> None:
        if template_id == "blank":
            document = blank_project()
        elif template_id == "divider":
            document = divider_project()
        else:
            raise ValueError(f"unsupported designer template: {template_id}")
        self.load_document(document)
        self._document_path = None
        self._dirty = False
        self._update_project_header()

    def load_document(self, document: EdaProjectDocument) -> None:
        self._document = document
        self.undo_stack.clear()
        self._update_undo_actions()
        self._dirty = False
        self._selected_id = ""
        self._selected_kind = ""
        blockers = QtCore.QSignalBlocker(self.mode_combo)
        self._populate_modes()
        index = self.mode_combo.findData(document.teaching.mode)
        self.mode_combo.setCurrentIndex(max(index, 0))
        del blockers
        self._refresh_document_views()
        self._show_validation_placeholder()
        self._validation_has_run = False
        self._set_status("designer.status.ready")
        self._update_project_header()
        self.document_changed.emit(self._document)

    def _apply_edit_state(self, target: EdaProjectDocument, selected_id: str = "") -> None:
        current = self._document
        target = self._board_synchronizer.update_from_schematic(target).document
        self._document = replace(
            target,
            manifest=replace(target.manifest, revision=current.revision + 1),
        )
        self._dirty = True
        self._selected_id = selected_id
        self._refresh_document_views()
        self._update_project_header()
        self.document_changed.emit(self._document)

    def _push_document(
        self,
        target: EdaProjectDocument,
        message_id: str,
        *,
        parameters: dict[str, object] | None = None,
        selected_id: str = "",
    ) -> None:
        self.undo_stack.push(
            _DocumentCommand(
                self,
                self._document,
                target,
                message_id,
                parameters,
                selected_id,
            )
        )
        self._update_undo_actions()

    def add_component(self, part_id: str, position: PointNm | None = None) -> None:
        if part_id not in {"resistor", "voltage_source", "led", "ground"}:
            return
        position = _snap_point(position or self._default_placement_position(), mm(1))
        symbol, footprint = _make_component(part_id, self._document, position)
        schematic = replace(
            self._document.schematic,
            symbols=(*self._document.schematic.symbols, symbol),
        )
        board = self._document.board
        if footprint is not None:
            board = replace(board, footprints=(*board.footprints, footprint))
        target = replace(self._document, schematic=schematic, board=board)
        self._push_document(
            target,
            "designer.command.place",
            parameters={"reference": symbol.reference},
            selected_id=symbol.symbol_id,
        )
        self._set_status("designer.status.placed", reference=symbol.reference)

    def place_selected_component(self) -> None:
        item = self.palette_list.currentItem()
        if item is None:
            return
        part_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(part_id, str):
            self.add_component(part_id)

    def set_library_indexing(self) -> None:
        self.refresh_libraries_action.setEnabled(False)
        self.palette_filter.setEnabled(False)
        self.library_status.setText(self._text("designer.library.indexing"))

    def set_library_results(
        self,
        results: tuple[LibraryPartSummary, ...],
        report: CatalogRefreshReport | None = None,
    ) -> None:
        self._library_results = tuple(results)
        if report is not None:
            self._library_report = report
        self.refresh_libraries_action.setEnabled(True)
        self.palette_filter.setEnabled(True)
        self._populate_palette()
        if self._library_report is not None:
            self.library_status.setText(
                self._text(
                    "designer.library.ready",
                    symbols=self._library_report.symbol_count,
                    footprints=self._library_report.footprint_count,
                )
            )

    def set_library_error(self, detail: str, *, enable_refresh: bool = True) -> None:
        self.refresh_libraries_action.setEnabled(enable_refresh)
        self.palette_filter.setEnabled(True)
        self.library_status.setText(self._text("designer.library.failed", detail=detail))

    def _request_library_refresh(self) -> None:
        self.set_library_indexing()
        self.library_refresh_requested.emit()

    def move_symbol(self, symbol_id: str, old: PointNm, new: PointNm) -> None:
        new = _snap_point(new, mm(1))
        if old == new:
            return
        symbols = tuple(
            replace(symbol, position=new) if symbol.symbol_id == symbol_id else symbol
            for symbol in self._document.schematic.symbols
        )
        if symbols == self._document.schematic.symbols:
            return
        target = replace(
            self._document,
            schematic=replace(self._document.schematic, symbols=symbols),
        )
        self._push_document(
            target,
            "designer.command.move_symbol",
            selected_id=symbol_id,
        )

    def move_footprint(self, footprint_id: str, old: PointNm, new: PointNm) -> None:
        new = _snap_point(new, mm(0.5))
        if old == new:
            return
        footprints = tuple(
            replace(footprint, position=new)
            if footprint.footprint_id == footprint_id
            else footprint
            for footprint in self._document.board.footprints
        )
        if footprints == self._document.board.footprints:
            return
        target = replace(
            self._document,
            board=replace(self._document.board, footprints=footprints),
        )
        self._push_document(
            target,
            "designer.command.move_footprint",
            selected_id=footprint_id,
        )

    def add_wire(self, start: PointNm, end: PointNm) -> None:
        if start == end:
            return
        wire = SchematicWire(new_id(), (start, end))
        schematic = replace(
            self._document.schematic,
            wires=(*self._document.schematic.wires, wire),
        )
        self._push_document(
            replace(self._document, schematic=schematic),
            "designer.command.add_wire",
            selected_id=wire.wire_id,
        )
        self._set_status("designer.status.wired")

    def add_track(self, start: PointNm, end: PointNm) -> None:
        if start == end:
            return
        net = str(self.route_net_combo.currentData() or self.route_net_combo.currentText())
        if not net:
            net = "UNASSIGNED"
        track = BoardTrack(
            new_id(),
            net,
            start,
            _snap_45(start, end),
            max(self._document.rules.minimum_track_width_nm, mm(0.25)),
            CopperLayer.FRONT,
        )
        board = replace(self._document.board, tracks=(*self._document.board.tracks, track))
        self._push_document(
            replace(self._document, board=board),
            "designer.command.add_track",
            selected_id=track.track_id,
        )
        self._set_status("designer.status.routed", net=net)

    def rotate_selection(self) -> None:
        if not self._selected_id:
            return
        symbols = tuple(
            replace(symbol, rotation_deg=(symbol.rotation_deg + 90) % 360)
            if symbol.symbol_id == self._selected_id
            else symbol
            for symbol in self._document.schematic.symbols
        )
        footprints = tuple(
            replace(footprint, rotation_deg=(footprint.rotation_deg + 90) % 360)
            if footprint.footprint_id == self._selected_id
            else footprint
            for footprint in self._document.board.footprints
        )
        target = replace(
            self._document,
            schematic=replace(self._document.schematic, symbols=symbols),
            board=replace(self._document.board, footprints=footprints),
        )
        if target != self._document:
            self._push_document(
                target,
                "designer.command.rotate",
                selected_id=self._selected_id,
            )

    def delete_selection(self) -> None:
        selected_id = self._selected_id
        if not selected_id:
            return
        schematic = replace(
            self._document.schematic,
            symbols=tuple(
                item for item in self._document.schematic.symbols if item.symbol_id != selected_id
            ),
            wires=tuple(
                item for item in self._document.schematic.wires if item.wire_id != selected_id
            ),
        )
        board = replace(
            self._document.board,
            footprints=tuple(
                item for item in self._document.board.footprints if item.footprint_id != selected_id
            ),
            tracks=tuple(
                item for item in self._document.board.tracks if item.track_id != selected_id
            ),
        )
        target = replace(self._document, schematic=schematic, board=board)
        if target != self._document:
            self._push_document(target, "designer.command.delete")
            self._selected_id = ""
            self._selected_kind = ""

    def validate_design(self) -> None:
        self._validation_has_run = True
        self._render_validation()
        self._set_status("designer.checks.requested")
        self.validation_requested.emit(self._document)

    def _render_validation(self) -> None:
        graph = SchematicCompiler().compile(self._document)
        board_report = DrcEngine().check(self._document, self._document.revision)
        self.validation_list.clear()
        errors = 0
        for issue in (*graph.issues, *board_report.issues):
            severity = self._text(f"designer.severity.{issue.severity.value}")
            message = issue_text(issue, self.language_manager)
            item = QtWidgets.QTreeWidgetItem([f"{severity}: {message}"])
            item.setToolTip(0, issue.message)
            if issue.severity.value == "error":
                item.setForeground(0, QtGui.QColor("#9b1c1c"))
                errors += 1
            elif issue.severity.value == "warning":
                item.setForeground(0, QtGui.QColor("#7a4b00"))
            self.validation_list.addTopLevelItem(item)
        if self.validation_list.topLevelItemCount() == 0:
            self.validation_list.addTopLevelItem(
                QtWidgets.QTreeWidgetItem([self._text("designer.checks.clean")])
            )
        self.validation_list.setProperty("errorCount", errors)

    def simulate_design(self) -> None:
        self._set_status("designer.status.simulation")
        self.simulation_requested.emit(self._document)

    def request_export(self) -> None:
        self._set_status("designer.status.export")
        self.export_requested.emit(self._document)

    def _set_status(self, message_id: str, **parameters: object) -> None:
        self._status_message = (message_id, dict(parameters))
        self.status_label.setText(self._text(message_id, **parameters))

    def set_status_text(self, text: str) -> None:
        self._status_message = None
        self.status_label.setText(text)

    def _request_save(self) -> None:
        self.save_requested.emit(self._document)

    @QtCore.Slot(bool)
    def _clean_changed(self, clean: bool) -> None:
        self._dirty = not clean
        if hasattr(self, "project_header"):
            self._update_project_header()

    def _undo(self) -> None:
        self.undo_stack.undo()
        self._update_undo_actions()

    def _redo(self) -> None:
        self.undo_stack.redo()
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        undo_text = self.undo_stack.undoText()
        redo_text = self.undo_stack.redoText()
        undo_label = self._text("designer.undo")
        redo_label = self._text("designer.redo")
        self.undo_action.setText(f"{undo_label}: {undo_text}" if undo_text else undo_label)
        self.redo_action.setText(f"{redo_label}: {redo_text}" if redo_text else redo_label)
        self.undo_action.setEnabled(self.undo_stack.canUndo())
        self.redo_action.setEnabled(self.undo_stack.canRedo())

    def fit_active_view(self) -> None:
        view = self.tabs.currentWidget()
        if isinstance(view, _DesignView):
            view.fit_drawing()

    def select_item(self, item_id: str, kind: str | None = None) -> None:
        self._selected_id = item_id
        if kind is not None:
            self._selected_kind = kind
        self._refresh_document_views()
        self._refresh_properties()
        self.selection_changed.emit(item_id)

    def _selection_from_view(self, item_id: str, kind: str) -> None:
        self._selected_id = item_id
        self._selected_kind = kind
        self._refresh_properties()
        self.selection_changed.emit(item_id)

    def _default_placement_position(self) -> PointNm:
        view = self.schematic_view
        center = _model_point(view.center_scene_point())
        count = len(self._document.schematic.symbols)
        return PointNm(center.x_nm + mm((count % 4) * 3), center.y_nm - mm((count % 3) * 3))

    def _set_active_tool(self, tool: str) -> None:
        if tool == "wire":
            self.tabs.setCurrentWidget(self.schematic_view)
        elif tool == "route":
            self.tabs.setCurrentWidget(self.pcb_view)
        self.schematic_view.set_active_tool(tool if tool == "wire" else "select")
        self.pcb_view.set_active_tool(tool if tool == "route" else "select")
        self.select_tool.setChecked(tool == "select")
        self.wire_tool.setChecked(tool == "wire")
        self.route_tool.setChecked(tool == "route")

    def _tab_changed(self, index: int) -> None:
        if (index == 0 and self.route_tool.isChecked()) or (
            index == 1 and self.wire_tool.isChecked()
        ):
            self._set_active_tool("select")

    def _mode_changed(self, _index: int) -> None:
        mode = self.mode_combo.currentData()
        if not isinstance(mode, str) or mode == self._document.teaching.mode:
            return
        teaching = replace(self._document.teaching, mode=mode)
        self._push_document(
            replace(self._document, teaching=teaching),
            "designer.command.mode",
        )
        self._refresh_teaching()

    def _refresh_document_views(self) -> None:
        self.schematic_view.render_document(self._document, self._selected_id)
        self.pcb_view.render_document(self._document, self._selected_id)
        self._refresh_route_nets()
        self._refresh_properties()
        self._refresh_teaching()

    def _refresh_route_nets(self) -> None:
        selected = self.route_net_combo.currentData()
        nets = sorted(
            {
                pad.net
                for footprint in self._document.board.footprints
                for pad in footprint.pads
                if pad.net
            }
        )
        blocker = QtCore.QSignalBlocker(self.route_net_combo)
        self.route_net_combo.clear()
        for net in nets:
            self.route_net_combo.addItem(net, net)
        if selected in nets:
            self.route_net_combo.setCurrentIndex(self.route_net_combo.findData(selected))
        del blocker

    def _refresh_properties(self) -> None:
        self.properties_panel.clear()
        rows: list[tuple[str, str]] = []
        symbol = next(
            (
                item
                for item in self._document.schematic.symbols
                if item.symbol_id == self._selected_id
            ),
            None,
        )
        footprint = next(
            (
                item
                for item in self._document.board.footprints
                if item.footprint_id == self._selected_id
            ),
            None,
        )
        wire = next(
            (item for item in self._document.schematic.wires if item.wire_id == self._selected_id),
            None,
        )
        track = next(
            (item for item in self._document.board.tracks if item.track_id == self._selected_id),
            None,
        )
        if symbol is not None:
            rows = [
                (self._text("designer.property.reference"), symbol.reference),
                (self._text("designer.property.component_value"), symbol.value),
                (self._text("designer.property.kind"), symbol.kind),
                (self._text("designer.property.position"), _format_position(symbol.position)),
                (self._text("designer.property.footprint"), symbol.footprint_id or "—"),
            ]
        elif footprint is not None:
            rows = [
                (self._text("designer.property.reference"), footprint.reference),
                (self._text("designer.property.position"), _format_position(footprint.position)),
                (self._text("designer.property.footprint"), footprint.library_id),
                (self._text("designer.property.layer"), footprint.side.value),
            ]
        elif wire is not None:
            rows = [
                (self._text("designer.property.kind"), self._text("designer.wire")),
                (self._text("designer.property.position"), _format_position(wire.points[0])),
            ]
        elif track is not None:
            rows = [
                (self._text("designer.property.kind"), self._text("designer.track")),
                (self._text("designer.property.net"), track.net),
                (self._text("designer.property.layer"), track.layer.value),
                (
                    self._text("designer.property.width"),
                    f"{track.width_nm / NM_PER_SCENE_UNIT:.3f} mm",
                ),
            ]
        for name, value in rows:
            self.properties_panel.addTopLevelItem(QtWidgets.QTreeWidgetItem([name, value]))
        self.inspector_hint.setVisible(not rows)
        self.properties_panel.resizeColumnToContents(0)

    def _refresh_teaching(self) -> None:
        lesson_id = (
            "designer.learning.divider"
            if self._document.teaching.template_id == "divider"
            else "designer.learning.blank"
        )
        body = self._text(lesson_id)
        if self.mode_combo.currentData() == "advanced":
            body += "\n\n" + self._text("designer.advanced.note")
        self.teaching_browser.setPlainText(body)

    def _show_validation_placeholder(self) -> None:
        self.validation_list.clear()
        self.validation_list.addTopLevelItem(
            QtWidgets.QTreeWidgetItem([self._text("designer.checks.ready")])
        )

    def _filter_palette(self, text: str) -> None:
        needle = text.strip().casefold()
        first_visible: QtWidgets.QListWidgetItem | None = None
        for index in range(self.palette_list.count()):
            item = self.palette_list.item(index)
            hidden = needle not in item.text().casefold()
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        current = self.palette_list.currentItem()
        if current is None or current.isHidden():
            self.palette_list.setCurrentItem(first_visible)
        self._update_place_enabled()

    def _update_place_enabled(self) -> None:
        item = self.palette_list.currentItem()
        part_id = item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        self.place_button.setEnabled(isinstance(part_id, str) and bool(part_id))

    def _populate_palette(self) -> None:
        selected_data = None
        if self.palette_list.currentItem() is not None:
            selected_data = self.palette_list.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)
        parts = (
            ("resistor", "designer.part.resistor", "R"),
            ("voltage_source", "designer.part.voltage_source", "V"),
            ("led", "designer.part.led", "D"),
            ("ground", "designer.part.ground", "GND"),
        )
        blocker = QtCore.QSignalBlocker(self.palette_list)
        self.palette_list.clear()
        for part_id, message_id, prefix in parts:
            item = QtWidgets.QListWidgetItem(f"{prefix}  ·  {self._text(message_id)}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, part_id)
            self.palette_list.addItem(item)
            if part_id == selected_data:
                self.palette_list.setCurrentItem(item)
        for summary in self._library_results:
            kind = "SYM" if summary.kind.value == "symbol" else "FP"
            item = QtWidgets.QListWidgetItem(f"KiCad {kind}  ·  {summary.identifier}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
            item.setForeground(QtGui.QColor("#52677a"))
            item.setToolTip(
                self._text("designer.library.browse_only", identifier=summary.identifier)
            )
            self.palette_list.addItem(item)
        if self.palette_list.currentItem() is None and self.palette_list.count():
            self.palette_list.setCurrentRow(0)
        del blocker
        self._filter_palette(self.palette_filter.text())
        self._update_place_enabled()

    def _populate_modes(self) -> None:
        selected = self.mode_combo.currentData()
        blocker = QtCore.QSignalBlocker(self.mode_combo)
        self.mode_combo.clear()
        self.mode_combo.addItem(self._text("designer.mode.learning"), "learning")
        self.mode_combo.addItem(self._text("designer.mode.advanced"), "advanced")
        index = self.mode_combo.findData(selected)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        del blocker

    def _update_project_header(self) -> None:
        marker = " *" if self._dirty else ""
        path_text = f" — {self._document_path}" if self._document_path else ""
        self.project_header.setText(
            f"{self._text('designer.title')} · {self._document.name}{marker}{path_text}"
        )

    def retranslate_ui(self) -> None:
        selected_mode = self.mode_combo.currentData()
        current_tab = self.tabs.currentIndex()
        current_side_tab = self.side_tabs.currentIndex()
        self.new_button.setText(self._text("designer.new"))
        self.new_blank_action.setText(self._text("designer.new.blank"))
        self.new_divider_action.setText(self._text("designer.new.divider"))
        self.save_action.setText(self._text("designer.save"))
        for index in range(self.undo_stack.count()):
            command = self.undo_stack.command(index)
            if isinstance(command, _DocumentCommand):
                command.retranslate()
        self._update_undo_actions()
        self.delete_action.setText(self._text("designer.delete"))
        self.rotate_action.setText(self._text("designer.rotate"))
        self.fit_action.setText(self._text("designer.fit"))
        self.validate_action.setText(self._text("designer.validate"))
        self.simulate_action.setText(self._text("designer.simulate"))
        self.export_action.setText(self._text("designer.export"))
        self.refresh_libraries_action.setText(self._text("designer.library.refresh"))
        self.refresh_libraries_action.setToolTip(self._text("designer.library.refresh_tooltip"))
        self.mode_label.setText(self._text("designer.mode.label") + ": ")
        self._populate_modes()
        if selected_mode is not None:
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(selected_mode)))
        self.palette_title.setText(self._text("designer.palette.title"))
        self.palette_filter.setPlaceholderText(self._text("designer.palette.filter"))
        self.place_button.setText(self._text("designer.palette.add"))
        self.palette_help.setText(self._text("designer.palette.help"))
        if self._library_report is None:
            self.library_status.setText(self._text("designer.library.not_indexed"))
        else:
            self.library_status.setText(
                self._text(
                    "designer.library.ready",
                    symbols=self._library_report.symbol_count,
                    footprints=self._library_report.footprint_count,
                )
            )
        self._populate_palette()
        self.tabs.setTabText(0, self._text("designer.tab.schematic"))
        self.tabs.setTabText(1, self._text("designer.tab.pcb"))
        self.select_tool.setText(self._text("designer.tool.select"))
        self.wire_tool.setText(self._text("designer.tool.wire"))
        self.route_tool.setText(self._text("designer.tool.route"))
        self.route_net_label.setText(self._text("designer.route.net") + ":")
        self.side_tabs.setTabText(0, self._text("designer.inspector.title"))
        self.side_tabs.setTabText(1, self._text("designer.checks.title"))
        self.side_tabs.setTabText(2, self._text("designer.learning.title"))
        self.properties_panel.setHeaderLabels(
            [
                self._text("designer.inspector.property"),
                self._text("designer.inspector.value"),
            ]
        )
        self.inspector_hint.setText(self._text("designer.inspector.empty"))
        self.tabs.setCurrentIndex(current_tab)
        self.side_tabs.setCurrentIndex(current_side_tab)
        self._refresh_properties()
        self._refresh_teaching()
        self._update_project_header()
        if self._validation_has_run:
            self._render_validation()
        else:
            self._show_validation_placeholder()
        if self._status_message is not None:
            message_id, parameters = self._status_message
            self.status_label.setText(self._text(message_id, **parameters))

    @QtCore.Slot(str)
    def _language_changed(self, _language: str) -> None:
        self.retranslate_ui()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        if event.type() == QtCore.QEvent.Type.LanguageChange and hasattr(self, "tabs"):
            self.retranslate_ui()
        super().changeEvent(event)


def _make_component(
    part_id: str,
    document: EdaProjectDocument,
    position: PointNm,
    *,
    reference: str | None = None,
    value: str | None = None,
) -> tuple[SchematicSymbol, BoardFootprint | None]:
    specifications = {
        "resistor": ("R", "10k", "Resistor_SMD:R_0805_2012Metric", "resistor", 2),
        "voltage_source": ("V", "3.3", "Connector_PinHeader_1x02", "voltage_source", 2),
        "led": ("D", "LED", "LED_SMD:LED_0805_2012Metric", "led", 2),
        "ground": ("GND", "GND", "", "ground", 1),
    }
    prefix, default_value, footprint_id, kind, pin_count = specifications[part_id]
    if reference is None:
        existing = {symbol.reference for symbol in document.schematic.symbols}
        if prefix == "GND":
            sequence = 1
            reference = f"GND{sequence}"
        else:
            sequence = 1
            reference = f"{prefix}{sequence}"
        while reference in existing:
            sequence += 1
            reference = f"{prefix}{sequence}"
    pin_offsets = (
        (PointNm(-mm(5), 0), PointNm(mm(5), 0)) if pin_count == 2 else (PointNm(0, mm(3)),)
    )
    pins = tuple(
        SchematicPin(new_id(), str(index), str(index), offset)
        for index, offset in enumerate(pin_offsets, start=1)
    )
    symbol = SchematicSymbol(
        symbol_id=new_id(),
        reference=reference,
        value=value or default_value,
        library_id=kind,
        kind=kind,
        position=position,
        pins=pins,
        footprint_id=footprint_id,
    )
    if not footprint_id:
        return symbol, None
    board_index = len(document.board.footprints)
    board_position = PointNm(mm(15 + (board_index % 5) * 12), mm(15 + (board_index // 5) * 10))
    pads = tuple(
        BoardPad(
            new_id(),
            str(index),
            PointNm(-mm(2.0) if index == 1 else mm(2.0), 0),
            mm(2.0),
            mm(2.4),
        )
        for index in range(1, pin_count + 1)
    )
    footprint = BoardFootprint(
        footprint_id=new_id(),
        reference=reference,
        library_id=footprint_id,
        symbol_id=symbol.symbol_id,
        position=board_position,
        pads=pads,
        courtyard_width_nm=mm(8),
        courtyard_height_nm=mm(5),
    )
    return symbol, footprint


__all__ = [
    "ComponentPalette",
    "DesignerWorkspace",
    "PcbEditorView",
    "SchematicEditorView",
]
