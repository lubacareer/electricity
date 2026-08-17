"""EDA-independent domain models shared by importers, engines, and UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ComponentSide(StrEnum):
    FRONT = "front"
    BACK = "back"
    UNKNOWN = "unknown"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FaultKind(StrEnum):
    NONE = "none"
    COMPONENT_OPEN = "component_open"
    NET_SHORT = "net_short"
    WRONG_VALUE = "wrong_value"
    REVERSED_POLARITY = "reversed_polarity"
    INTERMITTENT = "intermittent"


class FirmwareState(StrEnum):
    NORMAL = "NORMAL"
    ALARM = "ALARM"
    SENSOR_FAULT = "SENSOR_FAULT"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str
    reference: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class Capability:
    status: CapabilityStatus
    detail: str


@dataclass(frozen=True, slots=True)
class PinRef:
    reference: str
    pin: str


@dataclass(frozen=True, slots=True)
class Net:
    name: str
    pins: tuple[PinRef, ...] = ()


@dataclass(frozen=True, slots=True)
class Component:
    reference: str
    value: str = ""
    footprint: str = ""
    x_mm: float | None = None
    y_mm: float | None = None
    rotation_deg: float = 0.0
    side: ComponentSide = ComponentSide.UNKNOWN
    in_bom: bool = False
    on_board: bool = False
    is_smd: bool = False
    nets: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoardGeometry:
    outline_path: str | None = None
    top_preview_path: str | None = None
    bottom_preview_path: str | None = None
    min_x_mm: float = 0.0
    min_y_mm: float = 0.0
    max_x_mm: float = 100.0
    max_y_mm: float = 60.0


@dataclass(frozen=True, slots=True)
class ProjectCapabilities:
    geometry: Capability
    circuit: Capability
    firmware: Capability
    hardware: Capability


@dataclass(frozen=True, slots=True)
class ImportedProject:
    schema_version: int
    project_id: str
    name: str
    source_dir: str
    cache_dir: str
    source_hashes: dict[str, str]
    kicad_version: str | None
    variant: str
    components: tuple[Component, ...]
    nets: tuple[Net, ...]
    geometry: BoardGeometry
    capabilities: ProjectCapabilities
    diagnostics: tuple[Diagnostic, ...] = ()
    spice_netlist_path: str | None = None
    twin_manifest_path: str | None = None


@dataclass(frozen=True, slots=True)
class FaultSpec:
    kind: FaultKind
    reference: str | None = None
    net_a: str | None = None
    net_b: str | None = None
    value: float | None = None
    start_s: float | None = None
    duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class SignalSeries:
    name: str
    unit: str
    x: tuple[float, ...]
    y: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SimulationRequest:
    analysis: str
    temperature_c: float
    fault: FaultSpec
    duration_s: float = 0.1
    sample_count: int = 101
    netlist_path: str | None = None


@dataclass(frozen=True, slots=True)
class SimulationResult:
    success: bool
    engine: str
    engine_version: str
    measurements: dict[str, float]
    signals: tuple[SignalSeries, ...]
    stdout: str = ""
    stderr: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class FirmwareRequest:
    adc_voltage_v: float
    supply_voltage_v: float
    acknowledge: bool = False
    duration_s: float = 0.1


@dataclass(frozen=True, slots=True)
class FirmwareResult:
    success: bool
    engine: str
    state: FirmwareState
    outputs: dict[str, bool | float]
    uart_lines: tuple[str, ...]
    events: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    name: str
    temperature_c: float
    fault: FaultSpec
    acknowledge: bool = False


@dataclass(frozen=True, slots=True)
class RunReport:
    schema_version: int
    run_id: str
    project_id: str
    scenario_id: str
    started_at: str
    completed_at: str
    passed: bool
    infrastructure_error: bool
    firmware_state: FirmwareState
    outputs: dict[str, bool | float]
    measurements: dict[str, float]
    signals: tuple[SignalSeries, ...]
    timeline: tuple[dict[str, Any], ...]
    explanations: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
