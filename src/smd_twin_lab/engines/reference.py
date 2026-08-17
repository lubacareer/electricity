"""Deterministic electrical and firmware models for the reference board."""

from __future__ import annotations

import math

from ..models import (
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    FirmwareRequest,
    FirmwareResult,
    FirmwareState,
    SignalSeries,
    SimulationRequest,
    SimulationResult,
)

SUPPLY_VOLTAGE_V = 3.3
FIXED_RESISTANCE_OHM = 10_000.0
NTC_NOMINAL_RESISTANCE_OHM = 10_000.0
NTC_NOMINAL_TEMPERATURE_K = 25.0 + 273.15
NTC_BETA_K = 3950.0
OPEN_RESISTANCE_OHM = 1.0e12
SHORT_RESISTANCE_OHM = 1.0e-3

_THERMISTOR_REFERENCES = {"rt1", "ntc1", "th1", "r_ntc", "thermistor"}
_SENSOR_NETS = {
    "adc",
    "adc_sense",
    "adc_temp",
    "ntc_sense",
    "sensor",
    "temp_adc",
    "temp_sense",
}
_GROUND_NETS = {"0", "agnd", "gnd"}
_SUPPLY_NETS = {"+3v3", "3v3", "supply", "vcc", "vdd"}


def ntc_resistance_ohm(temperature_c: float) -> float:
    """Return a 10 kOhm, beta-3950 NTC resistance at ``temperature_c``."""

    temperature_k = temperature_c + 273.15
    if not math.isfinite(temperature_k) or temperature_k <= 0:
        raise ValueError("temperature must be above absolute zero")
    exponent = NTC_BETA_K * ((1.0 / temperature_k) - (1.0 / NTC_NOMINAL_TEMPERATURE_K))
    return NTC_NOMINAL_RESISTANCE_OHM * math.exp(exponent)


def divider_voltage_v(
    thermistor_resistance_ohm: float,
    *,
    supply_voltage_v: float = SUPPLY_VOLTAGE_V,
    fixed_resistance_ohm: float = FIXED_RESISTANCE_OHM,
) -> float:
    """Voltage for a fixed-resistor-to-VDD and NTC-to-ground divider."""

    if thermistor_resistance_ohm <= 0 or fixed_resistance_ohm <= 0:
        raise ValueError("divider resistances must be positive")
    return (
        supply_voltage_v
        * thermistor_resistance_ohm
        / (thermistor_resistance_ohm + fixed_resistance_ohm)
    )


def temperature_from_divider_c(
    adc_voltage_v: float,
    *,
    supply_voltage_v: float = SUPPLY_VOLTAGE_V,
    fixed_resistance_ohm: float = FIXED_RESISTANCE_OHM,
) -> float:
    """Invert the reference divider and beta equation."""

    if not 0 < adc_voltage_v < supply_voltage_v:
        raise ValueError("ADC voltage must be strictly inside the supply rails")
    resistance = fixed_resistance_ohm * adc_voltage_v / (supply_voltage_v - adc_voltage_v)
    reciprocal_k = (1.0 / NTC_NOMINAL_TEMPERATURE_K) + (
        math.log(resistance / NTC_NOMINAL_RESISTANCE_OHM) / NTC_BETA_K
    )
    return (1.0 / reciprocal_k) - 273.15


def _diagnostic(
    severity: DiagnosticSeverity,
    code: str,
    message: str,
    *,
    reference: str | None = None,
) -> Diagnostic:
    return Diagnostic(severity=severity, code=code, message=message, reference=reference)


def _failure(message: str, code: str = "reference.invalid_request") -> SimulationResult:
    return SimulationResult(
        success=False,
        engine="reference-ntc",
        engine_version="1",
        measurements={},
        signals=(),
        diagnostics=(_diagnostic(DiagnosticSeverity.ERROR, code, message),),
    )


def _fault_targets_thermistor(reference: str | None) -> bool:
    return reference is None or reference.strip().casefold() in _THERMISTOR_REFERENCES


