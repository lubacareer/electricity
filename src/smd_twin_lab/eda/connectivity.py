"""Schematic connectivity compilation and electrical-rule checks."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .model import (
    EdaProjectDocument,
    IssueSeverity,
    PinElectricalType,
    PointNm,
    SchematicSymbol,
    SchematicWire,
)


@dataclass(frozen=True, slots=True)
class PinConnection:
    symbol_id: str
    pin_id: str
    reference: str
    pin_number: str
    electrical_type: PinElectricalType


@dataclass(frozen=True, slots=True)
class ErcIssue:
    severity: IssueSeverity
    code: str
    message: str
    item_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectivityNet:
    net_id: str
    name: str
    pins: tuple[PinConnection, ...]
    wire_ids: tuple[str, ...] = ()
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectivityGraph:
    nets: tuple[ConnectivityNet, ...]
    issues: tuple[ErcIssue, ...] = ()
    pin_to_net: tuple[tuple[str, str, str], ...] = ()

    def net_for_pin(self, symbol_id: str, pin_id: str) -> ConnectivityNet | None:
        net_id = next(
            (
                candidate
                for candidate_symbol, candidate_pin, candidate in self.pin_to_net
                if candidate_symbol == symbol_id and candidate_pin == pin_id
            ),
            None,
        )
        return next((net for net in self.nets if net.net_id == net_id), None)

    def named(self, name: str) -> ConnectivityNet | None:
        return next((net for net in self.nets if net.name == name), None)


class _DisjointSet:
    def __init__(self, keys: list[str]) -> None:
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def union(self, first: str, second: str) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            low, high = sorted((first_root, second_root))
            self.parent[high] = low


def _segments(wire: SchematicWire) -> tuple[tuple[PointNm, PointNm], ...]:
    return tuple(zip(wire.points, wire.points[1:], strict=False))


def point_on_segment(point: PointNm, start: PointNm, end: PointNm) -> bool:
    """Return whether *point* lies exactly on the integer-coordinate segment."""

    cross = (point.y_nm - start.y_nm) * (end.x_nm - start.x_nm) - (point.x_nm - start.x_nm) * (
        end.y_nm - start.y_nm
    )
    if cross != 0:
        return False
    return min(start.x_nm, end.x_nm) <= point.x_nm <= max(start.x_nm, end.x_nm) and min(
        start.y_nm, end.y_nm
    ) <= point.y_nm <= max(start.y_nm, end.y_nm)


def point_on_wire(point: PointNm, wire: SchematicWire) -> bool:
    return any(point_on_segment(point, start, end) for start, end in _segments(wire))


def _wire_endpoints(wire: SchematicWire) -> tuple[PointNm, PointNm]:
    return wire.points[0], wire.points[-1]


class SchematicCompiler:
    """Compile explicit drawing topology into named electrical nets.

    Two wires connect when an endpoint touches the other wire, or when an
    explicit junction lies on both.  A pure interior-to-interior crossing is
    deliberately not a connection.
    """

    def compile(self, document: EdaProjectDocument) -> ConnectivityGraph:
        schematic = document.schematic
        keys: list[str] = []
        pin_records: dict[str, tuple[SchematicSymbol, object, PointNm]] = {}
        for symbol in schematic.symbols:
            for pin in symbol.pins:
                key = f"pin:{symbol.symbol_id}:{pin.pin_id}"
                keys.append(key)
                pin_records[key] = (symbol, pin, symbol.pin_position(pin))

        wire_records = {f"wire:{wire.wire_id}": wire for wire in schematic.wires}
        label_records = {f"label:{label.label_id}": label for label in schematic.labels}
        junction_records = {
            f"junction:{junction.junction_id}": junction for junction in schematic.junctions
        }
        keys.extend(wire_records)
        keys.extend(label_records)
        keys.extend(junction_records)
        union = _DisjointSet(keys)

        pin_items = list(pin_records.items())
        for index, (first_key, (_, _, first_position)) in enumerate(pin_items):
            for second_key, (_, _, second_position) in pin_items[index + 1 :]:
                if first_position == second_position:
                    union.union(first_key, second_key)

        for pin_key, (_, _, position) in pin_records.items():
            for wire_key, wire in wire_records.items():
                if point_on_wire(position, wire):
                    union.union(pin_key, wire_key)

        wire_items = list(wire_records.items())
        for index, (first_key, first_wire) in enumerate(wire_items):
            first_endpoints = _wire_endpoints(first_wire)
            for second_key, second_wire in wire_items[index + 1 :]:
                second_endpoints = _wire_endpoints(second_wire)
                if any(point_on_wire(point, second_wire) for point in first_endpoints) or any(
                    point_on_wire(point, first_wire) for point in second_endpoints
                ):
                    union.union(first_key, second_key)

        for junction_key, junction in junction_records.items():
            touching: list[str] = []
            touching.extend(
                key
                for key, (_, _, position) in pin_records.items()
                if position == junction.position
            )
            touching.extend(
                key for key, wire in wire_records.items() if point_on_wire(junction.position, wire)
            )
            for key in touching:
                union.union(junction_key, key)

        label_attachment: dict[str, bool] = {}
        labels_by_text: dict[str, list[str]] = {}
        for label_key, label in label_records.items():
            touching: list[str] = []
            touching.extend(
                key for key, (_, _, position) in pin_records.items() if position == label.position
            )
            touching.extend(
                key for key, wire in wire_records.items() if point_on_wire(label.position, wire)
            )
            label_attachment[label_key] = bool(touching)
            for key in touching:
                union.union(label_key, key)
            labels_by_text.setdefault(label.text, []).append(label_key)
        for label_keys in labels_by_text.values():
            for label_key in label_keys[1:]:
                union.union(label_keys[0], label_key)

        groups: dict[str, list[str]] = {}
        for key in keys:
            groups.setdefault(union.find(key), []).append(key)

        issues = self._base_issues(document, label_attachment, wire_records, pin_records)
        sortable_groups: list[tuple[str, list[str]]] = []
        for members in groups.values():
            signature = "|".join(sorted(members))
            sortable_groups.append((signature, members))
        sortable_groups.sort(key=lambda item: item[0])

        nets: list[ConnectivityNet] = []
        pin_to_net: list[tuple[str, str, str]] = []
        for sequence, (signature, members) in enumerate(sortable_groups, start=1):
            labels = sorted(
                (label_records[key] for key in members if key in label_records),
                key=lambda label: (label.text, label.label_id),
            )
            label_names = sorted({label.text for label in labels if label.text})
            name = label_names[0] if label_names else f"N${sequence}"
            net_id = "net-" + sha256(signature.encode("utf-8")).hexdigest()[:16]
            pins = tuple(
                sorted(
                    (
                        PinConnection(
                            symbol_id=symbol.symbol_id,
                            pin_id=pin.pin_id,
                            reference=symbol.reference,
                            pin_number=pin.number,
                            electrical_type=pin.electrical_type,
                        )
                        for key, (symbol, pin, _) in pin_records.items()
                        if key in members
                    ),
                    key=lambda connection: (
                        connection.reference,
                        connection.pin_number,
                        connection.pin_id,
                    ),
                )
            )
            if len(label_names) > 1:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "erc.conflicting_labels",
                        f"Net contains conflicting labels: {', '.join(label_names)}",
                        tuple(label.label_id for label in labels),
                    )
                )
            drivers = tuple(
                pin
                for pin in pins
                if pin.electrical_type in (PinElectricalType.OUTPUT, PinElectricalType.POWER_OUTPUT)
            )
            if len(drivers) > 1:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "erc.driver_conflict",
                        f"Net {name} has multiple outputs",
                        tuple(pin.symbol_id for pin in drivers),
                    )
                )
            net = ConnectivityNet(
                net_id=net_id,
                name=name,
                pins=pins,
                wire_ids=tuple(
                    sorted(wire_records[key].wire_id for key in members if key in wire_records)
                ),
                label_ids=tuple(label.label_id for label in labels),
            )
            nets.append(net)
            pin_to_net.extend((pin.symbol_id, pin.pin_id, net_id) for pin in pins)

        for net in nets:
            if len(net.pins) == 1:
                pin_record = next(
                    pin
                    for pin in pin_records.values()
                    if pin[0].symbol_id == net.pins[0].symbol_id
                    and pin[1].pin_id == net.pins[0].pin_id
                )
                if (
                    pin_record[1].required
                    and pin_record[1].electrical_type is not PinElectricalType.NO_CONNECT
                ):
                    issues.append(
                        ErcIssue(
                            IssueSeverity.WARNING,
                            "erc.dangling_pin",
                            f"Pin {net.pins[0].reference}.{net.pins[0].pin_number} is unconnected",
                            (net.pins[0].symbol_id, net.pins[0].pin_id),
                        )
                    )

        return ConnectivityGraph(
            nets=tuple(sorted(nets, key=lambda net: (net.name, net.net_id))),
            issues=tuple(
                sorted(
                    issues,
                    key=lambda issue: (issue.severity, issue.code, issue.item_ids),
                )
            ),
            pin_to_net=tuple(sorted(pin_to_net)),
        )

    def _base_issues(
        self,
        document: EdaProjectDocument,
        label_attachment: dict[str, bool],
        wire_records: dict[str, SchematicWire],
        pin_records: dict[str, tuple[SchematicSymbol, object, PointNm]],
    ) -> list[ErcIssue]:
        issues: list[ErcIssue] = []
        references: dict[str, list[str]] = {}
        for symbol in document.schematic.symbols:
            references.setdefault(symbol.reference, []).append(symbol.symbol_id)
        for reference, symbol_ids in references.items():
            if len(symbol_ids) > 1:
                issues.append(
                    ErcIssue(
                        IssueSeverity.ERROR,
                        "erc.duplicate_reference",
                        f"Reference {reference} is used more than once",
                        tuple(sorted(symbol_ids)),
                    )
                )
        for label_key, attached in label_attachment.items():
            if not attached:
                label = next(
                    label
                    for label in document.schematic.labels
                    if f"label:{label.label_id}" == label_key
                )
                issues.append(
                    ErcIssue(
                        IssueSeverity.WARNING,
                        "erc.unattached_label",
                        f"Label {label.text} is not attached to a wire or pin",
                        (label.label_id,),
                    )
                )
        for wire_key, wire in wire_records.items():
            if not any(point_on_wire(position, wire) for _, _, position in pin_records.values()):
                issues.append(
                    ErcIssue(
                        IssueSeverity.WARNING,
                        "erc.wire_without_pin",
                        "Wire is not attached to a component pin",
                        (wire_key.removeprefix("wire:"),),
                    )
                )
        return issues
