from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from threading import Event

import pytest

from smd_twin_lab.engines import (
    OPEN_RESISTANCE_OHM,
    RESULT_PREFIX,
    JsonLinesCodec,
    JsonLinesDecoder,
    NgspiceBatchEngine,
    ProtocolError,
    ReferenceFirmwareEngine,
    ReferenceNtcCircuitEngine,
    RenodeFirmwareEngine,
    build_reference_deck,
    divider_voltage_v,
    ntc_resistance_ohm,
    parse_renode_result,
    parse_wrdata,
    run_isolated_process,
)
from smd_twin_lab.models import (
    FaultKind,
    FaultSpec,
    FirmwareRequest,
    FirmwareState,
    SimulationRequest,
)


def simulation_request(
    fault: FaultSpec | None = None,
    *,
    temperature_c: float = 25.0,
) -> SimulationRequest:
    return SimulationRequest(
        analysis="transient",
        temperature_c=temperature_c,
        fault=fault or FaultSpec(FaultKind.NONE),
        duration_s=0.1,
        sample_count=11,
    )


def adc_at_temperature(temperature_c: float) -> float:
    return divider_voltage_v(ntc_resistance_ohm(temperature_c))


def test_reference_divider_is_analytically_half_supply_at_25_c() -> None:
    result = ReferenceNtcCircuitEngine().run(simulation_request())

    assert result.success
    assert result.measurements["thermistor_resistance_ohm"] == pytest.approx(10_000.0)
    assert result.measurements["adc_voltage_v"] == pytest.approx(1.65)
    assert result.signals[0].x == pytest.approx(tuple(index / 100 for index in range(11)))
    assert result.signals[0].y == pytest.approx((1.65,) * 11)


@pytest.mark.parametrize(
    ("fault", "expected_voltage"),
    [
        (FaultSpec(FaultKind.COMPONENT_OPEN, reference="RT1"), 3.3),
        (
            FaultSpec(FaultKind.NET_SHORT, net_a="TEMP_ADC", net_b="GND"),
            0.0,
        ),
        (
            FaultSpec(FaultKind.NET_SHORT, net_a="TEMP_ADC", net_b="3V3"),
            3.3,
        ),
        (
            FaultSpec(FaultKind.WRONG_VALUE, reference="RT1", value=30_000.0),
            2.475,
        ),
    ],
)
def test_reference_circuit_fault_cases(fault: FaultSpec, expected_voltage: float) -> None:
    result = ReferenceNtcCircuitEngine().run(simulation_request(fault))

    assert result.success
    assert result.measurements["adc_voltage_v"] == pytest.approx(expected_voltage, abs=1e-5)


def test_reference_circuit_is_deterministic() -> None:
    engine = ReferenceNtcCircuitEngine()
    request = simulation_request(
        FaultSpec(
            FaultKind.INTERMITTENT,
            reference="RT1",
            start_s=0.02,
            duration_s=0.04,
        )
    )

    assert engine.run(request) == engine.run(request)


def test_generated_ngspice_decks_use_finite_fault_resistances(tmp_path: Path) -> None:
    open_deck = build_reference_deck(
        simulation_request(FaultSpec(FaultKind.COMPONENT_OPEN, reference="RT1")),
        tmp_path / "open.tsv",
    )
    short_deck = build_reference_deck(
        simulation_request(FaultSpec(FaultKind.NET_SHORT, net_a="TEMP_ADC", net_b="GND")),
        tmp_path / "short.tsv",
    )
    wrong_value_deck = build_reference_deck(
        simulation_request(FaultSpec(FaultKind.WRONG_VALUE, reference="RT1", value=47_000.0)),
        tmp_path / "wrong.tsv",
    )

    assert f".param NTC_RESISTANCE_OHM={OPEN_RESISTANCE_OHM:.12g}" in open_deck
    assert "R_FAULT ADC_SENSE 0 0.001" in short_deck
    assert ".param NTC_RESISTANCE_OHM=47000" in wrong_value_deck
    assert "R_NTC ADC_SENSE 0 {NTC_RESISTANCE_OHM}" in wrong_value_deck
    assert "linearize v(ADC_SENSE)" in open_deck


def test_ngspice_wrdata_parser_accepts_headers_and_repeated_scale() -> None:
    x_values, y_values = parse_wrdata("time time v(TEMP_ADC)\n0.0 0.0 1.5\n1.0D-3 1.0D-3 1.6\n")

    assert x_values == (0.0, 0.001)
    assert y_values == (1.5, 1.6)