def _shorted_sensor_voltage(
    thermistor_resistance: float,
    net_a: str | None,
    net_b: str | None,
) -> float | None:
    a = (net_a or "").strip().casefold()
    b = (net_b or "").strip().casefold()
    if a in _SENSOR_NETS:
        other = b
    elif b in _SENSOR_NETS:
        other = a
    else:
        return None

    if other in _GROUND_NETS:
        rail_voltage = 0.0
    elif other in _SUPPLY_NETS:
        rail_voltage = SUPPLY_VOLTAGE_V
    else:
        return None

    conductance_sum = (
        1.0 / thermistor_resistance + 1.0 / FIXED_RESISTANCE_OHM + 1.0 / SHORT_RESISTANCE_OHM
    )
    current_sum = SUPPLY_VOLTAGE_V / FIXED_RESISTANCE_OHM + rail_voltage / SHORT_RESISTANCE_OHM
    return current_sum / conductance_sum


class ReferenceNtcCircuitEngine:
    """Analytical reference circuit, available without native dependencies."""

    @property
    def available(self) -> bool:
        return True

    def run(self, request: SimulationRequest) -> SimulationResult:
        if request.duration_s <= 0:
            return _failure("duration_s must be positive")
        if request.sample_count < 2:
            return _failure("sample_count must be at least 2")
        if not math.isfinite(request.temperature_c):
            return _failure("temperature_c must be finite")

        try:
            nominal_resistance = ntc_resistance_ohm(request.temperature_c)
        except (ValueError, OverflowError) as exc:
            return _failure(str(exc))

        diagnostics: list[Diagnostic] = []
        fault = request.fault
        effective_resistance = nominal_resistance
        if fault.kind is FaultKind.COMPONENT_OPEN:
            if _fault_targets_thermistor(fault.reference):
                effective_resistance = OPEN_RESISTANCE_OHM
            else:
                return _failure(
                    "This release can inject component-open faults only into RT1.",
                    "reference.unsupported_fault",
                )
        elif fault.kind is FaultKind.WRONG_VALUE:
            if not _fault_targets_thermistor(fault.reference):
                return _failure(
                    "This release can substitute resistance values only for RT1.",
                    "reference.unsupported_fault",
                )
            elif fault.value is None or not math.isfinite(fault.value) or fault.value <= 0:
                return _failure(
                    "A wrong-value thermistor fault requires a positive resistance in ohms.",
                    "reference.invalid_fault_value",
                )
            else:
                effective_resistance = fault.value
        elif fault.kind is FaultKind.REVERSED_POLARITY:
            if not _fault_targets_thermistor(fault.reference):
                return _failure(
                    "This release models reversed polarity only for the non-polar RT1 sensor.",
                    "reference.unsupported_fault",
                )
            diagnostics.append(
                _diagnostic(
                    DiagnosticSeverity.INFO,
                    "reference.nonpolar_thermistor",
                    "Reversing this two-terminal NTC does not change its electrical behavior.",
                    reference=fault.reference,
                )
            )

        normal_voltage = divider_voltage_v(effective_resistance)
        short_voltage: float | None = None
        if fault.kind is FaultKind.NET_SHORT:
            short_voltage = _shorted_sensor_voltage(
                effective_resistance,
                fault.net_a,
                fault.net_b,
            )
            if short_voltage is None:
                return _failure(
                    "Only ADC_SENSE-to-3V3 and ADC_SENSE-to-GND shorts are supported.",
                    "reference.unsupported_fault",
                )

        x_values = tuple(
            request.duration_s * index / (request.sample_count - 1)
            for index in range(request.sample_count)
        )

        if fault.kind is FaultKind.INTERMITTENT and not _fault_targets_thermistor(fault.reference):
            return _failure(
                "This release can inject intermittent opens only into RT1.",
                "reference.unsupported_fault",
            )

        if fault.kind is FaultKind.INTERMITTENT:
            start_s = fault.start_s if fault.start_s is not None else request.duration_s * 0.25
            fault_duration_s = (
                fault.duration_s if fault.duration_s is not None else request.duration_s * 0.5
            )
            if start_s < 0 or fault_duration_s <= 0:
                return _failure(
                    "An intermittent fault requires a non-negative start and positive duration.",
                    "reference.invalid_intermittent_fault",
                )
            open_voltage = divider_voltage_v(OPEN_RESISTANCE_OHM)
            y_values = tuple(
                open_voltage if start_s <= time_s < start_s + fault_duration_s else normal_voltage
                for time_s in x_values
            )
        else:
            observed_voltage = short_voltage if short_voltage is not None else normal_voltage
            y_values = (observed_voltage,) * request.sample_count

        observed_voltage = y_values[-1]
        inferred_temperature: float | None
        try:
            inferred_temperature = temperature_from_divider_c(observed_voltage)
        except ValueError:
            inferred_temperature = None

        measurements = {
            "supply_voltage_v": SUPPLY_VOLTAGE_V,
            "adc_voltage_v": observed_voltage,
            "sensor_voltage_v": observed_voltage,
            "sensor_voltage_min_v": min(y_values),
            "sensor_voltage_max_v": max(y_values),
            "thermistor_resistance_ohm": effective_resistance,
            "ambient_temperature_c": request.temperature_c,
        }
        if inferred_temperature is not None:
            measurements["inferred_temperature_c"] = inferred_temperature

        return SimulationResult(
            success=True,
            engine="reference-ntc",
            engine_version="1",
            measurements=measurements,
            signals=(SignalSeries("ADC_SENSE", "V", x_values, y_values),),
            diagnostics=tuple(diagnostics),
        )


