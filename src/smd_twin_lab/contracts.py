"""Stable integration contracts for tools, emulators, and future hardware."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import (
    FirmwareRequest,
    FirmwareResult,
    ImportedProject,
    RunReport,
    Scenario,
    SimulationRequest,
    SimulationResult,
)


class ProjectImporter(Protocol):
    def import_project(self, project_path: Path, variant: str = "default") -> ImportedProject: ...


class CircuitEngine(Protocol):
    @property
    def available(self) -> bool: ...

    def run(self, request: SimulationRequest) -> SimulationResult: ...


class FirmwareEngine(Protocol):
    @property
    def available(self) -> bool: ...

    def load_and_step(self, request: FirmwareRequest) -> FirmwareResult: ...


class CoSimulationSupervisor(Protocol):
    def run(self, project: ImportedProject, scenario: Scenario) -> RunReport: ...


class HardwareTarget(Protocol):
    @property
    def available(self) -> bool: ...

    def execute(self, project: ImportedProject, scenario: Scenario) -> RunReport: ...
