"""Waveform visualization with a Qt-only fallback."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from ..localization import LanguageManager, current_language_manager
from ..models import SignalSeries

try:
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - dependency-free fallback
    pg = None


class _PainterWaveform(QtWidgets.QWidget):
    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.language_manager = language_manager or current_language_manager()
        self._signals: tuple[SignalSeries, ...] = ()
        self.setMinimumHeight(180)
        self.setAutoFillBackground(True)

    def set_signals(self, signals: Sequence[SignalSeries]) -> None:
        self._signals = tuple(signals)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt API
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#101620"))
        plot = self.rect().adjusted(48, 20, -18, -35)
        painter.setPen(QtGui.QPen(QtGui.QColor("#405064"), 1))
        painter.drawRect(plot)
        if not self._signals:
            painter.setPen(QtGui.QColor("#91a0b4"))
            painter.drawText(
                plot,
                QtCore.Qt.AlignmentFlag.AlignCenter,
                self.language_manager.text(
                    "waveform.empty",
                    "Run a scenario to see signals",
                ),
            )
            return

        all_x = [value for signal in self._signals for value in signal.x]
        all_y = [value for signal in self._signals for value in signal.y]
        if not all_x or not all_y:
            return
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        if max_x == min_x:
            max_x += 1.0
        if max_y == min_y:
            max_y += 1.0
        colors = ("#54c2ff", "#ffa657", "#74d99f", "#d6a4ff", "#ff7b9c")
        for index, signal in enumerate(self._signals):
            path = QtGui.QPainterPath()
            for point_index, (x_value, y_value) in enumerate(zip(signal.x, signal.y, strict=False)):
                x_pos = plot.left() + (x_value - min_x) / (max_x - min_x) * plot.width()
                y_pos = plot.bottom() - (y_value - min_y) / (max_y - min_y) * plot.height()
                if point_index == 0:
                    path.moveTo(x_pos, y_pos)
                else:
                    path.lineTo(x_pos, y_pos)
            painter.setPen(QtGui.QPen(QtGui.QColor(colors[index % len(colors)]), 1.7))
            painter.drawPath(path)
            painter.drawText(
                plot.left() + 8,
                plot.top() + 17 + index * 17,
                f"{signal.name} [{signal.unit}]",
            )


class WaveformView(QtWidgets.QWidget):
    """Small adapter that keeps pyqtgraph out of the rest of the UI."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.language_manager = language_manager or current_language_manager()
        self._signals: tuple[SignalSeries, ...] = ()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if pg is None:
            self._plot: object = _PainterWaveform(language_manager=self.language_manager)
        else:
            plot = pg.PlotWidget(background="#101620")
            plot.showGrid(x=True, y=True, alpha=0.2)
            plot.addLegend(offset=(8, 8))
            self._plot = plot
        layout.addWidget(self._plot)  # type: ignore[arg-type]
        self.retranslate_ui()

    @property
    def signals(self) -> tuple[SignalSeries, ...]:
        return self._signals

    def set_signals(self, signals: Sequence[SignalSeries]) -> None:
        self._signals = tuple(signals)
        if isinstance(self._plot, _PainterWaveform):
            self._plot.set_signals(signals)
            return
        self._plot.clear()  # type: ignore[union-attr]
        colors = ("#54c2ff", "#ffa657", "#74d99f", "#d6a4ff", "#ff7b9c")
        for index, signal in enumerate(signals):
            self._plot.plot(  # type: ignore[union-attr]
                signal.x,
                signal.y,
                name=f"{signal.name} [{signal.unit}]",
                pen=pg.mkPen(colors[index % len(colors)], width=2),
            )

    def retranslate_ui(self) -> None:
        if isinstance(self._plot, _PainterWaveform):
            self._plot.update()
            return
        self._plot.setLabel(  # type: ignore[union-attr]
            "bottom",
            self.language_manager.text("waveform.axis.time", "Simulation time"),
            units="s",
        )
        self._plot.setLabel(  # type: ignore[union-attr]
            "left",
            self.language_manager.text("waveform.axis.value", "Signal value"),
        )

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.LanguageChange:
            self.retranslate_ui()
