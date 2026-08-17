"""Quasi-static circuit/firmware orchestration."""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from .contracts import CircuitEngine, FirmwareEngine
from .models import (
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    FirmwareRequest,
    FirmwareState,
    ImportedProject,
    RunReport,
    Scenario,
    SimulationRequest,
)


def _iso_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _expected_state(measurements: dict[str, float]) -> FirmwareState:
    supply = measurements.get("supply_voltage_v", 3.3)
    adc = measurements.get("adc_voltage_v", measurements.get("sensor_voltage_v", math.nan))
    if (
        not math.isfinite(supply)
        or supply <= 0
        or not math.isfinite(adc)
        or adc <= supply * 0.02
        or adc >= supply * 0.98
    ):
        return FirmwareState.SENSOR_FAULT
    inferred_temperature = measurements.get("inferred_temperature_c")
    if inferred_temperature is not None and inferred_temperature >= 35.0:
        return FirmwareState.ALARM
    return FirmwareState.NORMAL


def _expected_outputs(state: FirmwareState, acknowledge: bool) -> dict[str, bool]:
    if state is FirmwareState.NORMAL:
        return {"green_led": True, "red_led": False, "buzzer": False}
    if state is FirmwareState.ALARM:
        return {"green_led": False, "red_led": True, "buzzer": not acknowledge}
    return {"green_led": False, "red_led": True, "buzzer": True}


def _explanations(
    scenario: Scenario,
    state: FirmwareState,
    measurements: dict[str, float],
) -> tuple[str, ...]:
    lines = [
        (
            "A 10 kOhm resistor is above the ADC node and the 10 kOhm NTC is below it. "
            "At 25 C their equal resistance produces about half of the 3.3 V supply."
        )
    ]
    fault = scenario.fault
    if fault.kind is FaultKind.COMPONENT_OPEN:
        reference = fault.reference or "RT1"
        lines.append(
            f"{reference} is open. A finite 1 TOhm model replaces the broken path, so ADC_SENSE "
            "is pulled close to 3.3 V instead of hiding the numerical topology from SPICE."
        )
    elif fault.kind is FaultKind.NET_SHORT:
        lines.append(
            f"A finite 1 mOhm bridge connects {fault.net_a or 'one net'} to "
            f"{fault.net_b or 'the other net'}, forcing the ADC node toward a rail."
        )
    elif fault.kind is FaultKind.WRONG_VALUE:
        lines.append(
            f"The selected part is substituted with {fault.value:g} Ohm; the KiCad source remains "
            "unchanged."
            if fault.value is not None
            else "The requested wrong-value fault did not specify a resistance."
        )
    elif fault.kind is FaultKind.INTERMITTENT:
        lines.append(
            "A controlled switch opens only during the selected interval, exposing a fault that "
            "a single static measurement could miss."
        )

    adc = measurements.get("adc_voltage_v", measurements.get("sensor_voltage_v"))
    if state is FirmwareState.SENSOR_FAULT:
        lines.append(
            f"The ADC reading{f' ({adc:.6g} V)' if adc is not None else ''} is outside the "
            "valid sensor range. Firmware enters SENSOR_FAULT and keeps the red LED and buzzer on; "
            "acknowledge cannot silence this fail-safe state."
        )
    elif state is FirmwareState.ALARM:
        lines.append(
            "The inferred temperature reached the 35 C alarm threshold. The alarm clears only "
            "below 33 C, preventing chatter near the threshold."
        )
    else:
        lines.append(
            "The sensor is valid and below the 35 C alarm threshold, so the green status LED is on."
        )
    lines.append(
        "This simulation is an educational diagnostic result, not proof of solder quality, safety, "
        "or product compliance."
    )
    return tuple(lines)


