"""Deterministic ngspice adapter for editable EDA DC circuits.

The adapter deliberately consumes only the small, owned :class:`CompiledCircuit`
surface.  It never executes source-project netlists and it returns a circuit-only
result, keeping firmware state outside this layer.
"""

from __future__ import annotations

import math
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from ..engines.process import run_isolated_process
from ..tooling import discover_tools
from .connectivity import ErcIssue
from .model import IssueSeverity
from .simulation import CompiledCircuit, ResistorElement, VoltageSourceElement

NGSPICE_REQUIRED_MAJOR = "47"
_OP_BEGIN = "__SMD_TWIN_OP_BEGIN__"
_OP_END = "__SMD_TWIN_OP_END__"
_VERSION_PATTERN = re.compile(r"\bngspice(?:-|\s+)([0-9][\w.+-]*)", re.IGNORECASE)
_NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?"
_VALUE_PATTERN = re.compile(
    rf"^(?P<kind>[vi])\((?P<name>[a-z][0-9]{{4}})\)\s*=\s*(?P<value>{_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NgspiceNodeMap:
    """A reversible mapping from canonical design-net names to safe SPICE nodes."""

    entries: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        canonical = [name for name, _safe in self.entries]
        safe = [name for _canonical, name in self.entries]
        if len(canonical) != len(set(canonical)) or len(safe) != len(set(safe)):
            raise ValueError("ngspice node mapping must be one-to-one")
        if any(not name for name in canonical):
            raise ValueError("canonical net names must not be empty")
        if any(name != "0" and not re.fullmatch(r"n[0-9]{4}", name) for name in safe):
            raise ValueError("ngspice node mapping contains an unsafe node")

    def safe_for(self, canonical_name: str) -> str:
        try:
            return dict(self.entries)[canonical_name]
        except KeyError as error:
            raise KeyError(f"no ngspice node for canonical net {canonical_name!r}") from error

    def canonical_for(self, safe_name: str) -> str:
        try:
            return {safe: canonical for canonical, safe in self.entries}[safe_name]
        except KeyError as error:
            raise KeyError(f"no canonical net for ngspice node {safe_name!r}") from error


@dataclass(frozen=True, slots=True)
class GeneratedDcDeck:
    """A generated working deck plus the mappings needed to interpret its output."""

    text: str
    node_map: NgspiceNodeMap
    source_entries: tuple[tuple[str, str], ...]

    def source_reference_for(self, safe_name: str) -> str:
        try:
            return {safe: reference for reference, safe in self.source_entries}[safe_name]
        except KeyError as error:
            raise KeyError(f"no canonical source for ngspice element {safe_name!r}") from error


@dataclass(frozen=True, slots=True)
class NgspiceDcResult:
    """Typed circuit-only result produced by :class:`NgspiceCircuitEngine`."""

    success: bool
    engine_version: str = "unknown"
    node_voltages: tuple[tuple[str, float], ...] = ()
    source_currents: tuple[tuple[str, float], ...] = ()
    issues: tuple[ErcIssue, ...] = ()
    stdout: str = ""
    stderr: str = ""
    engine: str = field(default="ngspice", init=False)

    def voltage(self, net_name: str) -> float:
        try:
            return dict(self.node_voltages)[net_name]
        except KeyError as error:
            raise KeyError(f"no simulated voltage for net {net_name!r}") from error

    def current(self, source_reference: str) -> float:
        try:
            return dict(self.source_currents)[source_reference]
        except KeyError as error:
            raise KeyError(f"no simulated current for source {source_reference!r}") from error


def _spice_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("SPICE values must be finite")
    return f"{value:.17g}"


def _all_nodes(circuit: CompiledCircuit) -> tuple[str, ...]:
    nodes = set(circuit.nodes)
    nodes.add(circuit.ground_node)
    for resistor in circuit.resistors:
        nodes.update((resistor.node_a, resistor.node_b))
    for source in circuit.voltage_sources:
        nodes.update((source.positive_node, source.negative_node))
    if "" in nodes:
        raise ValueError("circuit net names must not be empty")
    return tuple(sorted(nodes))


def _node_map(circuit: CompiledCircuit) -> NgspiceNodeMap:
    non_ground = [node for node in _all_nodes(circuit) if node != circuit.ground_node]
    if len(non_ground) > 9_999:
        raise ValueError("ngspice adapter supports at most 9,999 non-ground nets")
    entries = [(circuit.ground_node, "0")]
    entries.extend((node, f"n{index:04d}") for index, node in enumerate(non_ground, start=1))
    return NgspiceNodeMap(tuple(entries))


def _validate_resistor(element: ResistorElement) -> None:
    if not math.isfinite(element.resistance_ohm) or element.resistance_ohm <= 0:
        raise ValueError(f"{element.reference} resistance must be positive and finite")


def _validate_source(element: VoltageSourceElement) -> None:
    if not math.isfinite(element.voltage_v):
        raise ValueError(f"{element.reference} voltage must be finite")


def build_dc_deck(circuit: CompiledCircuit) -> GeneratedDcDeck:
    """Build a deterministic ``.op`` deck without embedding design-controlled names."""

    if circuit.analysis != "dc":
        raise ValueError("ngspice DC adapter accepts only dc analysis")
    if not circuit.ready:
        raise ValueError("circuit has blocking compilation issues")

    node_map = _node_map(circuit)
    resistors = tuple(
        sorted(
            circuit.resistors,
            key=lambda item: (
                item.reference,
                item.node_a,
                item.node_b,
                item.resistance_ohm,
            ),
        )
    )
    sources = tuple(
        sorted(
            circuit.voltage_sources,
            key=lambda item: (
                item.reference,
                item.positive_node,
                item.negative_node,
                item.voltage_v,
            ),
        )
    )
    if len(resistors) > 9_999 or len(sources) > 9_999:
        raise ValueError("ngspice adapter supports at most 9,999 elements of each type")

    circuit_lines: list[str] = []
    for index, resistor in enumerate(resistors, start=1):
        _validate_resistor(resistor)
        circuit_lines.append(
            " ".join(
                (
                    f"r{index:04d}",
                    node_map.safe_for(resistor.node_a),
                    node_map.safe_for(resistor.node_b),
                    _spice_number(resistor.resistance_ohm),
                )
            )
        )

    source_entries: list[tuple[str, str]] = []
    for index, source in enumerate(sources, start=1):
        _validate_source(source)
        safe_reference = f"v{index:04d}"
        source_entries.append((source.reference, safe_reference))
        circuit_lines.append(
            " ".join(
                (
                    safe_reference,
                    node_map.safe_for(source.positive_node),
                    node_map.safe_for(source.negative_node),
                    "DC",
                    _spice_number(source.voltage_v),
                )
            )
        )

    probes = [f"print v({safe})" for _canonical, safe in node_map.entries if safe != "0"]
    probes.extend(f"print i({safe})" for _canonical, safe in source_entries)
    lines = [
        "* SMD Twin Lab generated DC working deck",
        "* Canonical design names are kept outside this temporary deck.",
        ".options numdgt=15",
        *circuit_lines,
        ".op",
        ".control",
        "set noaskquit",
        "op",
        f"echo {_OP_BEGIN}",
        *probes,
        f"echo {_OP_END}",
        "quit",
        ".endc",
        ".end",
        "",
    ]
    return GeneratedDcDeck(
        text="\n".join(lines),
        node_map=node_map,
        source_entries=tuple(source_entries),
    )


def detect_ngspice_version(output: str) -> str | None:
    """Return the first ngspice version token found in process output."""

    match = _VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def parse_operating_point(
    output: str,
    deck: GeneratedDcDeck,
) -> tuple[tuple[tuple[str, float], ...], tuple[tuple[str, float], ...]]:
    """Parse the marker-bounded scalar values printed by the generated ``.op`` deck."""

    lines = output.splitlines()
    try:
        begin = next(index for index, line in enumerate(lines) if line.strip() == _OP_BEGIN)
        end = next(
            index
            for index, line in enumerate(lines[begin + 1 :], start=begin + 1)
            if line.strip() == _OP_END
        )
    except StopIteration as error:
        raise ValueError("ngspice operating-point markers are missing") from error
    if end <= begin:
        raise ValueError("ngspice operating-point markers are out of order")

    safe_nodes = {safe: canonical for canonical, safe in deck.node_map.entries if safe != "0"}
    safe_sources = {safe: canonical for canonical, safe in deck.source_entries}
    voltages: dict[str, float] = {}
    currents: dict[str, float] = {}
    for raw_line in lines[begin + 1 : end]:
        line = raw_line.strip()
        if not line:
            continue
        match = _VALUE_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"malformed ngspice operating-point line: {line!r}")
        safe_name = match.group("name").casefold()
        value = float(match.group("value").replace("D", "E").replace("d", "e"))
        if not math.isfinite(value):
            raise ValueError("ngspice produced a non-finite operating-point value")
        if match.group("kind").casefold() == "v":
            if safe_name not in safe_nodes:
                raise ValueError(f"ngspice returned an unexpected node {safe_name!r}")
            canonical = safe_nodes[safe_name]
            if canonical in voltages:
                raise ValueError(f"ngspice returned duplicate voltage for {safe_name!r}")
            voltages[canonical] = value
        else:
            if safe_name not in safe_sources:
                raise ValueError(f"ngspice returned an unexpected source {safe_name!r}")
            canonical = safe_sources[safe_name]
            if canonical in currents:
                raise ValueError(f"ngspice returned duplicate current for {safe_name!r}")
            currents[canonical] = value

    expected_nodes = set(safe_nodes.values())
    expected_sources = set(safe_sources.values())
    missing_nodes = sorted(expected_nodes - voltages.keys())
    missing_sources = sorted(expected_sources - currents.keys())
    if missing_nodes or missing_sources:
        missing = [*(f"net {name!r}" for name in missing_nodes)]
        missing.extend(f"source {name!r}" for name in missing_sources)
        raise ValueError(f"ngspice omitted operating-point values for {', '.join(missing)}")

    voltages[deck.node_map.canonical_for("0")] = 0.0
    return tuple(sorted(voltages.items())), tuple(sorted(currents.items()))