def test_ngspice_adapter_reports_missing_and_malformed_output(tmp_path: Path) -> None:
    missing = NgspiceBatchEngine(executable=tmp_path / "does-not-exist.exe")
    assert not missing.available
    assert missing.run(simulation_request()).diagnostics[0].code == "ngspice.unavailable"

    no_output_script = "import sys; print('ngspice-47 fake')"
    malformed = NgspiceBatchEngine(
        command_prefix=(sys.executable, "-c", no_output_script),
        timeout_s=2.0,
    )
    result = malformed.run(simulation_request())
    assert not result.success
    assert result.diagnostics[0].code == "ngspice.missing_output"

    wrong_version_script = (
        "from pathlib import Path; "
        "Path('sensor.tsv').write_text('time v\\n0 1.65\\n0.1 1.65\\n'); "
        "print('ngspice-46 done')"
    )
    wrong_version = NgspiceBatchEngine(
        command_prefix=(sys.executable, "-c", wrong_version_script),
        timeout_s=2.0,
    ).run(simulation_request())
    assert not wrong_version.success
    assert wrong_version.diagnostics[0].code == "ngspice.unsupported_version"


def test_isolated_process_honors_timeout_and_preexisting_cancellation() -> None:
    timeout = run_isolated_process(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_s=0.05,
    )
    assert timeout.timed_out

    cancelled_event = Event()
    cancelled_event.set()
    cancelled = run_isolated_process(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_s=2.0,
        cancel_event=cancelled_event,
    )
    assert cancelled.cancelled


def test_reference_firmware_hysteresis_acknowledge_and_fail_safe() -> None:
    firmware = ReferenceFirmwareEngine()

    nominal = firmware.load_and_step(FirmwareRequest(adc_at_temperature(25.0), 3.3))
    assert nominal.state is FirmwareState.NORMAL
    assert nominal.outputs["green_led"] is True
    alarm = firmware.load_and_step(FirmwareRequest(adc_at_temperature(36.0), 3.3))
    assert alarm.state is FirmwareState.ALARM
    assert alarm.outputs["buzzer"] is True

    within_hysteresis = firmware.load_and_step(FirmwareRequest(adc_at_temperature(34.0), 3.3))
    assert within_hysteresis.state is FirmwareState.ALARM

    acknowledged = firmware.load_and_step(
        FirmwareRequest(adc_at_temperature(34.0), 3.3, acknowledge=True)
    )
    assert acknowledged.outputs["buzzer"] is False
    assert (
        firmware.load_and_step(FirmwareRequest(adc_at_temperature(34.0), 3.3)).outputs["buzzer"]
        is False
    )

    recovered = firmware.load_and_step(FirmwareRequest(adc_at_temperature(32.0), 3.3))
    assert recovered.state is FirmwareState.NORMAL

    sensor_fault = firmware.load_and_step(FirmwareRequest(0.0, 3.3, acknowledge=True))
    assert sensor_fault.state is FirmwareState.SENSOR_FAULT
    assert sensor_fault.outputs == {
        "green_led": False,
        "red_led": True,
        "buzzer": True,
        "adc_voltage_v": 0.0,
    }


def test_reference_circuit_rejects_unmodelled_faults_instead_of_passing() -> None:
    result = ReferenceNtcCircuitEngine().run(
        simulation_request(FaultSpec(FaultKind.COMPONENT_OPEN, reference="J1"))
    )

    assert not result.success
    assert result.diagnostics[0].code == "reference.unsupported_fault"


def test_renode_result_parser_and_determinism_qualification(tmp_path: Path) -> None:
    payload = {
        "state": "NORMAL",
        "outputs": {"green_led": True, "red_led": False, "buzzer": False},
        "uart_lines": ["STATE=NORMAL"],
        "events": [{"time_s": 0.1, "kind": "gpio"}],
    }
    marker = RESULT_PREFIX + json.dumps(payload, separators=(",", ":"))
    parsed = parse_renode_result("boot\n" + marker + "\n")
    assert parsed.state is FirmwareState.NORMAL

    firmware_path = tmp_path / "firmware.elf"
    integration_path = tmp_path / "board.resc"
    firmware_path.write_bytes(b"ELF placeholder")
    integration_path.write_text("# integration placeholder", encoding="utf-8")
    fake_command = f"print({marker!r})"
    engine = RenodeFirmwareEngine(
        firmware_path,
        integration_path,
        command_prefix=(sys.executable, "-c", fake_command),
        timeout_s=2.0,
    )

    qualification = engine.qualify(FirmwareRequest(1.65, 3.3))
    assert qualification.available
    assert qualification.deterministic


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"{}\n",
        b'{"protocol_version":2,"request_id":"x","type":"ping","payload":{}}\n',
        b'{"protocol_version":1,"request_id":"x","type":"ping","payload":NaN}\n',
    ],
)
def test_json_lines_codec_rejects_malformed_protocol(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        JsonLinesCodec().decode_line(line)


def test_json_lines_codec_incremental_round_trip() -> None:
    codec = JsonLinesCodec()
    message = codec.make_message("ping", {"value": 1.25}, request_id="request-1")
    encoded = codec.encode(message)
    decoder = JsonLinesDecoder(codec)

    assert decoder.feed(encoded[:4]) == ()
    assert decoder.feed(encoded[4:]) == (message,)
    decoder.finish()


def test_reference_helpers_reject_nonphysical_values() -> None:
    with pytest.raises(ValueError):
        ntc_resistance_ohm(-273.15)
    with pytest.raises(ValueError):
        divider_voltage_v(0.0)
    assert math.isfinite(ntc_resistance_ohm(25.0))