class QuasiStaticSupervisor:
    """Solve the sensor circuit, then advance firmware with the sampled ADC value."""

    def __init__(
        self,
        circuit_engine: CircuitEngine,
        firmware_engine: FirmwareEngine,
        *,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        duration_s: float = 0.1,
        sample_count: int = 101,
    ) -> None:
        self.circuit_engine = circuit_engine
        self.firmware_engine = firmware_engine
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_id_factory = run_id_factory or (lambda: str(uuid.uuid4()))
        self.duration_s = duration_s
        self.sample_count = sample_count
        self._run_lock = threading.Lock()

    def run(self, project: ImportedProject, scenario: Scenario) -> RunReport:
        # Reference firmware carries hysteresis state, while each scenario is a
        # fresh functional test. Serializing and resetting preserves repeatable
        # runs and mirrors Renode's fresh process per request.
        with self._run_lock:
            return self._run_locked(project, scenario)

    def _run_locked(self, project: ImportedProject, scenario: Scenario) -> RunReport:
        started_at = _iso_timestamp(self.clock)
        run_id = self.run_id_factory()
        reset = getattr(self.firmware_engine, "reset", None)
        if callable(reset):
            reset()

        circuit = self.circuit_engine.run(
            SimulationRequest(
                analysis="transient",
                temperature_c=scenario.temperature_c,
                fault=scenario.fault,
                duration_s=self.duration_s,
                sample_count=self.sample_count,
                netlist_path=project.spice_netlist_path,
            )
        )
        timeline: list[dict[str, object]] = [
            {
                "time_s": 0.0,
                "kind": "circuit_complete",
                "engine": circuit.engine,
                "success": circuit.success,
            }
        ]
        diagnostics = list(circuit.diagnostics)

        adc = circuit.measurements.get(
            "adc_voltage_v", circuit.measurements.get("sensor_voltage_v")
        )
        supply = circuit.measurements.get("supply_voltage_v", 3.3)
        if not circuit.success or adc is None:
            if circuit.success and adc is None:
                diagnostics.append(
                    Diagnostic(
                        severity=DiagnosticSeverity.ERROR,
                        code="supervisor.missing_adc",
                        message="Circuit engine succeeded without an ADC voltage measurement.",
                    )
                )
            completed_at = _iso_timestamp(self.clock)
            return RunReport(
                schema_version=1,
                run_id=run_id,
                project_id=project.project_id,
                scenario_id=scenario.scenario_id,
                started_at=started_at,
                completed_at=completed_at,
                passed=False,
                infrastructure_error=True,
                firmware_state=FirmwareState.SENSOR_FAULT,
                outputs={"green_led": False, "red_led": True, "buzzer": True},
                measurements=circuit.measurements,
                signals=circuit.signals,
                timeline=tuple(timeline),
                explanations=("The circuit simulation failed before firmware could be evaluated.",),
                diagnostics=tuple(diagnostics),
            )

        firmware = self.firmware_engine.load_and_step(
            FirmwareRequest(
                adc_voltage_v=adc,
                supply_voltage_v=supply,
                acknowledge=scenario.acknowledge,
                duration_s=self.duration_s,
            )
        )
        diagnostics.extend(firmware.diagnostics)
        timeline.extend(dict(event) for event in firmware.events)
        timeline.extend(
            {
                "time_s": self.duration_s,
                "kind": "uart",
                "line": line,
            }
            for line in firmware.uart_lines
        )

        expected_state = _expected_state(circuit.measurements)
        expected_outputs = _expected_outputs(expected_state, scenario.acknowledge)
        output_match = all(
            firmware.outputs.get(name) is value for name, value in expected_outputs.items()
        )
        passed = firmware.success and firmware.state is expected_state and output_match
        timeline.append(
            {
                "time_s": self.duration_s,
                "kind": "assertion",
                "expected_state": expected_state.value,
                "observed_state": firmware.state.value,
                "passed": passed,
            }
        )
        if firmware.success and not passed:
            diagnostics.append(
                Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    code="supervisor.functional_mismatch",
                    message=(
                        f"Expected {expected_state.value} with {expected_outputs}, observed "
                        f"{firmware.state.value} with {firmware.outputs}."
                    ),
                )
            )

        completed_at = _iso_timestamp(self.clock)
        return RunReport(
            schema_version=1,
            run_id=run_id,
            project_id=project.project_id,
            scenario_id=scenario.scenario_id,
            started_at=started_at,
            completed_at=completed_at,
            passed=passed,
            infrastructure_error=not firmware.success,
            firmware_state=firmware.state,
            outputs=firmware.outputs,
            measurements=circuit.measurements,
            signals=circuit.signals,
            timeline=tuple(timeline),
            explanations=_explanations(scenario, firmware.state, circuit.measurements),
            diagnostics=tuple(diagnostics),
        )
