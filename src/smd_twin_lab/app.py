"""Application composition root.

Concrete services are imported lazily so importing this module does not start
Qt or any external process. Integrators can still replace the controller.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from .localization import LanguageManager
    from .ui import ControllerBindings, MainWindow


ControllerFactory = Callable[[], "ControllerBindings"]


def _apply_light_palette(application: QApplication) -> None:
    """Keep the deliberately light UI readable when Windows uses a dark theme."""

    from PySide6 import QtGui

    palette = QtGui.QPalette()
    colors = {
        QtGui.QPalette.ColorRole.Window: "#eef2f6",
        QtGui.QPalette.ColorRole.WindowText: "#1b2b3c",
        QtGui.QPalette.ColorRole.Base: "#ffffff",
        QtGui.QPalette.ColorRole.AlternateBase: "#f4f7fa",
        QtGui.QPalette.ColorRole.ToolTipBase: "#ffffff",
        QtGui.QPalette.ColorRole.ToolTipText: "#1b2b3c",
        QtGui.QPalette.ColorRole.Text: "#1b2b3c",
        QtGui.QPalette.ColorRole.Button: "#f7f9fb",
        QtGui.QPalette.ColorRole.ButtonText: "#1b2b3c",
        QtGui.QPalette.ColorRole.BrightText: "#ffffff",
        QtGui.QPalette.ColorRole.Link: "#075e9f",
        QtGui.QPalette.ColorRole.LinkVisited: "#5a3d91",
        QtGui.QPalette.ColorRole.Highlight: "#1769aa",
        QtGui.QPalette.ColorRole.HighlightedText: "#ffffff",
        QtGui.QPalette.ColorRole.PlaceholderText: "#596978",
    }
    for role, color in colors.items():
        palette.setColor(role, QtGui.QColor(color))
    for role in (
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QPalette.ColorRole.Text,
        QtGui.QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            role,
            QtGui.QColor("#596978"),
        )
    palette.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.Button,
        QtGui.QColor("#e2e8ee"),
    )
    application.setPalette(palette)


def build_window(
    controller_factory: ControllerFactory | None = None,
    *,
    language_manager: LanguageManager | None = None,
) -> MainWindow:
    """Construct the main window without starting the Qt event loop."""

    from .ui import ControllerBindings, MainWindow

    if controller_factory is None:
        from .services import build_runtime_services

        services = build_runtime_services()
        bindings = ControllerBindings(
            initial_project=services.load_sample(),
            project_loader=services.load_project,
            scenario_runner=services.run_scenario,
            scenario_gate=services.scenario_availability,
        )
    else:
        bindings = controller_factory()
        if not isinstance(bindings, ControllerBindings):
            raise TypeError("controller_factory must return ControllerBindings")
    return MainWindow(bindings, language_manager=language_manager)


def create_application(
    argv: Sequence[str] | None = None,
    *,
    settings: QSettings | None = None,
    system_languages: Sequence[str] | None = None,
    catalog_directory: Path | None = None,
) -> QApplication:
    """Create or reuse QApplication, keeping PySide6 an entry-point dependency."""

    from PySide6 import QtCore, QtWidgets

    QtCore.QCoreApplication.setOrganizationName("SMD Twin Lab")
    QtCore.QCoreApplication.setApplicationName("SMD Twin Lab")
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        _apply_light_palette(existing)
        from .localization import initialize_language_manager

        initialize_language_manager(
            existing,
            settings=settings,
            system_languages=system_languages,
            catalog_directory=catalog_directory,
        )
        return existing
    application = QtWidgets.QApplication(list(argv) if argv is not None else sys.argv)
    _apply_light_palette(application)
    from .localization import initialize_language_manager

    initialize_language_manager(
        application,
        settings=settings,
        system_languages=system_languages,
        catalog_directory=catalog_directory,
    )
    return application


def main(
    argv: Sequence[str] | None = None,
    controller_factory: ControllerFactory | None = None,
) -> int:
    """Launch the desktop UI."""

    application = create_application(argv)
    window = build_window(controller_factory)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
