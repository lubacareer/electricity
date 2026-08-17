"""Concrete circuit, firmware, and hardware adapters."""

from .hardware import (
    DEFAULT_MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    JsonLinesCodec,
    JsonLinesDecoder,
    LoopbackHardwareTarget,
    ProtocolError,
)
from .ngspice import NgspiceBatchEngine, build_reference_deck, parse_wrdata
from .process import ProcessResult, run_isolated_process
from .reference import (
    FIXED_RESISTANCE_OHM,
    NTC_BETA_K,
    NTC_NOMINAL_RESISTANCE_OHM,
    OPEN_RESISTANCE_OHM,
    SHORT_RESISTANCE_OHM,
    SUPPLY_VOLTAGE_V,
    ReferenceFirmwareEngine,
    ReferenceNtcCircuitEngine,
    divider_voltage_v,
    ntc_resistance_ohm,
    temperature_from_divider_c,
)
from .renode import (
    RESULT_PREFIX,
    RenodeFirmwareEngine,
    RenodeQualification,
    build_renode_runner_script,
    parse_renode_result,
)

__all__ = [
    "DEFAULT_MAX_LINE_BYTES",
    "FIXED_RESISTANCE_OHM",
    "NTC_BETA_K",
    "NTC_NOMINAL_RESISTANCE_OHM",
    "OPEN_RESISTANCE_OHM",
    "PROTOCOL_VERSION",
    "RESULT_PREFIX",
    "SHORT_RESISTANCE_OHM",
    "SUPPLY_VOLTAGE_V",
    "JsonLinesCodec",
    "JsonLinesDecoder",
    "LoopbackHardwareTarget",
    "NgspiceBatchEngine",
    "ProcessResult",
    "ProtocolError",
    "ReferenceFirmwareEngine",
    "ReferenceNtcCircuitEngine",
    "RenodeFirmwareEngine",
    "RenodeQualification",
    "build_reference_deck",
    "build_renode_runner_script",
    "divider_voltage_v",
    "ntc_resistance_ohm",
    "parse_renode_result",
    "parse_wrdata",
    "run_isolated_process",
    "temperature_from_divider_c",
]
