from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from smd_twin_lab.engines import (
    JsonLinesCodec,
    LoopbackHardwareTarget,
    ReferenceFirmwareEngine,
    ReferenceNtcCircuitEngine,
)
from smd_twin_lab.models import (
    BoardGeometry,
    Capability,
    CapabilityStatus,
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    FaultSpec,
    FirmwareState,
    ImportedProject,
    ProjectCapabilities,
    Scenario,
    SimulationResult,
)
from smd_twin_lab.supervisor import QuasiStaticSupervisor


def imported_project(tmp_path: Path) -> ImportedProject:
    available = Capability(CapabilityStatus.AVAILABLE, "test")
    return ImportedProject(
        schema_version=1,
        project_id="reference-board",
        name="Reference board",
        source_dir=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
        source_hashes={},
        kicad_version=None,
        variant="default",
        components=(),
        nets=(),
        geometry=BoardGeometry(),
        capabilities=ProjectCapabilities(
            geometry=available,
            circuit=available,
            firmware=available,
            hardware=available,
        ),
    )


def supervisor() -> QuasiStaticSupervisor:
    fixed_time = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    return QuasiStaticSupervisor(
        ReferenceNtcCircuitEngine(),
        ReferenceFirmwareEngine(),
        clock=lambda: fixed_time,
        run_id_factory=lambda: "deterministic-run",
    )


def test_nominal_25_c_end_to_end_report(tmp_path: Path) -> None:
    report = supervisor().run(
        imported_project(tmp_path),
        Scenario("nominal-25c", "Nominal 25 C", 25.0, FaultSpec(FaultKind.NONE)),
    )

    assert report.passed
    assert not report.infrastructure_error
    assert report.firmware_state is FirmwareState.NORMAL
    assert report.outputs["green_led"] is True
    assert report.outputs["red_led"] is False
    assert report.outputs["buzzer"] is False
    assert report.measurements["adc_voltage_v"] == 1.65
    assert any(item["kind"] == "assertion" and item["passed"] for item in report.timeline)


def test_thermistor_open_enters_fail_safe_and_explains_exact_fault(tmp_path: Path) -> None:
    report = supervisor().run(
        imported_project(tmp_path),
        Scenario(
            "thermistor-open",
            "Open thermistor",
            25.0,
            FaultSpec(FaultKind.COMPONENT_OPEN, reference="RT1"),
            acknowledge=True,
        ),
    )

    assert report.passed
    assert report.firmware_state is FirmwareState.SENSOR_FAULT
    assert report.outputs["green_led"] is False
    assert report.outputs["red_led"] is True
    assert report.outputs["buzzer"] is True
    assert report.measurements["adc_voltage_v"] > 3.299
    explanation = " ".join(report.explanations)
    assert "RT1 is open" in explanation
    assert "ADC_SENSE" in explanation
    assert "SENSOR_FAULT" in explanation
    assert len(report.explanation_refs) == len(report.explanations)
    open_ref = next(
        item
        for item in report.explanation_refs
        if item.message_id == "explanation.supervisor.component_open"
    )
    assert open_ref.parameters["reference"] == "RT1"


def test_supervisor_runs_are_reproducible_with_injected_identity_and_clock(
    tmp_path: Path,
) -> None:
    orchestrator = supervisor()
    project = imported_project(tmp_path)
    scenario = Scenario(
        "alarm",
        "Hot board",
        40.0,
        FaultSpec(FaultKind.NONE),
        acknowledge=True,
    )

    first = orchestrator.run(project, scenario)
    second = orchestrator.run(project, scenario)

    assert first == second
    assert first.firmware_state is FirmwareState.ALARM
    assert first.outputs["buzzer"] is False


def test_loopback_hardware_uses_versioned_protocol_and_same_scenario_contract(
    tmp_path: Path,
) -> None:
    project = imported_project(tmp_path)
    scenario = Scenario("nominal", "Nominal", 25.0, FaultSpec(FaultKind.NONE))
    target = LoopbackHardwareTarget(supervisor(), codec=JsonLinesCodec())

    report = target.execute(project, scenario)

    assert target.available
    assert report.passed
    assert report.project_id == project.project_id
    assert report.scenario_id == scenario.scenario_id


class FailingCircuitEngine:
    @property
    def available(self) -> bool:
        return True

    def run(self, request: object) -> SimulationResult:
        return SimulationResult(
            success=False,
            engine="failed-test-engine",
            engine_version="1",
            measurements={},
            signals=(),
            diagnostics=(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "test.failure",
                    "intentional failure",
                ),
            ),
        )


def test_circuit_failure_is_reported_as_infrastructure_error(tmp_path: Path) -> None:
    orchestrator = QuasiStaticSupervisor(FailingCircuitEngine(), ReferenceFirmwareEngine())
    report = orchestrator.run(
        imported_project(tmp_path),
        Scenario("failed", "Failed", 25.0, FaultSpec(FaultKind.NONE)),
    )

    assert not report.passed
    assert report.infrastructure_error
    assert report.firmware_state is FirmwareState.SENSOR_FAULT
    assert report.diagnostics[0].code == "test.failure"
    assert report.explanation_refs[0].message_id == (
        "explanation.supervisor.circuit_failed_before_firmware"
    )


def test_report_can_be_saved_as_json(tmp_path: Path) -> None:
    report = supervisor().run(
        imported_project(tmp_path),
        Scenario("nominal", "Nominal", 25.0, FaultSpec(FaultKind.NONE)),
    )
    output = tmp_path / "reports" / "run.json"

    report.write_json(output)

    text = output.read_text(encoding="utf-8")
    assert '"firmware_state": "NORMAL"' in text
    assert '"scenario_id": "nominal"' in text
