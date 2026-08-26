"""Transactional KiCad 10 export, validation, and fabrication packaging.

The bridge writes only application-owned documents.  Existing imported KiCad
projects are never accepted as export destinations.  KiCad runs against a
temporary staging tree before any files are committed to the user-selected
empty directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid5

from ..engines.process import ProcessResult, run_isolated_process
from ..tooling import discover_tools
from .model import (
    BoardFootprint,
    BoardPad,
    BoardSide,
    EdaProjectDocument,
    IssueSeverity,
    PointNm,
    SchematicPin,
    SchematicSymbol,
)
from .pcb import DrcEngine

KICAD_ADAPTER_VERSION = "10.0.5"
_UUID_NAMESPACE = UUID("1a849723-2bd7-4b43-96d5-3897ceec23fa")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_INTERNAL_SCHEMATIC_GRID_NM = 1_000_000
_KICAD_SCHEMATIC_GRID_NM = 1_270_000


class BridgeStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class BridgeDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedKiCadProject:
    project_name: str
    directory: str
    project_file: str
    schematic_file: str
    board_file: str
    expected_references: tuple[str, ...] = ()
    expected_net_labels: tuple[str, ...] = ()
    design_hash: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    status: BridgeStatus
    kicad_version: str | None
    erc_violation_count: int
    drc_violation_count: int
    unconnected_count: int
    diagnostics: tuple[BridgeDiagnostic, ...] = ()
    erc_report_path: str | None = None
    drc_report_path: str | None = None
    semantic_match: bool = True

    @property
    def clean(self) -> bool:
        return (
            self.status is BridgeStatus.AVAILABLE
            and self.erc_violation_count == 0
            and self.drc_violation_count == 0
            and self.unconnected_count == 0
            and self.semantic_match
        )


@dataclass(frozen=True, slots=True)
class ExportReport:
    success: bool
    status: BridgeStatus
    destination: str | None
    generated: GeneratedKiCadProject | None
    validation: ValidationReport | None
    diagnostics: tuple[BridgeDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class FabricationManifest:
    schema_version: int
    project_name: str
    kicad_version: str
    design_hash: str
    files: tuple[tuple[str, str], ...]
    output_directory: str


@dataclass(frozen=True, slots=True)
class MergePlan:
    supported: bool
    baseline_hash: str | None
    operations: tuple[dict[str, object], ...] = ()
    conflicts: tuple[dict[str, object], ...] = ()
    diagnostics: tuple[BridgeDiagnostic, ...] = ()


def _safe_project_name(name: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", name.strip()).strip("._")
    return safe[:80] or "smd_eda_project"


def _uuid(value: str, suffix: str = "") -> str:
    try:
        return str(UUID(value)) if not suffix else str(uuid5(_UUID_NAMESPACE, value + suffix))
    except ValueError:
        return str(uuid5(_UUID_NAMESPACE, value + suffix))


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mm(value_nm: int) -> str:
    value = value_nm / 1_000_000
    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    return rendered if rendered not in {"", "-0"} else "0"


def _sheet_point(point: PointNm) -> tuple[str, str]:
    # The editor's 1 mm logical grid maps to KiCad's 50 mil connection grid.
    # Schematic drawing coordinates have no physical scale.
    x_step = round(point.x_nm / _INTERNAL_SCHEMATIC_GRID_NM)
    y_step = round(point.y_nm / _INTERNAL_SCHEMATIC_GRID_NM)
    return _mm(x_step * _KICAD_SCHEMATIC_GRID_NM), _mm(-y_step * _KICAD_SCHEMATIC_GRID_NM)


def _symbol_point(point: PointNm) -> tuple[str, str]:
    x_step = round(point.x_nm / _INTERNAL_SCHEMATIC_GRID_NM)
    y_step = round(point.y_nm / _INTERNAL_SCHEMATIC_GRID_NM)
    return _mm(x_step * _KICAD_SCHEMATIC_GRID_NM), _mm(y_step * _KICAD_SCHEMATIC_GRID_NM)


def _symbol_library_name(symbol: SchematicSymbol) -> str:
    stem = _SAFE_NAME_RE.sub("_", symbol.library_id or symbol.kind or "Part")
    return f"SMD_Twin_{stem}"


def _owned_footprint_name(library_id: str) -> str:
    stem = _SAFE_NAME_RE.sub("_", library_id or "Generated")
    return f"SMD_Twin_{stem}"


def _board_net_name(name: str) -> str:
    return name if name.startswith("/") else f"/{name}"


def _pin_angle(pin: SchematicPin) -> int:
    x = pin.offset.x_nm
    y = -pin.offset.y_nm
    if abs(x) >= abs(y):
        return 0 if x < 0 else 180
    return 270 if y < 0 else 90


def _lib_symbol(symbol: SchematicSymbol) -> str:
    lib_name = _symbol_library_name(symbol)
    base_name = lib_name
    reference_prefix = re.match(r"[A-Za-z#]+", symbol.reference)
    reference = reference_prefix.group(0) if reference_prefix else "U"
    pin_forms: list[str] = []
    for pin in symbol.pins:
        x, y = _symbol_point(pin.offset)
        pin_type = {
            "input": "input",
            "output": "output",
            "bidirectional": "bidirectional",
            "power_input": "power_in",
            "power_output": "power_out",
            "no_connect": "no_connect",
        }.get(pin.electrical_type.value, "passive")
        pin_forms.append(
            f"""\t\t\t(pin {pin_type} line
\t\t\t\t(at {x} {y} {_pin_angle(pin)})
\t\t\t\t(length 1.27)
\t\t\t\t(name {_quote(pin.name or "~")} (effects (font (size 1.27 1.27))))
\t\t\t\t(number {_quote(pin.number)} (effects (font (size 1.27 1.27))))
\t\t\t)"""
        )
    body = (
        "\t\t\t(circle (center 0 0) (radius 1.8) "
        "(stroke (width 0.254) (type default)) (fill (type none)))"
        if symbol.kind.casefold() == "voltage_source"
        else "\t\t\t(rectangle (start -1.4 -2.2) (end 1.4 2.2) "
        "(stroke (width 0.254) (type default)) (fill (type none)))"
    )
    return f"""\t\t(symbol {_quote(lib_name)}
\t\t\t(pin_numbers (hide yes))
\t\t\t(pin_names (offset 0))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" {_quote(reference)} (at 2.54 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(property "Value" {_quote(base_name)} (at 0 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(property "Footprint" "" (at -2.54 0 90)
\t\t\t\t(effects (font (size 1.27 1.27)) (hide yes)))
\t\t\t(property "Datasheet" "~" (at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)) (hide yes)))
\t\t\t(property "Description" "SMD Twin Lab generated symbol" (at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)) (hide yes)))
\t\t\t(symbol {_quote(base_name + "_0_1")}
{body}
\t\t\t)
\t\t\t(symbol {_quote(base_name + "_1_1")}
{"\n".join(pin_forms)}
\t\t\t)
\t\t\t(embedded_fonts no)
\t\t)"""


def _symbol_instance(symbol: SchematicSymbol, root_uuid: str, project_name: str) -> str:
    x, y = _sheet_point(symbol.position)
    properties = (
        ("Reference", symbol.reference, False),
        ("Value", symbol.value, False),
        ("Footprint", _owned_footprint_name(symbol.footprint_id), True),
        ("Datasheet", "~", True),
        ("Description", "", True),
    )
    property_forms = []
    for name, value, hidden in properties:
        hide = " (hide yes)" if hidden else ""
        property_forms.append(
            f"""\t\t(property {_quote(name)} {_quote(value)}
\t\t\t(at {x} {y} 0)
\t\t\t(effects (font (size 1.27 1.27)){hide})
\t\t)"""
        )
    pins = "\n".join(
        f"\t\t(pin {_quote(pin.number)} (uuid {_quote(_uuid(pin.pin_id, symbol.symbol_id))}))"
        for pin in symbol.pins
    )
    return f"""\t(symbol
\t\t(lib_id {_quote(_symbol_library_name(symbol))})
\t\t(at {x} {y} {symbol.rotation_deg % 360})
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid {_quote(_uuid(symbol.symbol_id))})
{"\n".join(property_forms)}
{pins}
\t\t(instances
\t\t\t(project {_quote(project_name)}
\t\t\t\t(path {_quote("/" + root_uuid)}
\t\t\t\t\t(reference {_quote(symbol.reference)})
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""


def render_schematic(document: EdaProjectDocument, project_name: str) -> str:
    root_uuid = _uuid(document.project_id, "root-sheet")
    definitions: dict[str, SchematicSymbol] = {}
    for symbol in document.schematic.symbols:
        definitions.setdefault(_symbol_library_name(symbol), symbol)

    junctions = []
    for junction in document.schematic.junctions:
        x, y = _sheet_point(junction.position)
        junctions.append(
            f"""\t(junction (at {x} {y}) (diameter 0) (color 0 0 0 0)
\t\t(uuid {_quote(_uuid(junction.junction_id))}))"""
        )
    wires = []
    for wire in document.schematic.wires:
        # KiCad schematic wires are single straight segments. The editable
        # model stores an entire gesture as a polyline so undo remains atomic.
        for index, (start, end) in enumerate(zip(wire.points, wire.points[1:], strict=False)):
            points = " ".join(f"(xy {x} {y})" for x, y in map(_sheet_point, (start, end)))
            wires.append(
                f"""\t(wire (pts {points})
\t\t(stroke (width 0) (type default))
\t\t(uuid {_quote(_uuid(wire.wire_id, f"segment-{index}"))}))"""
            )
    labels = []
    for label in document.schematic.labels:
        x, y = _sheet_point(label.position)
        labels.append(
            f"""\t(label {_quote(label.text)} (at {x} {y} 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid {_quote(_uuid(label.label_id))}))"""
        )
    instances = [
        _symbol_instance(symbol, root_uuid, project_name) for symbol in document.schematic.symbols
    ]
    return f"""(kicad_sch
\t(version 20250114)
\t(generator "smd_twin_lab")
\t(generator_version "0.1")
\t(uuid {_quote(root_uuid)})
\t(paper "A4")
\t(title_block (title {_quote(document.name)}) (company "SMD Twin Lab"))
\t(lib_symbols
{"\n".join(_lib_symbol(symbol) for symbol in definitions.values())}
\t)
{"\n".join(junctions)}
{"\n".join(wires)}
{"\n".join(labels)}
{"\n".join(instances)}
\t(sheet_instances (path "/" (page "1")))
\t(embedded_fonts no)
)
"""


@dataclass(frozen=True, slots=True)
class _BoardTransform:
    min_x_nm: int
    max_y_nm: int
    margin_mm: float = 20.0

    def point(self, value: PointNm) -> tuple[str, str]:
        x = self.margin_mm + (value.x_nm - self.min_x_nm) / 1_000_000
        y = self.margin_mm + (self.max_y_nm - value.y_nm) / 1_000_000
        return _format_number(x), _format_number(y)

    @staticmethod
    def local(value: PointNm) -> tuple[str, str]:
        return _mm(value.x_nm), _mm(-value.y_nm)


def _format_number(value: float) -> str:
    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    return rendered if rendered not in {"", "-0"} else "0"


def _board_transform(document: EdaProjectDocument) -> _BoardTransform:
    points = list(document.board.outline)
    points.extend(footprint.position for footprint in document.board.footprints)
    if not points:
        return _BoardTransform(0, 0)
    return _BoardTransform(min(point.x_nm for point in points), max(point.y_nm for point in points))


def _footprint_form(
    footprint: BoardFootprint,
    symbol_by_id: dict[str, SchematicSymbol],
    net_ids: dict[str, int],
    transform: _BoardTransform,
    root_uuid: str,
) -> str:
    x, y = transform.point(footprint.position)
    layer = "F.Cu" if footprint.side is BoardSide.FRONT else "B.Cu"
    symbol = symbol_by_id.get(footprint.symbol_id)
    value = symbol.value if symbol is not None else ""
    library_id = _owned_footprint_name(footprint.library_id)
    path = f"/{root_uuid}/{_uuid(footprint.symbol_id)}" if footprint.symbol_id else ""
    width = max(footprint.courtyard_width_nm, 2_000_000) / 2_000_000
    height = max(footprint.courtyard_height_nm, 1_200_000) / 2_000_000
    pad_forms = [_pad_form(pad, footprint, net_ids) for pad in footprint.pads]
    path_form = f"\n\t\t(path {_quote(path)})" if path else ""
    fab_layer = "F.Fab" if footprint.side is BoardSide.FRONT else "B.Fab"
    return f"""\t(footprint {_quote(library_id)}
\t\t(layer {_quote(layer)})
\t\t(uuid {_quote(_uuid(footprint.footprint_id))})
\t\t(at {x} {y} {footprint.rotation_deg % 360})
\t\t(property "Reference" {_quote(footprint.reference)}
\t\t\t(at 0 {_format_number(-height - 1)} 0) (layer {_quote(fab_layer)})
\t\t\t(uuid {_quote(_uuid(footprint.footprint_id, "reference"))})
\t\t\t(effects (font (size 1 1) (thickness 0.15))))
\t\t(property "Value" {_quote(value)}
\t\t\t(at 0 {_format_number(height + 1)} 0) (layer "F.Fab")
\t\t\t(uuid {_quote(_uuid(footprint.footprint_id, "value"))})
\t\t\t(effects (font (size 1 1) (thickness 0.15)))){path_form}
\t\t(sheetname "/")
\t\t(sheetfile {_quote("")})
\t\t(attr smd)
\t\t(fp_rect (start {_format_number(-width)} {_format_number(-height)})
\t\t\t(end {_format_number(width)} {_format_number(height)})
\t\t\t(stroke (width 0.15) (type solid)) (fill no)
\t\t\t(layer {_quote(fab_layer)}) (uuid {_quote(_uuid(footprint.footprint_id, "body"))}))
{"\n".join(pad_forms)}
\t\t(embedded_fonts no)
\t)"""


def _pad_form(pad: BoardPad, footprint: BoardFootprint, net_ids: dict[str, int]) -> str:
    x, y = _BoardTransform.local(pad.offset)
    layers = (
        '"*.Cu" "*.Mask"'
        if pad.drill_nm
        else (
            '"F.Cu" "F.Paste" "F.Mask"'
            if footprint.side is BoardSide.FRONT
            else '"B.Cu" "B.Paste" "B.Mask"'
        )
    )
    pad_type = "thru_hole" if pad.drill_nm else "smd"
    shape = pad.shape if pad.shape in {"circle", "oval", "rect", "roundrect"} else "rect"
    drill = f"\n\t\t\t(drill {_mm(pad.drill_nm)})" if pad.drill_nm else ""
    net = f"\n\t\t\t(net {net_ids[pad.net]} {_quote(_board_net_name(pad.net))})" if pad.net else ""
    return f"""\t\t(pad {_quote(pad.pin_number)} {pad_type} {shape}
\t\t\t(at {x} {y})
\t\t\t(size {_mm(pad.width_nm)} {_mm(pad.height_nm)}){drill}
\t\t\t(layers {layers}){net}
\t\t\t(pinfunction {_quote(pad.pin_number)})
\t\t\t(pintype "passive")
\t\t\t(uuid {_quote(_uuid(pad.pad_id))})
\t\t)"""


def render_board(document: EdaProjectDocument) -> str:
    transform = _board_transform(document)
    root_uuid = _uuid(document.project_id, "root-sheet")
    net_names = sorted(
        {item.net for footprint in document.board.footprints for item in footprint.pads if item.net}
        | {track.net for track in document.board.tracks if track.net}
        | {via.net for via in document.board.vias if via.net}
    )
    net_ids = {name: index for index, name in enumerate(net_names, start=1)}
    symbol_by_id = {symbol.symbol_id: symbol for symbol in document.schematic.symbols}
    footprints = [
        _footprint_form(footprint, symbol_by_id, net_ids, transform, root_uuid)
        for footprint in document.board.footprints
    ]
    outline = list(document.board.outline)
    outline_forms: list[str] = []
    if len(outline) >= 3:
        if outline[0] != outline[-1]:
            outline.append(outline[0])
        for index, (start, end) in enumerate(zip(outline, outline[1:], strict=False)):
            sx, sy = transform.point(start)
            ex, ey = transform.point(end)
            outline_forms.append(
                f"""\t(gr_line (start {sx} {sy}) (end {ex} {ey})
\t\t(stroke (width 0.1) (type solid)) (layer "Edge.Cuts")
\t\t(uuid {_quote(_uuid(document.project_id, f"outline-{index}"))}))"""
            )
    track_forms = []
    for track in document.board.tracks:
        sx, sy = transform.point(track.start)
        ex, ey = transform.point(track.end)
        track_forms.append(
            f"""\t(segment (start {sx} {sy}) (end {ex} {ey})
\t\t(width {_mm(track.width_nm)}) (layer {_quote(track.layer.value)})
\t\t(net {net_ids[track.net] if track.net else 0}) (uuid {_quote(_uuid(track.track_id))}))"""
        )
    via_forms = []
    for via in document.board.vias:
        x, y = transform.point(via.position)
        via_forms.append(
            f"""\t(via (at {x} {y}) (size {_mm(via.diameter_nm)})
\t\t(drill {_mm(via.drill_nm)}) (layers "F.Cu" "B.Cu")
\t\t(net {net_ids[via.net] if via.net else 0}) (uuid {_quote(_uuid(via.via_id))}))"""
        )
    net_forms = "\n".join(
        f"\t(net {identifier} {_quote(_board_net_name(name))})"
        for name, identifier in net_ids.items()
    )
    return f"""(kicad_pcb
\t(version 20241229)
\t(generator "smd_twin_lab")
\t(generator_version "0.1")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(21 "Eco1.User" user "User.Eco1")
\t\t(23 "Eco2.User" user "User.Eco2")
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
{net_forms}
{"\n".join(footprints)}
{"\n".join(outline_forms)}
{"\n".join(track_forms)}
{"\n".join(via_forms)}
\t(embedded_fonts no)
)
"""


def _project_json(project_name: str) -> str:
    payload = {
        "board": {},
        "boards": [],
        "cvpcb": {},
        "erc": {
            "rule_severities": {
                # Generated primitives and footprints are embedded in the files.
                # They intentionally have no mutable external-library link.
                "footprint_link_issues": "ignore",
                "lib_symbol_issues": "ignore",
            }
        },
        "libraries": {},
        "meta": {"filename": f"{project_name}.kicad_pro", "version": 1},
        "net_settings": {},
        "pcbnew": {},
        "schematic": {},
        "text_variables": {},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _document_hash(document: EdaProjectDocument) -> str:
    encoded = json.dumps(
        asdict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _count_report_violations(payload: object) -> tuple[int, int]:
    """Count KiCad issues in both flat DRC and per-sheet ERC reports."""

    violation_count = 0
    unconnected_count = 0

    def visit(value: object) -> None:
        nonlocal violation_count, unconnected_count
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"violations", "schematic_parity"} and isinstance(child, list):
                    violation_count += len(child)
                    continue
                if key == "unconnected_items" and isinstance(child, list):
                    unconnected_count += len(child)
                    continue
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return violation_count, unconnected_count


def _semantic_netlist_summary(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = ElementTree.parse(path).getroot()
    references = tuple(
        sorted(
            element.attrib["ref"]
            for element in root.findall("./components/comp")
            if element.attrib.get("ref")
        )
    )
    nets = tuple(
        sorted(
            element.attrib["name"]
            for element in root.findall("./nets/net")
            if element.attrib.get("name")
        )
    )
    return references, nets


def _process_diagnostic(result: ProcessResult, code: str, path: Path) -> BridgeDiagnostic:
    if result.timed_out:
        return BridgeDiagnostic("error", f"{code}_TIMEOUT", "KiCad command timed out.", str(path))
    detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
    return BridgeDiagnostic("error", code, detail, str(path))


class KiCad10Bridge:
    """One-way transactional writer now; safe three-way sync is explicitly gated."""

    def __init__(self, kicad_cli: Path | None = None, *, timeout_s: float = 120.0) -> None:
        self.kicad_cli = kicad_cli if kicad_cli is not None else discover_tools().kicad_cli
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return self.kicad_cli is not None and self.kicad_cli.is_file()

    def generate(self, document: EdaProjectDocument, directory: Path) -> GeneratedKiCadProject:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        project_name = _safe_project_name(document.name)
        project_file = directory / f"{project_name}.kicad_pro"
        schematic_file = directory / f"{project_name}.kicad_sch"
        board_file = directory / f"{project_name}.kicad_pcb"
        project_file.write_text(_project_json(project_name), encoding="utf-8", newline="\n")
        schematic_file.write_text(
            render_schematic(document, project_name), encoding="utf-8", newline="\n"
        )
        board_file.write_text(render_board(document), encoding="utf-8", newline="\n")
        return GeneratedKiCadProject(
            project_name,
            str(directory.resolve()),
            str(project_file.resolve()),
            str(schematic_file.resolve()),
            str(board_file.resolve()),
            tuple(sorted(symbol.reference for symbol in document.schematic.symbols)),
            tuple(sorted({_board_net_name(label.text) for label in document.schematic.labels})),
            _document_hash(document),
        )

    def validate(self, generated: GeneratedKiCadProject) -> ValidationReport:
        if not self.available:
            return ValidationReport(
                BridgeStatus.UNAVAILABLE,
                None,
                0,
                0,
                0,
                (BridgeDiagnostic("error", "KICAD_UNAVAILABLE", "KiCad 10.0.5 was not found."),),
            )
        directory = Path(generated.directory)
        schematic = Path(generated.schematic_file)
        board = Path(generated.board_file)
        diagnostics: list[BridgeDiagnostic] = []
        version_result = run_isolated_process(
            (self.kicad_cli, "version"), timeout_s=min(self.timeout_s, 10.0)
        )
        version = version_result.stdout.strip() if version_result.returncode == 0 else None
        if version is None or not version.startswith(KICAD_ADAPTER_VERSION):
            diagnostics.append(
                BridgeDiagnostic(
                    "error",
                    "KICAD_VERSION_UNSUPPORTED",
                    f"Expected KiCad {KICAD_ADAPTER_VERSION}, detected {version or 'unknown'}.",
                )
            )
            return ValidationReport(BridgeStatus.UNSUPPORTED, version, 0, 0, 0, tuple(diagnostics))

        for command, source in (("sch", schematic), ("pcb", board)):
            result = run_isolated_process(
                (self.kicad_cli, command, "upgrade", "--force", source),
                cwd=directory,
                timeout_s=self.timeout_s,
            )
            if result.returncode != 0:
                diagnostics.append(
                    _process_diagnostic(result, f"KICAD_{command.upper()}_PARSE", source)
                )
        if diagnostics:
            return ValidationReport(BridgeStatus.INVALID, version, 0, 0, 0, tuple(diagnostics))

        erc_path = directory / "erc.json"
        drc_path = directory / "drc.json"
        erc = run_isolated_process(
            (
                self.kicad_cli,
                "sch",
                "erc",
                "--format",
                "json",
                "--output",
                erc_path,
                schematic,
            ),
            cwd=directory,
            timeout_s=self.timeout_s,
        )
        if erc.returncode not in {0, 5} or not erc_path.is_file():
            diagnostics.append(_process_diagnostic(erc, "KICAD_ERC_FAILED", schematic))
        drc = run_isolated_process(
            (
                self.kicad_cli,
                "pcb",
                "drc",
                "--schematic-parity",
                "--format",
                "json",
                "--exit-code-violations",
                "--output",
                drc_path,
                board,
            ),
            cwd=directory,
            timeout_s=self.timeout_s,
        )
        if drc.returncode not in {0, 5} or not drc_path.is_file():
            diagnostics.append(_process_diagnostic(drc, "KICAD_DRC_FAILED", board))
        semantic_match = True
        if generated.expected_references or generated.expected_net_labels:
            semantic_path = directory / "semantic-netlist.xml"
            semantic = run_isolated_process(
                (
                    self.kicad_cli,
                    "sch",
                    "export",
                    "netlist",
                    "--format",
                    "kicadxml",
                    "--output",
                    semantic_path,
                    schematic,
                ),
                cwd=directory,
                timeout_s=self.timeout_s,
            )
            if semantic.returncode != 0 or not semantic_path.is_file():
                semantic_match = False
                diagnostics.append(
                    _process_diagnostic(semantic, "KICAD_SEMANTIC_EXPORT_FAILED", schematic)
                )
            else:
                try:
                    references, nets = _semantic_netlist_summary(semantic_path)
                    missing_labels = sorted(set(generated.expected_net_labels) - set(nets))
                    if references != generated.expected_references or missing_labels:
                        semantic_match = False
                        diagnostics.append(
                            BridgeDiagnostic(
                                "error",
                                "KICAD_SEMANTIC_MISMATCH",
                                "Canonicalized KiCad output changed references or named nets: "
                                f"references={references!r}, missing_labels={missing_labels!r}.",
                                str(semantic_path),
                            )
                        )
                except (OSError, ElementTree.ParseError) as error:
                    semantic_match = False
                    diagnostics.append(
                        BridgeDiagnostic(
                            "error",
                            "KICAD_SEMANTIC_XML",
                            str(error),
                            str(semantic_path),
                        )
                    )
        erc_count = drc_count = unconnected = 0
        if erc_path.is_file():
            try:
                erc_count, _ = _count_report_violations(
                    json.loads(erc_path.read_text(encoding="utf-8-sig"))
                )
            except (OSError, json.JSONDecodeError) as error:
                diagnostics.append(
                    BridgeDiagnostic("error", "KICAD_ERC_JSON", str(error), str(erc_path))
                )
        if drc_path.is_file():
            try:
                drc_count, unconnected = _count_report_violations(
                    json.loads(drc_path.read_text(encoding="utf-8-sig"))
                )
            except (OSError, json.JSONDecodeError) as error:
                diagnostics.append(
                    BridgeDiagnostic("error", "KICAD_DRC_JSON", str(error), str(drc_path))
                )
        return ValidationReport(
            BridgeStatus.INVALID if diagnostics else BridgeStatus.AVAILABLE,
            version,
            erc_count,
            drc_count,
            unconnected,
            tuple(diagnostics),
            str(erc_path) if erc_path.is_file() else None,
            str(drc_path) if drc_path.is_file() else None,
            semantic_match,
        )

    def export_new(
        self,
        document: EdaProjectDocument,
        empty_destination: Path,
        *,
        require_clean: bool = True,
    ) -> ExportReport:
        destination = Path(empty_destination).resolve()
        if destination.exists() and any(destination.iterdir()):
            diagnostic = BridgeDiagnostic(
                "error",
                "EXPORT_DESTINATION_NOT_EMPTY",
                "Select a new or empty directory; existing KiCad projects are never overwritten.",
                str(destination),
            )
            return ExportReport(False, BridgeStatus.INVALID, None, None, None, (diagnostic,))
        internal_report = DrcEngine().check(document, document.revision)
        internal_errors = tuple(
            issue for issue in internal_report.issues if issue.severity is IssueSeverity.ERROR
        )
        blocking_issues = internal_report.issues if require_clean else internal_errors
        if blocking_issues:
            codes = ", ".join(sorted({issue.code for issue in blocking_issues})[:8])
            diagnostic = BridgeDiagnostic(
                "error",
                "INTERNAL_DRC_BLOCKED",
                f"Owned design checks must pass before KiCad export ({codes}).",
                str(destination),
            )
            return ExportReport(False, BridgeStatus.INVALID, None, None, None, (diagnostic,))
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="smd-eda-kicad-", dir=parent) as temporary:
            generated = self.generate(document, Path(temporary))
            validation = self.validate(generated)
            if validation.status not in {BridgeStatus.AVAILABLE} or (
                require_clean and not validation.clean
            ):
                diagnostic = BridgeDiagnostic(
                    "error",
                    "EXPORT_VALIDATION_BLOCKED",
                    "KiCad validation did not pass; no destination files were committed.",
                    str(destination),
                )
                return ExportReport(
                    False,
                    validation.status,
                    None,
                    None,
                    replace(validation, erc_report_path=None, drc_report_path=None),
                    (*validation.diagnostics, diagnostic),
                )
            if destination.exists():
                destination.rmdir()
            shutil.copytree(temporary, destination)
        committed = GeneratedKiCadProject(
            generated.project_name,
            str(destination),
            str(destination / Path(generated.project_file).name),
            str(destination / Path(generated.schematic_file).name),
            str(destination / Path(generated.board_file).name),
            generated.expected_references,
            generated.expected_net_labels,
            generated.design_hash,
        )
        committed_validation = replace(
            validation,
            erc_report_path=str(destination / "erc.json") if validation.erc_report_path else None,
            drc_report_path=str(destination / "drc.json") if validation.drc_report_path else None,
        )
        return ExportReport(
            True,
            BridgeStatus.AVAILABLE,
            str(destination),
            committed,
            committed_validation,
        )

    def synchronize(self, document: EdaProjectDocument, managed_project: Path) -> MergePlan:
        del managed_project
        diagnostic = BridgeDiagnostic(
            "warning",
            "ROUND_TRIP_NOT_MATURE",
            "Editable three-way KiCad synchronization is an Advanced-mode milestone. "
            "The project can be imported read-only without changing the EDA document.",
        )
        return MergePlan(False, document.kicad_bridge.baseline_hash, diagnostics=(diagnostic,))


class FabricationPackager:
    """Generate a manufacturing package only from a clean validated project."""

    def __init__(self, bridge: KiCad10Bridge) -> None:
        self.bridge = bridge

    def build(
        self,
        generated: GeneratedKiCadProject,
        validation: ValidationReport,
        output_directory: Path,
    ) -> FabricationManifest:
        if not validation.clean:
            raise ValueError("fabrication output requires a clean KiCad validation report")
        if not self.bridge.available or self.bridge.kicad_cli is None:
            raise FileNotFoundError("KiCad 10.0.5 is required for fabrication output")
        output_directory = Path(output_directory).resolve()
        if output_directory.exists() and any(output_directory.iterdir()):
            raise FileExistsError("fabrication output directory must be empty")
        output_directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output_directory.name}.fabrication-",
            dir=output_directory.parent,
        ) as temporary:
            staging = Path(temporary)
            manifest = self._build_staged(
                generated,
                validation,
                staging,
                output_directory,
            )
            if output_directory.exists():
                output_directory.rmdir()
            staging.replace(output_directory)
        return manifest

    def _build_staged(
        self,
        generated: GeneratedKiCadProject,
        validation: ValidationReport,
        staging: Path,
        final_directory: Path,
    ) -> FabricationManifest:
        board = Path(generated.board_file)
        schematic = Path(generated.schematic_file)
        commands = (
            (
                "pcb",
                "export",
                "gerbers",
                "--layers",
                "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts",
                "--output",
                staging,
                board,
            ),
            ("pcb", "export", "drill", "--output", staging, board),
            (
                "pcb",
                "export",
                "pos",
                "--format",
                "csv",
                "--output",
                staging / "placements.csv",
                board,
            ),
            (
                "sch",
                "export",
                "bom",
                "--output",
                staging / "bom.csv",
                schematic,
            ),
        )
        for command in commands:
            result = run_isolated_process(
                (self.bridge.kicad_cli, *command),
                cwd=Path(generated.directory),
                timeout_s=self.bridge.timeout_s,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"KiCad fabrication export failed: {detail}")
        files = tuple(
            sorted(
                (
                    str(path.relative_to(staging)).replace("\\", "/"),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in staging.rglob("*")
                if path.is_file() and path.name != "manifest.json"
            )
        )
        if not files:
            raise RuntimeError("KiCad reported success but produced no fabrication files")
        design_hash = (
            generated.design_hash
            or hashlib.sha256(
                Path(generated.schematic_file).read_bytes()
                + Path(generated.board_file).read_bytes()
            ).hexdigest()
        )
        manifest = FabricationManifest(
            1,
            generated.project_name,
            validation.kicad_version or KICAD_ADAPTER_VERSION,
            design_hash,
            files,
            str(final_directory),
        )
        (staging / "manifest.json").write_text(
            json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return manifest
