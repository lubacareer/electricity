"""Owned DC circuit compiler and modified-nodal-analysis solver."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite

import numpy as np

from .connectivity import ErcIssue, SchematicCompiler
from .model import EdaProjectDocument, IssueSeverity

OPEN_RESISTANCE_OHM = 1e12
SHORT_RESISTANCE_OHM = 1e-3


class CircuitFaultKind(StrEnum):
    OPEN = "open"
    SHORT = "short"
    WRONG_VALUE = "wrong_value"


@dataclass(frozen=True, slots=True)
class CircuitFault:
    kind: CircuitFaultKind
    reference: str | None = None
    value_ohm: float | None = None
    net_a: str | None = None
    net_b: str | None = None


@dataclass(frozen=True, slots=True)
class ResistorElement:
    reference: str
    node_a: str
    node_b: str
    resistance_ohm: float


@dataclass(frozen=True, slots=True)
class VoltageSourceElement:
    reference: str
    positive_node: str
    negative_node: str
    voltage_v: float


@dataclass(frozen=True, slots=True)
class CompiledCircuit:
    analysis: str
    ground_node: str
    nodes: tuple[str, ...]
    resistors: tuple[ResistorElement, ...]
    voltage_sources: tuple[VoltageSourceElement, ...]
    issues: tuple[ErcIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)


@dataclass(frozen=True, slots=True)
class DcSimulationResult:
    success: bool
    node_voltages: tuple[tuple[str, float], ...] = ()
    source_currents: tuple[tuple[str, float], ...] = ()
    issues: tuple[ErcIssue, ...] = ()

    def voltage(self, net_name: str) -> float:
        try:
            return dict(self.node_voltages)[net_name]
        except KeyError as error:
            raise KeyError(f"no simulated voltage for net {net_name!r}") from error


def parse_engineering_value(text: str) -> float:
    """Parse the small, deterministic engineering notation used by templates."""

    normalized = text.strip().replace("Ω", "").replace("ohm", "").strip()
    if not normalized:
        raise ValueError("component value is empty")
    suffixes = {
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "µ": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "K": 1e3,
        "M": 1e6,
        "G": 1e9,
    }
    multiplier = 1.0
    if normalized[-1] in suffixes:
        multiplier = suffixes[normalized[-1]]
        normalized = normalized[:-1]
    value = float(normalized) * multiplier
    if not isfinite(value):
        raise ValueError("component value must be finite")
    return value


class CircuitCompiler:
    """Compile supported schematic symbols into a DC MNA-ready circuit."""

    def __init__(self, schematic_compiler: SchematicCompiler | None = None) -> None:
        self._schematic_compiler = schematic_compiler or SchematicCompiler()

    def compile(
        self,
        document: EdaProjectDocument,
        analysis: str = "dc",
        fault: CircuitFault | None = None,
    ) -> CompiledCircuit:
        graph = self._schematic_compiler.compile(document)
        issues = list(graph.issues)
        if analysis != "dc":
            issues.append(
                ErcIssue(
                    IssueSeverity.ERROR,
                    "circuit.unsupported_analysis",
                    f"Owned solver does not support {analysis!r} analysis",
                )
            )

        pin_nets = {(symbol_id, pin_id): net_id for symbol_id, pin_id, net_id in graph.pin_to_net}
        nets_by_id = {net.net_id: net for net in graph.nets}
        labelled_ground_nodes = {
            net.name for net in graph.nets if net.name.upper() in {"0", "GND", "GROUND"}
        }
        symbol_ground_nodes = {
            nets_by_id[pin_nets[(symbol.symbol_id, pin.pin_id)]].name
            for symbol in document.schematic.symbols
            if symbol.kind.casefold() == "ground"
            for pin in symbol.pins
            if (symbol.symbol_id, pin.pin_id) in pin_nets
        }
        ground_candidates = tuple(sorted(labelled_ground_nodes | symbol_ground_nodes))
        ground_node = ground_candidates[0] if ground_candidates else "GND"
        if len(ground_candidates) > 1:
            issues.append(
                ErcIssue(
                    IssueSeverity.ERROR,
                    "circuit.conflicting_ground",
                    "Ground symbols or labels refer to more than one electrical net",
                )
            )
        elif not ground_candidates:
            issues.append(
                ErcIssue(
                    IssueSeverity.ERROR,
                    "circuit.missing_ground",
                    "Circuit needs a net labelled GND or 0",
                )
            )

        resistors: list[ResistorElement] = []
        voltage_sources: list[VoltageSourceElement] = []
        supported_kinds = {"resistor", "voltage_source", "ground", "net_label"}
        for symbol in document.schematic.symbols:
            kind = symbol.kind.casefold()
            if kind not in supported_kinds:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "circuit.unsupported_symbol",
                        f"{symbol.reference} has no owned DC model",
                        (symbol.symbol_id,),
                    )
                )
                continue
            if kind in {"ground", "net_label"}:
                continue
            if len(symbol.pins) != 2:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "circuit.pin_count",
                        f"{symbol.reference} needs exactly two pins for its DC model",
                        (symbol.symbol_id,),
                    )
                )
                continue
            try:
                nodes = tuple(
                    nets_by_id[pin_nets[(symbol.symbol_id, pin.pin_id)]].name for pin in symbol.pins
                )
                value = parse_engineering_value(symbol.value)
            except (KeyError, ValueError) as error:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "circuit.invalid_component",
                        f"Cannot compile {symbol.reference}: {error}",
                        (symbol.symbol_id,),
                    )
                )
                continue
            if kind == "resistor":
                if value <= 0:
                    issues.append(
                        ErcIssue(
                            IssueSeverity.ERROR,
                            "circuit.invalid_resistance",
                            f"{symbol.reference} resistance must be positive",
                            (symbol.symbol_id,),
                        )
                    )
                    continue
                resistors.append(ResistorElement(symbol.reference, nodes[0], nodes[1], value))
            elif kind == "voltage_source":
                voltage_sources.append(
                    VoltageSourceElement(symbol.reference, nodes[0], nodes[1], value)
                )

        nodes = tuple(sorted({net.name for net in graph.nets} | {ground_node}))
        resistors, issues = self._apply_fault(resistors, fault, issues, set(nodes))
        return CompiledCircuit(
            analysis=analysis,
            ground_node=ground_node,
            nodes=nodes,
            resistors=tuple(sorted(resistors, key=lambda item: item.reference)),
            voltage_sources=tuple(sorted(voltage_sources, key=lambda item: item.reference)),
            issues=tuple(issues),
        )

    def _apply_fault(
        self,
        resistors: list[ResistorElement],
        fault: CircuitFault | None,
        issues: list[ErcIssue],
        available_nodes: set[str],
    ) -> tuple[list[ResistorElement], list[ErcIssue]]:
        if fault is None:
            return resistors, issues
        if fault.kind is CircuitFaultKind.SHORT and (
            fault.net_a is not None or fault.net_b is not None
        ):
            if (
                not fault.net_a
                or not fault.net_b
                or fault.net_a == fault.net_b
                or fault.net_a not in available_nodes
                or fault.net_b not in available_nodes
            ):
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "circuit.invalid_short_nets",
                        "Net-short fault requires two distinct existing net names",
                    )
                )
                return resistors, issues
            return [
                *resistors,
                ResistorElement("FAULT_SHORT", fault.net_a, fault.net_b, SHORT_RESISTANCE_OHM),
            ], issues
        target_index = next(
            (
                index
                for index, resistor in enumerate(resistors)
                if resistor.reference == fault.reference
            ),
            None,
        )
        if target_index is None:
            issues.append(
                ErcIssue(
                    IssueSeverity.ERROR,
                    "circuit.fault_target",
                    "Fault target is not a modelled resistor",
                    (fault.reference,) if fault.reference else (),
                )
            )
            return resistors, issues
        target = resistors[target_index]
        if fault.kind is CircuitFaultKind.OPEN:
            replacement = replace(target, resistance_ohm=OPEN_RESISTANCE_OHM)
        elif fault.kind is CircuitFaultKind.SHORT:
            replacement = replace(target, resistance_ohm=SHORT_RESISTANCE_OHM)
        elif fault.kind is CircuitFaultKind.WRONG_VALUE:
            if fault.value_ohm is None or fault.value_ohm <= 0 or not isfinite(fault.value_ohm):
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "circuit.invalid_fault_value",
                        "Wrong-value fault requires a positive finite resistance",
                        (target.reference,),
                    )
                )
                return resistors, issues
            replacement = replace(target, resistance_ohm=fault.value_ohm)
        else:  # pragma: no cover - exhaustive StrEnum protection
            raise ValueError(f"unsupported fault kind: {fault.kind}")
        updated = list(resistors)
        updated[target_index] = replacement
        return updated, issues


class DcMnaSolver:
    """Solve resistors and independent voltage sources using DC MNA."""

    def solve(self, circuit: CompiledCircuit) -> DcSimulationResult:
        if not circuit.ready:
            return DcSimulationResult(False, issues=circuit.issues)
        non_ground_nodes = tuple(node for node in circuit.nodes if node != circuit.ground_node)
        node_indexes = {node: index for index, node in enumerate(non_ground_nodes)}
        node_count = len(non_ground_nodes)
        source_count = len(circuit.voltage_sources)
        size = node_count + source_count
        matrix = np.zeros((size, size), dtype=float)
        right_hand = np.zeros(size, dtype=float)

        for resistor in circuit.resistors:
            conductance = 1.0 / resistor.resistance_ohm
            first = node_indexes.get(resistor.node_a)
            second = node_indexes.get(resistor.node_b)
            if first is not None:
                matrix[first, first] += conductance
            if second is not None:
                matrix[second, second] += conductance
            if first is not None and second is not None:
                matrix[first, second] -= conductance
                matrix[second, first] -= conductance

        for source_index, source in enumerate(circuit.voltage_sources):
            row = node_count + source_index
            positive = node_indexes.get(source.positive_node)
            negative = node_indexes.get(source.negative_node)
            if positive is not None:
                matrix[positive, row] += 1.0
                matrix[row, positive] += 1.0
            if negative is not None:
                matrix[negative, row] -= 1.0
                matrix[row, negative] -= 1.0
            right_hand[row] = source.voltage_v

        try:
            solution = np.linalg.solve(matrix, right_hand)
        except np.linalg.LinAlgError:
            issue = ErcIssue(
                IssueSeverity.ERROR,
                "circuit.singular",
                "Circuit cannot be solved; check for floating or contradictory nets",
            )
            return DcSimulationResult(False, issues=(*circuit.issues, issue))

        voltages = [(circuit.ground_node, 0.0)]
        voltages.extend((node, float(solution[index])) for node, index in node_indexes.items())
        currents = tuple(
            (source.reference, float(solution[node_count + index]))
            for index, source in enumerate(circuit.voltage_sources)
        )
        return DcSimulationResult(
            True,
            node_voltages=tuple(sorted(voltages)),
            source_currents=currents,
            issues=circuit.issues,
        )


def simulate_dc(
    document: EdaProjectDocument,
    fault: CircuitFault | None = None,
) -> DcSimulationResult:
    return DcMnaSolver().solve(CircuitCompiler().compile(document, fault=fault))
