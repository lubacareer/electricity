"""PCB synchronization, deterministic checks, and routing contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from math import cos, hypot, radians, sin
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid5

from .connectivity import SchematicCompiler
from .model import (
    AssetKind,
    BoardDocument,
    BoardFootprint,
    BoardPad,
    BoardSide,
    BoardTrack,
    CopperLayer,
    EdaProjectDocument,
    IssueSeverity,
    NetClass,
    PointNm,
    SchematicSymbol,
    mm,
)

_ROUTE_NAMESPACE = UUID("7fe5ae4a-9f1d-43e5-b08f-9bde8eb39468")


@dataclass(frozen=True, slots=True)
class DrcIssue:
    severity: IssueSeverity
    code: str
    message: str
    item_ids: tuple[str, ...] = ()
    location: PointNm | None = None


@dataclass(frozen=True, slots=True)
class DesignCheckReport:
    revision: int
    issues: tuple[DrcIssue, ...] = ()
    stale: bool = False

    @property
    def error_count(self) -> int:
        return sum(issue.severity is IssueSeverity.ERROR for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity is IssueSeverity.WARNING for issue in self.issues)

    @property
    def unconnected_count(self) -> int:
        return sum(issue.code == "drc.unrouted_net" for issue in self.issues)

    @property
    def clean(self) -> bool:
        return not self.stale and not self.issues


@dataclass(frozen=True, slots=True)
class BoardUpdate:
    document: EdaProjectDocument
    added_footprint_ids: tuple[str, ...] = ()
    updated_footprint_ids: tuple[str, ...] = ()
    removed_footprint_ids: tuple[str, ...] = ()
    issues: tuple[DrcIssue, ...] = ()


def _stable_id(*parts: object) -> str:
    return str(uuid5(_ROUTE_NAMESPACE, ":".join(str(part) for part in parts)))


class BoardSynchronizer:
    """Synchronize symbols and pad nets while preserving placed UUID objects."""

    def __init__(self, compiler: SchematicCompiler | None = None) -> None:
        self._compiler = compiler or SchematicCompiler()

    def update_from_schematic(self, document: EdaProjectDocument) -> BoardUpdate:
        graph = self._compiler.compile(document)
        pin_nets = {(symbol_id, pin_id): net_id for symbol_id, pin_id, net_id in graph.pin_to_net}
        nets = {net.net_id: net.name for net in graph.nets}
        snapshots = {
            asset.asset_id: asset
            for asset in document.library_assets
            if asset.kind is AssetKind.FOOTPRINT
        }
        existing = {footprint.symbol_id: footprint for footprint in document.board.footprints}
        symbols = {symbol.symbol_id: symbol for symbol in document.schematic.symbols}
        footprints: list[BoardFootprint] = []
        added: list[str] = []
        updated: list[str] = []
        issues: list[DrcIssue] = []

        for sequence, symbol in enumerate(document.schematic.symbols):
            if not symbol.footprint_id:
                issues.append(
                    DrcIssue(
                        IssueSeverity.WARNING,
                        "pcb.footprint_unassigned",
                        f"{symbol.reference} has no assigned footprint",
                        (symbol.symbol_id,),
                    )
                )
                continue
            old = existing.get(symbol.symbol_id)
            if old is not None and old.library_id == symbol.footprint_id:
                pads_by_number = {pad.pin_number: pad for pad in old.pads}
                synchronized_pads = tuple(
                    replace(
                        pad,
                        net=self._net_for_pin(symbol, pad.pin_number, pin_nets, nets),
                    )
                    for pad in old.pads
                )
                synchronized = replace(
                    old,
                    reference=symbol.reference,
                    pads=synchronized_pads,
                )
                footprints.append(synchronized)
                if synchronized != old:
                    updated.append(old.footprint_id)
                missing_numbers = {pin.number for pin in symbol.pins} - pads_by_number.keys()
                for number in sorted(missing_numbers):
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "pcb.footprint_pin_missing",
                            f"{symbol.reference} pin {number} is absent from its footprint",
                            (symbol.symbol_id, old.footprint_id),
                        )
                    )
                continue

            snapshot = snapshots.get(symbol.footprint_id)
            if snapshot is None:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "pcb.footprint_snapshot_missing",
                        f"{symbol.reference} needs a selected footprint snapshot",
                        (symbol.symbol_id,),
                    )
                )
                if old is not None:
                    footprints.append(old)
                continue
            try:
                footprint = self._from_snapshot(
                    document,
                    symbol,
                    snapshot.payload_json,
                    sequence,
                    old,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "pcb.footprint_snapshot_invalid",
                        f"Cannot place {symbol.reference}: {error}",
                        (symbol.symbol_id,),
                    )
                )
                if old is not None:
                    footprints.append(old)
                continue
            footprint = replace(
                footprint,
                pads=tuple(
                    replace(
                        pad,
                        net=self._net_for_pin(symbol, pad.pin_number, pin_nets, nets),
                    )
                    for pad in footprint.pads
                ),
            )
            footprints.append(footprint)
            if old is None:
                added.append(footprint.footprint_id)
            else:
                updated.append(footprint.footprint_id)

        removed = tuple(
            sorted(
                footprint.footprint_id
                for footprint in document.board.footprints
                if footprint.symbol_id not in symbols
            )
        )
        new_board = replace(document.board, footprints=tuple(footprints))
        changed = new_board != document.board
        updated_document = document.revised(board=new_board) if changed else document
        return BoardUpdate(
            updated_document,
            tuple(sorted(added)),
            tuple(sorted(updated)),
            removed,
            tuple(sorted(issues, key=_issue_key)),
        )

    @staticmethod
    def _net_for_pin(
        symbol: SchematicSymbol,
        pin_number: str,
        pin_nets: dict[tuple[str, str], str],
        nets: dict[str, str],
    ) -> str:
        matching_pin = next(
            (pin for pin in symbol.pins if pin.number == pin_number),
            None,
        )
        if matching_pin is None:
            return ""
        net_id = pin_nets.get((symbol.symbol_id, matching_pin.pin_id))
        return nets.get(net_id, "")

    @staticmethod
    def _from_snapshot(
        document: EdaProjectDocument,
        symbol: SchematicSymbol,
        payload_json: str,
        sequence: int,
        old: BoardFootprint | None,
    ) -> BoardFootprint:
        payload = json.loads(payload_json)
        pads_payload = payload["pads"]
        if not isinstance(pads_payload, list) or not pads_payload:
            raise ValueError("footprint snapshot has no pads")
        position = old.position if old else PointNm(mm(10 + sequence * 10), mm(10))
        footprint_id = (
            old.footprint_id
            if old
            else _stable_id(
                document.project_id,
                symbol.symbol_id,
                "footprint",
            )
        )
        old_pads = {pad.pin_number: pad for pad in old.pads} if old else {}
        pads = tuple(
            BoardPad(
                pad_id=(
                    old_pads[number].pad_id
                    if number in old_pads
                    else _stable_id(document.project_id, symbol.symbol_id, "pad", number)
                ),
                pin_number=number,
                offset=PointNm(int(item["x_nm"]), int(item["y_nm"])),
                width_nm=int(item["width_nm"]),
                height_nm=int(item["height_nm"]),
                shape=str(item.get("shape", "rect")),
                layers=tuple(
                    CopperLayer(layer) for layer in item.get("layers", (CopperLayer.FRONT,))
                ),
                drill_nm=int(item.get("drill_nm", 0)),
            )
            for item in pads_payload
            for number in (str(item["pin_number"]),)
        )
        return BoardFootprint(
            footprint_id=footprint_id,
            reference=symbol.reference,
            library_id=symbol.footprint_id,
            symbol_id=symbol.symbol_id,
            position=position,
            pads=pads,
            rotation_deg=old.rotation_deg if old else 0,
            side=old.side if old else BoardSide.FRONT,
            courtyard_width_nm=int(payload.get("courtyard_width_nm", 0)),
            courtyard_height_nm=int(payload.get("courtyard_height_nm", 0)),
        )


@dataclass(frozen=True, slots=True)
class _CopperItem:
    item_id: str
    net: str
    start: PointNm
    end: PointNm
    radius_nm: float
    layers: frozenset[CopperLayer]


def _distance(first: PointNm, second: PointNm) -> float:
    return hypot(first.x_nm - second.x_nm, first.y_nm - second.y_nm)


def _point_segment_distance(point: PointNm, start: PointNm, end: PointNm) -> float:
    delta_x = end.x_nm - start.x_nm
    delta_y = end.y_nm - start.y_nm
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0:
        return _distance(point, start)
    projection = (
        (point.x_nm - start.x_nm) * delta_x + (point.y_nm - start.y_nm) * delta_y
    ) / length_squared
    projection = max(0.0, min(1.0, projection))
    projected_x = start.x_nm + projection * delta_x
    projected_y = start.y_nm + projection * delta_y
    return hypot(point.x_nm - projected_x, point.y_nm - projected_y)


def _orientation(first: PointNm, second: PointNm, third: PointNm) -> int:
    value = (second.y_nm - first.y_nm) * (third.x_nm - second.x_nm) - (second.x_nm - first.x_nm) * (
        third.y_nm - second.y_nm
    )
    return (value > 0) - (value < 0)


def _segments_intersect(
    first_start: PointNm,
    first_end: PointNm,
    second_start: PointNm,
    second_end: PointNm,
) -> bool:
    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if orientations[0] != orientations[1] and orientations[2] != orientations[3]:
        return True
    return (
        (
            orientations[0] == 0
            and _point_segment_distance(second_start, first_start, first_end) == 0
        )
        or (
            orientations[1] == 0
            and _point_segment_distance(second_end, first_start, first_end) == 0
        )
        or (
            orientations[2] == 0
            and _point_segment_distance(first_start, second_start, second_end) == 0
        )
        or (
            orientations[3] == 0
            and _point_segment_distance(first_end, second_start, second_end) == 0
        )
    )


def _segment_distance(first: _CopperItem, second: _CopperItem) -> float:
    if _segments_intersect(first.start, first.end, second.start, second.end):
        return 0.0
    return min(
        _point_segment_distance(first.start, second.start, second.end),
        _point_segment_distance(first.end, second.start, second.end),
        _point_segment_distance(second.start, first.start, first.end),
        _point_segment_distance(second.end, first.start, first.end),
    )


def _point_in_polygon(point: PointNm, polygon: tuple[PointNm, ...]) -> bool:
    inside = False
    for start, end in zip(polygon, polygon[1:], strict=False):
        if _point_segment_distance(point, start, end) == 0:
            return True
        crosses_y = (start.y_nm > point.y_nm) != (end.y_nm > point.y_nm)
        if not crosses_y:
            continue
        crossing_x = start.x_nm + (
            (point.y_nm - start.y_nm) * (end.x_nm - start.x_nm) / (end.y_nm - start.y_nm)
        )
        if point.x_nm < crossing_x:
            inside = not inside
    return inside


def _courtyard_polygon(footprint: BoardFootprint) -> tuple[PointNm, ...]:
    """Return the footprint courtyard as a rotated, closed rectangle."""

    half_width = footprint.courtyard_width_nm / 2
    half_height = footprint.courtyard_height_nm / 2
    angle = radians(footprint.rotation_deg % 360)
    cosine = cos(angle)
    sine = sin(angle)
    corners = tuple(
        PointNm(
            round(footprint.position.x_nm + x_nm * cosine - y_nm * sine),
            round(footprint.position.y_nm + x_nm * sine + y_nm * cosine),
        )
        for x_nm, y_nm in (
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        )
    )
    return (*corners, corners[0])


def _convex_polygons_overlap(
    first: tuple[PointNm, ...],
    second: tuple[PointNm, ...],
) -> bool:
    """Use a strict separating-axis test so touching courtyards remain valid."""

    for polygon in (first, second):
        for start, end in zip(polygon, polygon[1:], strict=False):
            axis_x = -(end.y_nm - start.y_nm)
            axis_y = end.x_nm - start.x_nm
            first_projection = tuple(
                point.x_nm * axis_x + point.y_nm * axis_y for point in first[:-1]
            )
            second_projection = tuple(
                point.x_nm * axis_x + point.y_nm * axis_y for point in second[:-1]
            )
            if max(first_projection) <= min(second_projection) or max(second_projection) <= min(
                first_projection
            ):
                return False
    return True


def _pad_copper_layers(footprint: BoardFootprint, pad: BoardPad) -> frozenset[CopperLayer]:
    if pad.drill_nm:
        return frozenset((CopperLayer.FRONT, CopperLayer.BACK))
    layer = CopperLayer.FRONT if footprint.side is BoardSide.FRONT else CopperLayer.BACK
    return frozenset((layer,))


def _copper_items(board: BoardDocument) -> tuple[_CopperItem, ...]:
    items: list[_CopperItem] = []
    for footprint in board.footprints:
        for pad in footprint.pads:
            position = footprint.pad_position(pad)
            items.append(
                _CopperItem(
                    pad.pad_id,
                    pad.net,
                    position,
                    position,
                    max(pad.width_nm, pad.height_nm) / 2,
                    _pad_copper_layers(footprint, pad),
                )
            )
    items.extend(
        _CopperItem(
            track.track_id,
            track.net,
            track.start,
            track.end,
            track.width_nm / 2,
            frozenset((track.layer,)),
        )
        for track in board.tracks
    )
    items.extend(
        _CopperItem(
            via.via_id,
            via.net,
            via.position,
            via.position,
            via.diameter_nm / 2,
            frozenset((CopperLayer.FRONT, CopperLayer.BACK)),
        )
        for via in board.vias
    )
    return tuple(items)


def _issue_key(issue: DrcIssue) -> tuple[str, str, tuple[str, ...], int, int]:
    location = issue.location or PointNm(0, 0)
    return issue.severity, issue.code, issue.item_ids, location.x_nm, location.y_nm


class _DisjointSet:
    def __init__(self, item_ids: tuple[str, ...]) -> None:
        self._parent = {item_id: item_id for item_id in item_ids}

    def find(self, item_id: str) -> str:
        while self._parent[item_id] != item_id:
            self._parent[item_id] = self._parent[self._parent[item_id]]
            item_id = self._parent[item_id]
        return item_id

    def union(self, first: str, second: str) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self._parent[max(first_root, second_root)] = min(first_root, second_root)


class DrcEngine:
    """Run deterministic two-layer checks without modifying the document."""

    def __init__(self, compiler: SchematicCompiler | None = None) -> None:
        self._compiler = compiler or SchematicCompiler()

    def check(
        self,
        document: EdaProjectDocument,
        revision: int | None = None,
    ) -> DesignCheckReport:
        if revision is not None and revision != document.revision:
            issue = DrcIssue(
                IssueSeverity.ERROR,
                "drc.stale_revision",
                f"Requested revision {revision}, current revision is {document.revision}",
            )
            return DesignCheckReport(document.revision, (issue,), stale=True)
        issues: list[DrcIssue] = []
        self._check_outline(document, issues)
        self._check_dimensions(document, issues)
        items = _copper_items(document.board)
        self._check_clearance(document, items, issues)
        self._check_edge_clearance(document, items, issues)
        self._check_courtyards(document, issues)
        self._check_schematic_parity(document, issues)
        self._check_routing(document, items, issues)
        return DesignCheckReport(
            document.revision,
            tuple(sorted(set(issues), key=_issue_key)),
        )

    @staticmethod
    def _check_outline(document: EdaProjectDocument, issues: list[DrcIssue]) -> None:
        outline = document.board.outline
        if len(outline) < 4 or outline[0] != outline[-1]:
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "drc.outline_open",
                    "Board outline must be a closed polygon",
                )
            )
            return
        segments = tuple(zip(outline, outline[1:], strict=False))
        for index, (start, end) in enumerate(segments):
            if start == end:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.outline_zero_length",
                        "Board outline contains a zero-length edge",
                        location=start,
                    )
                )
            for other_index, (other_start, other_end) in enumerate(
                segments[index + 1 :],
                index + 1,
            ):
                if other_index in {index + 1, len(segments) - 1 if index == 0 else -1}:
                    continue
                if _segments_intersect(start, end, other_start, other_end):
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "drc.outline_self_intersection",
                            "Board outline crosses itself",
                            location=start,
                        )
                    )

    @staticmethod
    def _check_dimensions(document: EdaProjectDocument, issues: list[DrcIssue]) -> None:
        rules = document.rules
        for track in document.board.tracks:
            if track.width_nm < rules.minimum_track_width_nm:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.track_too_narrow",
                        f"Track width is below {rules.minimum_track_width_nm} nm",
                        (track.track_id,),
                        track.start,
                    )
                )
            delta_x = abs(track.end.x_nm - track.start.x_nm)
            delta_y = abs(track.end.y_nm - track.start.y_nm)
            if delta_x and delta_y and delta_x != delta_y:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.track_angle",
                        "Track must use a 45 or 90 degree segment",
                        (track.track_id,),
                        track.start,
                    )
                )
        for via in document.board.vias:
            if via.diameter_nm < rules.minimum_via_diameter_nm:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.via_too_small",
                        "Via diameter is below the active design rule",
                        (via.via_id,),
                        via.position,
                    )
                )
            if via.drill_nm < rules.minimum_via_drill_nm:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.via_drill_too_small",
                        "Via drill is below the active design rule",
                        (via.via_id,),
                        via.position,
                    )
                )

    @staticmethod
    def _check_clearance(
        document: EdaProjectDocument,
        items: tuple[_CopperItem, ...],
        issues: list[DrcIssue],
    ) -> None:
        clearance = document.rules.minimum_clearance_nm
        for index, first in enumerate(items):
            for second in items[index + 1 :]:
                if not first.layers.intersection(second.layers) or first.net == second.net:
                    continue
                separation = _segment_distance(first, second) - first.radius_nm - second.radius_nm
                if separation < clearance:
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "drc.copper_clearance",
                            f"Copper clearance is below {clearance} nm",
                            tuple(sorted((first.item_id, second.item_id))),
                            first.start,
                        )
                    )

    @staticmethod
    def _check_edge_clearance(
        document: EdaProjectDocument,
        items: tuple[_CopperItem, ...],
        issues: list[DrcIssue],
    ) -> None:
        outline = document.board.outline
        if len(outline) < 4 or outline[0] != outline[-1]:
            return
        edge_segments = tuple(zip(outline, outline[1:], strict=False))
        for item in items:
            if not _point_in_polygon(item.start, outline) or not _point_in_polygon(
                item.end,
                outline,
            ):
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.copper_outside_board",
                        "Copper lies outside the board outline",
                        (item.item_id,),
                        item.start,
                    )
                )
                continue
            clearance = (
                min(
                    _segment_distance(
                        item,
                        _CopperItem("edge", "", start, end, 0, item.layers),
                    )
                    for start, end in edge_segments
                )
                - item.radius_nm
            )
            if clearance < document.rules.copper_to_edge_nm:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.copper_to_edge",
                        "Copper is too close to the board edge",
                        (item.item_id,),
                        item.start,
                    )
                )

    @staticmethod
    def _check_courtyards(document: EdaProjectDocument, issues: list[DrcIssue]) -> None:
        footprints = document.board.footprints
        for index, first in enumerate(footprints):
            if not first.courtyard_width_nm or not first.courtyard_height_nm:
                continue
            first_polygon = _courtyard_polygon(first)
            for second in footprints[index + 1 :]:
                if not second.courtyard_width_nm or not second.courtyard_height_nm:
                    continue
                if _convex_polygons_overlap(
                    first_polygon,
                    _courtyard_polygon(second),
                ):
                    issues.append(
                        DrcIssue(
                            IssueSeverity.WARNING,
                            "drc.courtyard_overlap",
                            "Component courtyards overlap",
                            tuple(sorted((first.footprint_id, second.footprint_id))),
                            first.position,
                        )
                    )

    def _check_schematic_parity(
        self,
        document: EdaProjectDocument,
        issues: list[DrcIssue],
    ) -> None:
        graph = self._compiler.compile(document)
        nets = {net.net_id: net.name for net in graph.nets}
        expected = {
            (symbol_id, pin_id): nets[net_id] for symbol_id, pin_id, net_id in graph.pin_to_net
        }
        footprints = {item.symbol_id: item for item in document.board.footprints}
        references: dict[str, list[str]] = {}
        for footprint in document.board.footprints:
            references.setdefault(footprint.reference, []).append(footprint.footprint_id)
        for reference, item_ids in references.items():
            if len(item_ids) > 1:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.duplicate_reference",
                        f"Board reference {reference} is duplicated",
                        tuple(sorted(item_ids)),
                    )
                )
        for symbol in document.schematic.symbols:
            if not symbol.footprint_id:
                continue
            footprint = footprints.get(symbol.symbol_id)
            if footprint is None:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "drc.footprint_missing",
                        f"{symbol.reference} has no PCB footprint",
                        (symbol.symbol_id,),
                    )
                )
                continue
            pads = {pad.pin_number: pad for pad in footprint.pads}
            for pin in symbol.pins:
                pad = pads.get(pin.number)
                if pad is None:
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "drc.pad_missing",
                            f"{symbol.reference} pin {pin.number} has no PCB pad",
                            (symbol.symbol_id, footprint.footprint_id),
                        )
                    )
                    continue
                expected_net = expected.get((symbol.symbol_id, pin.pin_id), "")
                if pad.net != expected_net:
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "drc.pad_net_mismatch",
                            f"{symbol.reference}.{pin.number} should be on {expected_net}",
                            (pad.pad_id,),
                            footprint.pad_position(pad),
                        )
                    )

    @staticmethod
    def _check_routing(
        document: EdaProjectDocument,
        items: tuple[_CopperItem, ...],
        issues: list[DrcIssue],
    ) -> None:
        for track in document.board.tracks:
            for endpoint in (track.start, track.end):
                wrong_items = tuple(
                    item.item_id
                    for item in items
                    if item.item_id != track.track_id
                    and track.layer in item.layers
                    and item.net != track.net
                    and _point_segment_distance(endpoint, item.start, item.end) <= item.radius_nm
                )
                if wrong_items:
                    issues.append(
                        DrcIssue(
                            IssueSeverity.ERROR,
                            "drc.wrong_net_termination",
                            f"Track {track.track_id} terminates on another net",
                            (track.track_id, *sorted(wrong_items)),
                            endpoint,
                        )
                    )
        by_net: dict[str, list[_CopperItem]] = {}
        for item in items:
            if item.net:
                by_net.setdefault(item.net, []).append(item)
        for net, net_items in by_net.items():
            pads = {
                pad.pad_id
                for footprint in document.board.footprints
                for pad in footprint.pads
                if pad.net == net
            }
            if len(pads) < 2:
                continue
            connections = _DisjointSet(tuple(item.item_id for item in net_items))

            for index, first in enumerate(net_items):
                for second in net_items[index + 1 :]:
                    if first.layers.intersection(second.layers) and (
                        _segment_distance(first, second) <= first.radius_nm + second.radius_nm
                    ):
                        connections.union(first.item_id, second.item_id)
            pad_roots = {connections.find(pad_id) for pad_id in pads}
            if len(pad_roots) > 1:
                issues.append(
                    DrcIssue(
                        IssueSeverity.WARNING,
                        "drc.unrouted_net",
                        f"Net {net} still has {len(pad_roots)} disconnected groups",
                        tuple(sorted(pads)),
                    )
                )


@dataclass(frozen=True, slots=True)
class ManualRouteRequest:
    document: EdaProjectDocument
    net: str
    points: tuple[PointNm, ...]
    layer: CopperLayer = CopperLayer.FRONT
    width_nm: int | None = None
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    success: bool
    document: EdaProjectDocument
    tracks: tuple[BoardTrack, ...] = ()
    issues: tuple[DrcIssue, ...] = ()
    committed: bool = False


class ManualRouter:
    """Create 45/90-degree route previews and immutable one-revision commits."""

    def __init__(self, drc: DrcEngine | None = None) -> None:
        self._drc = drc or DrcEngine()

    def preview(self, request: ManualRouteRequest) -> RouteResult:
        issues = self._validate(request)
        if issues:
            return RouteResult(False, request.document, issues=tuple(issues))
        width = request.width_nm or self._width_for_net(request.document, request.net)
        tracks = tuple(
            BoardTrack(
                _stable_id(
                    request.document.project_id,
                    request.document.revision,
                    request.net,
                    request.layer,
                    index,
                    start.x_nm,
                    start.y_nm,
                    end.x_nm,
                    end.y_nm,
                ),
                request.net,
                start,
                end,
                width,
                request.layer,
            )
            for index, (start, end) in enumerate(
                zip(request.points, request.points[1:], strict=False)
            )
        )
        proposed_board = replace(
            request.document.board,
            tracks=(*request.document.board.tracks, *tracks),
        )
        proposed = replace(request.document, board=proposed_board)
        report = self._drc.check(proposed)
        track_ids = {track.track_id for track in tracks}
        route_issues = tuple(
            issue
            for issue in report.issues
            if track_ids.intersection(issue.item_ids) and issue.severity is IssueSeverity.ERROR
        )
        return RouteResult(not route_issues, proposed, tracks, route_issues)

    def commit(self, request: ManualRouteRequest) -> RouteResult:
        preview = self.preview(request)
        if not preview.success:
            return preview
        committed = request.document.revised(board=preview.document.board)
        return RouteResult(True, committed, preview.tracks, preview.issues, committed=True)

    @staticmethod
    def _validate(request: ManualRouteRequest) -> list[DrcIssue]:
        issues: list[DrcIssue] = []
        if (
            request.expected_revision is not None
            and request.expected_revision != request.document.revision
        ):
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "router.stale_revision",
                    "The design changed before this route could be applied",
                )
            )
        if len(request.points) < 2:
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "router.too_few_points",
                    "A route needs at least two points",
                )
            )
        if not request.net:
            issues.append(
                DrcIssue(IssueSeverity.ERROR, "router.net_missing", "Select a net before routing")
            )
        known_nets = {
            pad.net
            for footprint in request.document.board.footprints
            for pad in footprint.pads
            if pad.net
        }
        if request.net and request.net not in known_nets:
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "router.net_unknown",
                    f"Net {request.net} is not present on the board",
                )
            )
        if request.layer not in request.document.board.copper_layers:
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "router.layer_unavailable",
                    f"Layer {request.layer} is not in the board stackup",
                )
            )
        width = request.width_nm
        if width is not None and width < request.document.rules.minimum_track_width_nm:
            issues.append(
                DrcIssue(
                    IssueSeverity.ERROR,
                    "router.width_too_small",
                    "Route width is below the active design rule",
                )
            )
        for start, end in zip(request.points, request.points[1:], strict=False):
            delta_x = abs(end.x_nm - start.x_nm)
            delta_y = abs(end.y_nm - start.y_nm)
            if start == end:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "router.zero_length",
                        "A route cannot contain a zero-length segment",
                        location=start,
                    )
                )
            elif delta_x and delta_y and delta_x != delta_y:
                issues.append(
                    DrcIssue(
                        IssueSeverity.ERROR,
                        "router.invalid_angle",
                        "Manual routes must use 45 or 90 degree segments",
                        location=start,
                    )
                )
        return issues

    @staticmethod
    def _width_for_net(document: EdaProjectDocument, net: str) -> int:
        default_class = document.board.net_classes[0] if document.board.net_classes else NetClass()
        selected = next(
            (net_class for net_class in document.board.net_classes if net in net_class.nets),
            default_class,
        )
        return selected.track_width_nm


@dataclass(frozen=True, slots=True)
class AutorouteRequest:
    document: EdaProjectDocument
    nets: tuple[str, ...] = ()
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RoutingProposal:
    success: bool
    revision: int
    tracks: tuple[BoardTrack, ...] = ()
    remaining_nets: tuple[str, ...] = ()
    issues: tuple[DrcIssue, ...] = ()


@runtime_checkable
class Autorouter(Protocol):
    """Contract implemented by the isolated Rust router worker."""

    def route(self, request: AutorouteRequest) -> RoutingProposal: ...
