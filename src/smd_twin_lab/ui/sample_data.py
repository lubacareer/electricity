"""A useful no-tools-required project and deterministic teaching runner."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import uuid4

from ..models import (
    BoardGeometry,
    Capability,
    CapabilityStatus,
    Component,
    ComponentSide,
    FaultKind,
    FirmwareState,
    ImportedProject,
    Net,
    PinRef,
    ProjectCapabilities,
    RunReport,
    Scenario,
    SignalSeries,
)


def build_sample_project() -> ImportedProject:
    """Return a small sensor board so the application is useful before import setup."""

    components = (
        Component(
            "J1",
            "POWER",
            "Connector_PinHeader_1x02",
            8.0,
            25.0,
            side=ComponentSide.FRONT,
            in_bom=True,
            on_board=True,
            is_smd=False,
            nets=("+5V", "GND"),
        ),
        Component(
            "R1",
            "10k",
            "R_0603_1608Metric",
            24.0,
            33.0,
            side=ComponentSide.FRONT,
            in_bom=True,
            on_board=True,
            is_smd=True,
            nets=("+5V", "SENSOR"),
            fields={"Role": "Sensor pull-up"},
        ),
        Component(
            "R2",
            "10k NTC",
            "R_0603_1608Metric",
            24.0,
            17.0,
            rotation_deg=90.0,
            side=ComponentSide.FRONT,
            in_bom=True,
            on_board=True,
            is_smd=True,
            nets=("SENSOR", "GND"),
            fields={"Role": "Temperature sensor"},
        ),
        Component(
            "C1",
            "100n",
            "C_0603_1608Metric",
            35.0,
            17.0,
            side=ComponentSide.BACK,
            in_bom=True,
            on_board=True,
            is_smd=True,
            nets=("SENSOR", "GND"),
            fields={"Role": "ADC filter"},
        ),
        Component(
            "U1",
            "MCU",
            "QFN-32",
            49.0,
            25.0,
            rotation_deg=45.0,
            side=ComponentSide.FRONT,
            in_bom=True,
            on_board=True,
            is_smd=True,
            nets=("+5V", "GND", "SENSOR", "ALARM_LED", "UART_TX"),
            fields={"Role": "Firmware controller"},
        ),
        Component(
            "D1",
            "RED",
            "LED_0603_1608Metric",
            67.0,
            25.0,
            side=ComponentSide.FRONT,
            in_bom=True,
            on_board=True,
            is_smd=True,
            nets=("ALARM_LED", "GND"),
            fields={"Role": "Alarm indicator"},
        ),
    )
    nets = tuple(
        Net(name, tuple(PinRef(reference, pin) for reference, pin in pins))
        for name, pins in {
            "+5V": (("J1", "1"), ("R1", "1"), ("U1", "8")),
            "GND": (("J1", "2"), ("R2", "2"), ("C1", "2"), ("U1", "4"), ("D1", "2")),
            "SENSOR": (("R1", "2"), ("R2", "1"), ("C1", "1"), ("U1", "12")),
            "ALARM_LED": (("U1", "21"), ("D1", "1")),
            "UART_TX": (("U1", "18"),),
        }.items()
    )
    return ImportedProject(
        schema_version=1,
        project_id="sample-sensor-board",
        name="Sample temperature monitor",
        source_dir="",
        cache_dir="",
        source_hashes={},
        kicad_version=None,
        variant="teaching",
        components=components,
        nets=nets,
        geometry=BoardGeometry(min_x_mm=0.0, min_y_mm=0.0, max_x_mm=76.0, max_y_mm=50.0),
        capabilities=ProjectCapabilities(
            geometry=Capability(CapabilityStatus.AVAILABLE, "Interactive sample placement data"),
            circuit=Capability(
                CapabilityStatus.UNAVAILABLE,
                "Sample model active; connect ngspice for electrical simulation",
            ),
            firmware=Capability(
                CapabilityStatus.AVAILABLE,
                "Deterministic reference state machine",
            ),
            hardware=Capability(
                CapabilityStatus.UNAVAILABLE,
                "No fixture connected (hardware is optional)",
            ),
        ),
    )


def run_sample_scenario(project: ImportedProject, scenario: Scenario) -> RunReport:
    """Run a deterministic, intentionally simple sensor-board lesson."""

    count = 201
    duration_s = 0.1
    times = tuple(index * duration_s / (count - 1) for index in range(count))
    nominal = max(0.35, min(4.65, 2.5 - (scenario.temperature_c - 25.0) * 0.012))
    sensor: list[float] = []

    for time_s in times:
        ripple = 0.018 * math.sin(2.0 * math.pi * 120.0 * time_s)
        value = nominal + ripple
        fault = scenario.fault
        if fault.kind is FaultKind.COMPONENT_OPEN:
            value = 5.0 if fault.reference == "R2" else 0.0
        elif fault.kind is FaultKind.NET_SHORT:
            value = 0.0
        elif fault.kind is FaultKind.WRONG_VALUE:
            resistance_ohm = fault.value if fault.value is not None else 47_000.0
            value = 5.0 * resistance_ohm / (10_000.0 + resistance_ohm)
        elif fault.kind is FaultKind.REVERSED_POLARITY:
            value = 5.0 - nominal
        elif fault.kind is FaultKind.INTERMITTENT:
            start = fault.start_s if fault.start_s is not None else 0.035
            length = fault.duration_s if fault.duration_s is not None else 0.02
            if start <= time_s <= start + length:
                value = 0.0
        sensor.append(value)

    final_voltage = sensor[-1]
    within_range = min(sensor) >= 0.5 and max(sensor) <= 4.5
    state = FirmwareState.NORMAL if within_range else FirmwareState.SENSOR_FAULT
    alarm = state is not FirmwareState.NORMAL
    now = datetime.now(UTC).isoformat()
    uart = (
        f"ADC={final_voltage:.3f} V TEMP={scenario.temperature_c:.1f} C",
        f"STATE={state.value}",
    )
    timeline: list[dict[str, object]] = [
        {"time_s": 0.0, "kind": "state", "message": "Power-on self-test"},
        {"time_s": 0.004, "kind": "uart", "message": uart[0]},
    ]
    if scenario.fault.kind is not FaultKind.NONE:
        timeline.append(
            {
                "time_s": scenario.fault.start_s or 0.0,
                "kind": "fault",
                "message": scenario.fault.kind.value.replace("_", " ").title(),
            }
        )
    timeline.extend(
        (
            {"time_s": 0.08, "kind": "state", "message": state.value},
            {"time_s": 0.081, "kind": "uart", "message": uart[1]},
        )
    )
    passed = state is FirmwareState.NORMAL
    return RunReport(
        schema_version=1,
        run_id=f"sample-{uuid4().hex[:10]}",
        project_id=project.project_id,
        scenario_id=scenario.scenario_id,
        started_at=now,
        completed_at=datetime.now(UTC).isoformat(),
        passed=passed,
        infrastructure_error=False,
        firmware_state=state,
        outputs={"alarm_led": alarm, "sensor_voltage_v": final_voltage},
        measurements={
            "sensor_final_v": final_voltage,
            "sensor_min_v": min(sensor),
            "sensor_max_v": max(sensor),
            "temperature_c": scenario.temperature_c,
        },
        signals=(
            SignalSeries("supply", "V", times, tuple(5.0 for _ in times)),
            SignalSeries("sensor", "V", times, tuple(sensor)),
            SignalSeries("alarm", "logic", times, tuple(float(alarm) for _ in times)),
        ),
        timeline=tuple(timeline),
        explanations=(
            "The ADC input is valid when it remains between 0.5 V and 4.5 V.",
            "A real backend can replace this teaching model without changing the UI contract.",
        ),
    )