class NgspiceCircuitEngine:
    """Run owned compiled DC circuits in a pinned standalone ngspice 47 process."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        command_prefix: Sequence[str | Path] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        discovered = discover_tools().ngspice if executable is None else Path(executable)
        if command_prefix is not None:
            self._command_prefix = tuple(str(part) for part in command_prefix)
        elif discovered is not None:
            self._command_prefix = (str(discovered),)
        else:
            self._command_prefix = ()
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._command_prefix) and Path(self._command_prefix[0]).is_file()

    def run(
        self,
        circuit: CompiledCircuit,
        *,
        cancel_event: Event | None = None,
    ) -> NgspiceDcResult:
        if cancel_event is not None and cancel_event.is_set():
            return self._failure(circuit, "ngspice.cancelled", "ngspice simulation was cancelled")
        if not self.available:
            return self._failure(
                circuit,
                "ngspice.unavailable",
                "Standalone ngspice 47 was not found",
            )
        try:
            deck = build_dc_deck(circuit)
        except ValueError as error:
            return self._failure(circuit, "ngspice.invalid_circuit", str(error))

        try:
            with tempfile.TemporaryDirectory(prefix="smd-twin-eda-ngspice-") as temporary:
                work_dir = Path(temporary)
                deck_path = work_dir / "design.cir"
                deck_path.write_text(deck.text, encoding="utf-8", newline="\n")
                process = run_isolated_process(
                    (*self._command_prefix, "-b", deck_path.name),
                    cwd=work_dir,
                    timeout_s=self.timeout_s,
                    cancel_event=cancel_event,
                )
        except OSError as error:
            return self._failure(circuit, "ngspice.start_failed", str(error))

        if process.cancelled:
            return self._failure(
                circuit,
                "ngspice.cancelled",
                "ngspice simulation was cancelled",
                stdout=process.stdout,
                stderr=process.stderr,
            )
        if process.timed_out:
            return self._failure(
                circuit,
                "ngspice.timeout",
                f"ngspice exceeded the {self.timeout_s:g} second timeout",
                stdout=process.stdout,
                stderr=process.stderr,
            )
        if process.returncode != 0:
            return self._failure(
                circuit,
                "ngspice.failed",
                f"ngspice exited with status {process.returncode}",
                stdout=process.stdout,
                stderr=process.stderr,
            )

        combined_output = f"{process.stdout}\n{process.stderr}"
        version = detect_ngspice_version(combined_output)
        if version is None or version.split(".", 1)[0] != NGSPICE_REQUIRED_MAJOR:
            return self._failure(
                circuit,
                "ngspice.unsupported_version",
                f"Expected ngspice 47, detected {version or 'unknown'}",
                engine_version=version or "unknown",
                stdout=process.stdout,
                stderr=process.stderr,
            )

        try:
            voltages, currents = parse_operating_point(combined_output, deck)
        except ValueError as error:
            return self._failure(
                circuit,
                "ngspice.invalid_output",
                str(error),
                engine_version=version,
                stdout=process.stdout,
                stderr=process.stderr,
            )
        return NgspiceDcResult(
            success=True,
            engine_version=version,
            node_voltages=voltages,
            source_currents=currents,
            issues=circuit.issues,
            stdout=process.stdout,
            stderr=process.stderr,
        )

    @staticmethod
    def _failure(
        circuit: CompiledCircuit,
        code: str,
        message: str,
        *,
        engine_version: str = "unknown",
        stdout: str = "",
        stderr: str = "",
    ) -> NgspiceDcResult:
        issue = ErcIssue(IssueSeverity.ERROR, code, message)
        return NgspiceDcResult(
            success=False,
            engine_version=engine_version,
            issues=(*circuit.issues, issue),
            stdout=stdout,
            stderr=stderr,
        )
