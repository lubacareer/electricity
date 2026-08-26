"""Contextual lessons embedded in the desktop application."""

from __future__ import annotations

from importlib import resources

from PySide6 import QtCore, QtWidgets

from ..localization import LanguageManager, current_language_manager
from ..models import MessageRef

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


def load_lessons(language: str = "en") -> dict[str, str]:
    lessons = dict(_FALLBACK_LESSONS)
    try:
        directory = resources.files("smd_twin_lab").joinpath("resources", "lessons")
        for entry in directory.joinpath("en").iterdir():
            if entry.name.endswith(".md"):
                lessons[entry.name.removesuffix(".md")] = entry.read_text(encoding="utf-8")
        if language != "en":
            for entry in directory.joinpath(language).iterdir():
                if entry.name.endswith(".md"):
                    lessons[entry.name.removesuffix(".md")] = entry.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        pass
    return lessons


class TeachingPanel(QtWidgets.QWidget):
    """Display short lessons and switch them as the user explores the UI."""

    TOPICS = {
        "getting_started": ("teaching.topic.getting_started", "Start here"),
        "aoi": ("teaching.topic.aoi", "AOI and the digital twin"),
        "faults": ("teaching.topic.faults", "Fault injection"),
        "simulation": ("teaching.topic.simulation", "Reading simulation results"),
    }

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        language_manager: LanguageManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.language_manager = language_manager or current_language_manager()
        self._lessons = load_lessons(self.language_manager.current_language)
        self._context_ref: MessageRef | None = None
        self._context_fallback = "Guidance changes as you inspect the project."
        self.topic_combo = QtWidgets.QComboBox()
        for key in self.TOPICS:
            self.topic_combo.addItem("", key)
        self.context_label = QtWidgets.QLabel()
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color: #526273")
        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.topic_combo.currentIndexChanged.connect(self._render_current)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.topic_combo)
        layout.addWidget(self.context_label)
        layout.addWidget(self.browser, 1)
        self.retranslate_ui()

    def show_topic(
        self,
        key: str,
        context: str | None = None,
        *,
        context_ref: MessageRef | None = None,
    ) -> None:
        index = self.topic_combo.findData(key)
        if index >= 0:
            self.topic_combo.setCurrentIndex(index)
        if context:
            self._context_fallback = context
            self._context_ref = context_ref
            self._render_context()

    def retranslate_ui(self) -> None:
        current_key = str(self.topic_combo.currentData() or "getting_started")
        blocker = QtCore.QSignalBlocker(self.topic_combo)
        for index, (key, (message_id, fallback)) in enumerate(self.TOPICS.items()):
            self.topic_combo.setItemText(index, self.language_manager.text(message_id, fallback))
            self.topic_combo.setItemData(index, key)
        del blocker
        selected = self.topic_combo.findData(current_key)
        if selected >= 0:
            self.topic_combo.setCurrentIndex(selected)
        self._lessons = load_lessons(self.language_manager.current_language)
        self._render_context()
        self._render_current()

    def _render_context(self) -> None:
        if self._context_ref is None:
            if self._context_fallback == "Guidance changes as you inspect the project.":
                text = self.language_manager.text(
                    "teaching.context.default",
                    self._context_fallback,
                )
            else:
                text = self._context_fallback
        else:
            text = self.language_manager.render(self._context_ref, self._context_fallback)
        self.context_label.setText(text)

    @QtCore.Slot()
    def _render_current(self) -> None:
        key = str(self.topic_combo.currentData() or "getting_started")
        self.browser.setMarkdown(self._lessons.get(key, _FALLBACK_LESSONS["getting_started"]))

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.LanguageChange:
            self.retranslate_ui()
