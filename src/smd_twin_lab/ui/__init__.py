"""PySide6 presentation layer for SMD Twin Lab.

The UI depends only on the stable domain models. Tool discovery, project
import, and simulation engines are injected through callbacks at runtime.
"""

from .main_window import ControllerBindings, MainWindow
from .sample_data import build_sample_project, run_sample_scenario

__all__ = [
    "ControllerBindings",
    "MainWindow",
    "build_sample_project",
    "run_sample_scenario",
]
