"""Application composition root.

Concrete services are imported lazily so importing this module does not start
Qt or any external process. Integrators can still replace the controller.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from .ui import ControllerBindings, MainWindow


ControllerFactory = Callable[[], "ControllerBindings"]


def build_window(controller_factory: ControllerFactory | None = None) -> MainWindow:
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
    return MainWindow(bindings)


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Create or reuse QApplication, keeping PySide6 an entry-point dependency."""

    from PySide6 import QtCore, QtWidgets

    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing
    QtCore.QCoreApplication.setOrganizationName("SMD Twin Lab")
    QtCore.QCoreApplication.setApplicationName("SMD Twin Lab")
    return QtWidgets.QApplication(list(argv) if argv is not None else sys.argv)


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
