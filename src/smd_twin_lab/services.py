"""Composition of concrete import, simulation, reporting, and history services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .engines import NgspiceBatchEngine, ReferenceFirmwareEngine, ReferenceNtcCircuitEngine
from .history import RunHistory
from .importers import KiCadProjectImporter, load_bundle
from .models import CapabilityStatus, ImportedProject, RunReport, Scenario
from .paths import cache_root, reports_root, sample_bundle_path
from .supervisor import QuasiStaticSupervisor
from .tooling import ToolPaths, discover_tools


@dataclass(slots=True)
class RuntimeServices:
    tools: ToolPaths
    importer: KiCadProjectImporter
    supervisor: QuasiStaticSupervisor
    history: RunHistory
    circuit_engine_name: str
    firmware_engine_name: str

    def load_project(self, path: Path) -> ImportedProject:
        requested = path.expanduser().resolve()
        if requested.is_file() and requested.suffix.casefold() in {".json", ".smdtwin"}:
            return load_bundle(requested)
        if requested.is_dir() and (requested / "project.json").is_file():
            return load_bundle(requested)
        return self.importer.import_project(requested)

    def load_sample(self) -> ImportedProject:
        return load_bundle(sample_bundle_path())

    @staticmethod
    def scenario_availability(project: ImportedProject) -> tuple[bool, str]:
        if project.project_id != "sensor-status-v1":
            return (
                False,
                "This project can be inspected, but no supported circuit/firmware plugin is "
                "configured for it.",
            )
        if project.capabilities.circuit.status is not CapabilityStatus.AVAILABLE:
            return False, "Circuit simulation is unavailable for this project."
        if project.capabilities.firmware.status is not CapabilityStatus.AVAILABLE:
            return False, "Firmware simulation is unavailable for this project."
        return True, "Run the validated sensor/status reference model."

    def run_scenario(self, project: ImportedProject, scenario: Scenario) -> RunReport:
        available, detail = self.scenario_availability(project)
        if not available:
            raise ValueError(detail)
        report = self.supervisor.run(project, scenario)
        self.history.save(report, language=scenario.language)
        return report


def build_runtime_services(*, prefer_external_spice: bool = True) -> RuntimeServices:
    tools = discover_tools()
    importer = KiCadProjectImporter(kicad_cli=tools.kicad_cli, cache_root=cache_root())

    external_spice = NgspiceBatchEngine(executable=tools.ngspice)
    if prefer_external_spice and external_spice.available:
        circuit_engine = external_spice
        circuit_engine_name = "ngspice-batch"
    else:
        circuit_engine = ReferenceNtcCircuitEngine()
        circuit_engine_name = "reference-ntc"

    # Renode remains behind its qualification gate. The exact board ELF and
    # integration script are required before it can replace this state model.
    firmware_engine = ReferenceFirmwareEngine()
    supervisor = QuasiStaticSupervisor(circuit_engine, firmware_engine)
    history = RunHistory(reports_root().parent / "runs.sqlite3")
    return RuntimeServices(
        tools=tools,
        importer=importer,
        supervisor=supervisor,
        history=history,
        circuit_engine_name=circuit_engine_name,
        firmware_engine_name="reference-stm32g071",
    )
