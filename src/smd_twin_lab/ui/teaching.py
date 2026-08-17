"""Contextual lessons embedded in the desktop application."""

from __future__ import annotations

from importlib import resources

from PySide6 import QtCore, QtWidgets

_FALLBACK_LESSONS = {
    "getting_started": "# Start here\n\nSelect a component, choose a fault, and run the scenario.",
    "aoi": (
        "# AOI connection\n\nAOI finds visible assembly defects; simulation explores "
        "their electrical effect."
    ),
    "faults": (
        "# Fault injection\n\nUse finite electrical models and compare each run with a "
        "nominal baseline."
    ),
    "simulation": (
        "# Simulation\n\nA simulator is a model, not proof that a physical board is safe "
        "or correct."
    ),
}


def load_lessons() -> dict[str, str]:
    lessons = dict(_FALLBACK_LESSONS)
    try:
        directory = resources.files("smd_twin_lab").joinpath("resources", "lessons")
        for entry in directory.iterdir():
            if entry.name.endswith(".md"):
                lessons[entry.name.removesuffix(".md")] = entry.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        pass
    return lessons


class TeachingPanel(QtWidgets.QWidget):
    """Display short lessons and switch them as the user explores the UI."""

    TOPIC_LABELS = {
        "getting_started": "Start here",
        "aoi": "AOI and the digital twin",
        "faults": "Fault injection",
        "simulation": "Reading simulation results",
    }

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._lessons = load_lessons()
        self.topic_combo = QtWidgets.QComboBox()
        for key in self.TOPIC_LABELS:
            self.topic_combo.addItem(self.TOPIC_LABELS[key], key)
        self.context_label = QtWidgets.QLabel("Guidance changes as you inspect the project.")
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color: #728195")
        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.topic_combo.currentIndexChanged.connect(self._render_current)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.topic_combo)
        layout.addWidget(self.context_label)
        layout.addWidget(self.browser, 1)
        self._render_current()

    def show_topic(self, key: str, context: str | None = None) -> None:
        index = self.topic_combo.findData(key)
        if index >= 0:
            self.topic_combo.setCurrentIndex(index)
        if context:
            self.context_label.setText(context)

    @QtCore.Slot()
    def _render_current(self) -> None:
        key = str(self.topic_combo.currentData() or "getting_started")
        self.browser.setMarkdown(self._lessons.get(key, _FALLBACK_LESSONS["getting_started"]))
