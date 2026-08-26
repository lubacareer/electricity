from __future__ import annotations

import sys
from threading import Event, Thread

import pytest

from smd_twin_lab.eda.ngspice import (
    NgspiceCircuitEngine,
    build_dc_deck,
    detect_ngspice_version,
    parse_operating_point,
)
from smd_twin_lab.eda.simulation import (
    CircuitCompiler,
    CircuitFault,
    CircuitFaultKind,
    CompiledCircuit,
    DcMnaSolver,
    ResistorElement,
    VoltageSourceElement,
)
from smd_twin_lab.eda.templates import divider_project
from smd_twin_lab.tooling import discover_tools

_OP_BEGIN = "__SMD_TWIN_OP_BEGIN__"
_OP_END = "__SMD_TWIN_OP_END__"


def _fake_output(
    *,
    version: str = "47",
    vcc: float = 3.3,
    vout: float = 1.65,
    include_markers: bool = True,
) -> str:
    values = (
        "v(n0001) = " + f"{vcc:.12e}",
        "v(n0002) = " + f"{vout:.12e}",
        "i(v0001) = -1.650000000000e-04",
    )
    body = "\n".join((_OP_BEGIN, *values, _OP_END)) if include_markers else "\n".join(values)
    return f"ngspice-{version}\n{body}"


def _fake_engine(output: str, *, timeout_s: float = 2.0) -> NgspiceCircuitEngine:
    return NgspiceCircuitEngine(
        command_prefix=(sys.executable, "-c", f"print({output!r})"),
        timeout_s=timeout_s,
    )


def test_generated_dc_deck_is_deterministic_and_never_embeds_design_names() -> None:
    circuit = CompiledCircuit(
        analysis="dc",
        ground_node="ground; quit",
        nodes=("signal)\n.end", "supply with spaces", "ground; quit"),
        resistors=(
            ResistorElement("R1\n.control", "supply with spaces", "signal)\n.end", 10_000),
            ResistorElement("R2", "signal)\n.end", "ground; quit", 20_000),
        ),
        voltage_sources=(
            VoltageSourceElement("V unsafe", "supply with spaces", "ground; quit", 3.3),
        ),
    )

    first = build_dc_deck(circuit)
    second = build_dc_deck(circuit)

    assert first == second
    assert first.node_map.safe_for("ground; quit") == "0"
    assert first.node_map.canonical_for("n0001") == "signal)\n.end"
    assert first.node_map.canonical_for("n0002") == "supply with spaces"
    assert first.source_reference_for("v0001") == "V unsafe"
    for design_controlled_name in (
        "ground; quit",
        "signal)\n.end",
        "supply with spaces",
        "R1\n.control",
        "V unsafe",
    ):
        assert design_controlled_name not in first.text
    assert "r0001 n0002 n0001 10000" in first.text
    assert "v0001 n0002 0 DC" in first.text
    assert ".op" in first.text


def test_operating_point_parser_restores_canonical_names() -> None:
    deck = build_dc_deck(CircuitCompiler().compile(divider_project()))

    voltages, currents = parse_operating_point(_fake_output(), deck)

    assert dict(voltages) == pytest.approx({"GND": 0.0, "VCC": 3.3, "VOUT": 1.65})
    assert dict(currents) == pytest.approx({"V1": -0.000165})
    assert detect_ngspice_version("banner NGSPICE-47.2 build") == "47.2"


def test_fake_ngspice_run_returns_a_circuit_only_result() -> None:
    circuit = CircuitCompiler().compile(divider_project())

    result = _fake_engine(_fake_output()).run(circuit)

    assert result.success
    assert result.engine == "ngspice"
    assert result.engine_version == "47"
    assert result.voltage("VOUT") == pytest.approx(1.65)
    assert result.current("V1") == pytest.approx(-0.000165)
    assert not hasattr(result, "firmware_state")
    assert not hasattr(result, "uart_lines")


@pytest.mark.parametrize(
    ("output", "expected_code"),
    [
        (_fake_output(version="46"), "ngspice.unsupported_version"),
        (_fake_output(include_markers=False), "ngspice.invalid_output"),
        (
            f"ngspice-47\n{_OP_BEGIN}\nv(n0001) = not-a-number\n{_OP_END}",
            "ngspice.invalid_output",
        ),
    ],
)
def test_adapter_rejects_wrong_version_and_malformed_output(
    output: str,
    expected_code: str,
) -> None:
    result = _fake_engine(output).run(CircuitCompiler().compile(divider_project()))

    assert not result.success
    assert result.issues[-1].code == expected_code


def test_adapter_honors_timeout_and_cancellation() -> None:
    circuit = CircuitCompiler().compile(divider_project())
    sleeping = NgspiceCircuitEngine(
        command_prefix=(sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_s=0.05,
    )

    timed_out = sleeping.run(circuit)
    cancelled_event = Event()
    cancelled_event.set()
    cancelled = sleeping.run(circuit, cancel_event=cancelled_event)

    assert timed_out.issues[-1].code == "ngspice.timeout"
    assert cancelled.issues[-1].code == "ngspice.cancelled"


def test_adapter_cancels_a_running_process() -> None:
    circuit = CircuitCompiler().compile(divider_project())
    event = Event()
    canceller = Thread(target=lambda: event.wait(0.05) or event.set(), daemon=True)
    canceller.start()
    engine = NgspiceCircuitEngine(
        command_prefix=(sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_s=2,
    )

    result = engine.run(circuit, cancel_event=event)
    canceller.join(timeout=1)

    assert result.issues[-1].code == "ngspice.cancelled"


def test_blocking_circuit_issues_are_returned_without_execution() -> None:
    invalid = CircuitCompiler().compile(divider_project(), analysis="transient")

    result = _fake_engine(_fake_output()).run(invalid)

    assert not result.success
    assert result.issues[-1].code == "ngspice.invalid_circuit"
    assert any(issue.code == "circuit.unsupported_analysis" for issue in result.issues)


_NATIVE_NGSPICE = discover_tools().ngspice


@pytest.mark.skipif(_NATIVE_NGSPICE is None, reason="standalone ngspice is not installed")
@pytest.mark.parametrize(
    "fault",
    [
        None,
        CircuitFault(CircuitFaultKind.OPEN, reference="R1"),
        CircuitFault(CircuitFaultKind.WRONG_VALUE, reference="R2", value_ohm=20_000),
    ],
)
def test_native_ngspice_47_matches_the_owned_dc_solver(fault: CircuitFault | None) -> None:
    circuit = CircuitCompiler().compile(divider_project(), fault=fault)
    internal = DcMnaSolver().solve(circuit)

    external = NgspiceCircuitEngine(_NATIVE_NGSPICE, timeout_s=5).run(circuit)

    assert internal.success
    assert external.success, external.issues
    assert external.engine_version.split(".", 1)[0] == "47"
    assert dict(external.node_voltages) == pytest.approx(
        dict(internal.node_voltages), rel=1e-9, abs=1e-12
    )
    assert dict(external.source_currents) == pytest.approx(
        dict(internal.source_currents), rel=1e-9, abs=1e-12
    )