class ReferenceFirmwareEngine:
    """Deterministic model of the STM32 reference firmware state machine."""

    alarm_threshold_c = 35.0
    recovery_threshold_c = 33.0
    sensor_valid_min_ratio = 0.02
    sensor_valid_max_ratio = 0.98

    def __init__(self) -> None:
        self._state = FirmwareState.NORMAL
        self._alarm_acknowledged = False

    @property
    def available(self) -> bool:
        return True

    @property
    def state(self) -> FirmwareState:
        return self._state

    def reset(self) -> None:
        self._state = FirmwareState.NORMAL
        self._alarm_acknowledged = False

    def load_and_step(self, request: FirmwareRequest) -> FirmwareResult:
        supply = request.supply_voltage_v
        adc = request.adc_voltage_v
        if not math.isfinite(supply) or supply <= 0 or not math.isfinite(adc):
            return FirmwareResult(
                success=False,
                engine="reference-stm32g071",
                state=FirmwareState.SENSOR_FAULT,
                outputs={"green_led": False, "red_led": True, "buzzer": True},
                uart_lines=(),
                diagnostics=(
                    _diagnostic(
                        DiagnosticSeverity.ERROR,
                        "firmware.invalid_input",
                        "Supply and ADC voltages must be finite, with a positive supply.",
                    ),
                ),
            )

        lower_limit = supply * self.sensor_valid_min_ratio
        upper_limit = supply * self.sensor_valid_max_ratio
        sensor_valid = lower_limit < adc < upper_limit
        estimated_temperature: float | None = None
        previous_state = self._state

        if not sensor_valid:
            self._state = FirmwareState.SENSOR_FAULT
            self._alarm_acknowledged = False
        else:
            try:
                estimated_temperature = temperature_from_divider_c(
                    adc,
                    supply_voltage_v=supply,
                )
            except ValueError:
                self._state = FirmwareState.SENSOR_FAULT
                self._alarm_acknowledged = False
            else:
                if previous_state is FirmwareState.ALARM:
                    alarm_active = estimated_temperature >= self.recovery_threshold_c
                else:
                    alarm_active = estimated_temperature >= self.alarm_threshold_c

                if alarm_active:
                    self._state = FirmwareState.ALARM
                    if request.acknowledge:
                        self._alarm_acknowledged = True
                else:
                    self._state = FirmwareState.NORMAL
                    self._alarm_acknowledged = False

        if self._state is FirmwareState.NORMAL:
            outputs: dict[str, bool | float] = {
                "green_led": True,
                "red_led": False,
                "buzzer": False,
            }
        elif self._state is FirmwareState.ALARM:
            outputs = {
                "green_led": False,
                "red_led": True,
                "buzzer": not self._alarm_acknowledged,
            }
        else:
            # Acknowledge deliberately cannot silence a fail-safe sensor fault.
            outputs = {"green_led": False, "red_led": True, "buzzer": True}

        if estimated_temperature is not None:
            outputs["estimated_temperature_c"] = estimated_temperature
        outputs["adc_voltage_v"] = adc

        uart_line = f"STATE={self._state.value} ADC={adc:.6f}V" + (
            f" TEMP={estimated_temperature:.3f}C"
            if estimated_temperature is not None
            else " SENSOR=INVALID"
        )
        event = {
            "time_s": request.duration_s,
            "kind": "firmware_state",
            "state": self._state.value,
            "previous_state": previous_state.value,
        }
        return FirmwareResult(
            success=True,
            engine="reference-stm32g071",
            state=self._state,
            outputs=outputs,
            uart_lines=(uart_line,),
            events=(event,),
        )
