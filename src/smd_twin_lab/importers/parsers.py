"""Parsers for the stable, exported KiCad interchange files.

The importer deliberately does not parse or rewrite KiCad's native schematic or
board S-expressions.  Keeping that boundary makes import read-only and keeps the
normalized model independent from KiCad's internal file schema.
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smd_twin_lab.models import ComponentSide, Net, PinRef


class ArtifactParseError(ValueError):
    """An exported artifact exists but cannot be normalized safely."""


@dataclass(slots=True)
class BomPart:
    reference: str
    value: str = ""
    footprint: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Placement:
    reference: str
    value: str = ""
    footprint: str = ""
    x_mm: float | None = None
    y_mm: float | None = None
    rotation_deg: float = 0.0
    side: ComponentSide = ComponentSide.UNKNOWN
    is_smd: bool = False


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().replace("${", "").replace("}", ""))


def _column(row: dict[str, str], *names: str) -> str:
    normalized = {_key(str(name)): value for name, value in row.items() if name is not None}
    for name in names:
        if _key(name) in normalized:
            return (normalized[_key(name)] or "").strip()
    return ""


def _expand_reference_token(token: str) -> list[str]:
    token = token.strip()
    match = re.fullmatch(r"([A-Za-z]+)(\d+)-([A-Za-z]*)(\d+)", token)
    if not match:
        return [token] if token else []
    first_prefix, first_number, second_prefix, second_number = match.groups()
    second_prefix = second_prefix or first_prefix
    start, stop = int(first_number), int(second_number)
    if first_prefix.casefold() != second_prefix.casefold() or stop < start or stop - start > 1000:
        return [token]
    width = max(len(first_number), len(second_number))
    return [f"{first_prefix}{number:0{width}d}" for number in range(start, stop + 1)]


def expand_references(value: str) -> list[str]:
    """Expand grouped KiCad BOM references such as ``R1, R2`` or ``C1-C4``."""

    references: list[str] = []
    for token in re.split(r"[,;\s]+", value.strip()):
        references.extend(_expand_reference_token(token))
    return references


def _dict_reader(path: Path) -> csv.DictReader[str]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ArtifactParseError(f"could not read {path.name}: {exc}") from exc
    try:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        # Keep the file alive for the iterator; the caller closes this private handle.
        reader._smd_twin_handle = handle
        return reader
    except Exception:
        handle.close()
        raise


def parse_bom_csv(path: Path) -> dict[str, BomPart]:
    reader = _dict_reader(path)
    handle = reader._smd_twin_handle
    try:
        if not reader.fieldnames or not any(
            _key(name) in {"reference", "references", "ref", "refs"} for name in reader.fieldnames
        ):
            raise ArtifactParseError("BOM CSV has no Reference/Refs column")
        parts: dict[str, BomPart] = {}
        for row in reader:
            refs = expand_references(_column(row, "Reference", "References", "Ref", "Refs"))
            if not refs:
                continue
            value = _column(row, "Value", "Val")
            footprint = _column(row, "Footprint", "Package")
            core = {
                "reference",
                "references",
                "ref",
                "refs",
                "value",
                "val",
                "footprint",
                "package",
                "quantity",
                "qty",
            }
            fields = {
                str(name): str(field_value or "")
                for name, field_value in row.items()
                if name is not None and _key(str(name)) not in core
            }
            for reference in refs:
                parts[reference.casefold()] = BomPart(reference, value, footprint, fields.copy())
        return parts
    except (csv.Error, UnicodeError) as exc:
        raise ArtifactParseError(f"invalid BOM CSV: {exc}") from exc
    finally:
        handle.close()


def _float(value: str, label: str, reference: str, *, default: float | None = None) -> float | None:
    if not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ArtifactParseError(f"{reference} has invalid {label}: {value!r}") from exc


def _component_side(value: str) -> ComponentSide:
    normalized = value.strip().casefold()
    if normalized in {"front", "top", "f", "f.cu"}:
        return ComponentSide.FRONT
    if normalized in {"back", "bottom", "b", "b.cu"}:
        return ComponentSide.BACK
    return ComponentSide.UNKNOWN


def _looks_smd(row: dict[str, str], footprint: str) -> bool:
    mount = _column(row, "Mount Type", "Mount", "Technology", "Type").casefold()
    if mount:
        if any(word in mount for word in ("through", "tht", "pth")):
            return False
        if any(word in mount for word in ("smd", "smt", "surface")):
            return True
    text = footprint.casefold()
    if any(word in text for word in ("throughhole", "through_hole", "dip-", "pinheader")):
        return False
    return any(
        word in text
        for word in (
            "smd",
            "soic",
            "sot-",
            "qfn",
            "qfp",
            "bga",
            "0402",
            "0603",
            "0805",
            "1206",
        )
    )


def parse_placement_csv(path: Path) -> dict[str, Placement]:
    reader = _dict_reader(path)
    handle = reader._smd_twin_handle
    try:
        if not reader.fieldnames or not any(
            _key(name) in {"reference", "ref"} for name in reader.fieldnames
        ):
            raise ArtifactParseError("placement CSV has no Ref column")
        placements: dict[str, Placement] = {}
        for row in reader:
            reference = _column(row, "Reference", "Ref")
            if not reference:
                continue
            x_mm = _float(_column(row, "PosX", "X", "X(mm)"), "X coordinate", reference)
            y_mm = _float(_column(row, "PosY", "Y", "Y(mm)"), "Y coordinate", reference)
            rotation = _float(
                _column(row, "Rot", "Rotation", "Orientation"),
                "rotation",
                reference,
                default=0.0,
            )
            footprint = _column(row, "Package", "Footprint")
            placements[reference.casefold()] = Placement(
                reference=reference,
                value=_column(row, "Val", "Value"),
                footprint=footprint,
                x_mm=x_mm,
                y_mm=y_mm,
                rotation_deg=float(rotation or 0.0) % 360.0,
                side=_component_side(_column(row, "Side", "Layer")),
                is_smd=_looks_smd(row, footprint),
            )
        return placements
    except (csv.Error, UnicodeError) as exc:
        raise ArtifactParseError(f"invalid placement CSV: {exc}") from exc
    finally:
        handle.close()


def parse_logical_netlist(path: Path) -> tuple[tuple[Net, ...], dict[str, dict[str, Any]]]:
    """Return logical nets and schematic component metadata from KiCad XML."""

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, UnicodeError) as exc:
        raise ArtifactParseError(f"invalid KiCad XML netlist: {exc}") from exc
    if root.tag != "export" and root.find("nets") is None:
        raise ArtifactParseError("logical netlist has no KiCad export/nets root")

    components: dict[str, dict[str, Any]] = {}
    for node in root.findall("./components/comp"):
        reference = (node.get("ref") or "").strip()
        if not reference:
            continue
        fields = {
            (field.get("name") or "").strip(): (field.text or "").strip()
            for field in node.findall("./fields/field")
            if (field.get("name") or "").strip()
        }
        components[reference.casefold()] = {
            "reference": reference,
            "value": (node.findtext("value") or "").strip(),
            "footprint": (node.findtext("footprint") or "").strip(),
            "fields": fields,
        }

    nets: list[Net] = []
    for node in root.findall("./nets/net"):
        name = (node.get("name") or "").strip()
        if not name:
            continue
        seen: set[tuple[str, str]] = set()
        pins: list[PinRef] = []
        for pin_node in node.findall("./node"):
            reference = (pin_node.get("ref") or "").strip()
            pin = (pin_node.get("pin") or "").strip()
            identity = (reference.casefold(), pin)
            if not reference or not pin or identity in seen:
                continue
            seen.add(identity)
            pins.append(PinRef(reference, pin))
        pins.sort(key=lambda item: (natural_key(item.reference), natural_key(item.pin)))
        nets.append(Net(name=name, pins=tuple(pins)))
    nets.sort(key=lambda item: natural_key(item.name))
    return tuple(nets), components


def natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)
    )


def validate_svg(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, UnicodeError) as exc:
        raise ArtifactParseError(f"invalid SVG: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() != "svg":
        raise ArtifactParseError("preview root element is not SVG")


def dxf_bounds(path: Path) -> tuple[float, float, float, float] | None:
    """Read extents from the simple ASCII DXF emitted for ``Edge.Cuts``.

    Only values in the ``ENTITIES`` section are considered.  Other DXF
    sections contain sentinel coordinates (commonly +/-1e20) that are not
    board geometry.
    """

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ArtifactParseError(f"could not read DXF: {exc}") from exc
    if len(lines) < 4:
        raise ArtifactParseError("DXF is empty or truncated")
    pairs: list[tuple[int, str]] = []
    for index in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[index].strip())
        except ValueError:
            continue
        pairs.append((code, lines[index + 1].strip()))

    in_entities = False
    entity_pairs: list[tuple[int, str]] = []
    for index, pair in enumerate(pairs):
        code, value = pair
        if (
            not in_entities
            and code == 0
            and value.casefold() == "section"
            and index + 1 < len(pairs)
            and pairs[index + 1][0] == 2
            and pairs[index + 1][1].casefold() == "entities"
        ):
            in_entities = True
            continue
        if in_entities and code == 0 and value.casefold() == "endsec":
            break
        if in_entities:
            entity_pairs.append(pair)

    xs: list[float] = []
    ys: list[float] = []
    for code, raw_value in entity_pairs:
        if not (10 <= code <= 18 or 20 <= code <= 28):
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if abs(value) >= 1e19:
            continue
        if 10 <= code <= 18:
            xs.append(value)
        else:
            ys.append(value)
    if not xs or not ys:
        raise ArtifactParseError("DXF contains no outline coordinates")
    return min(xs), min(ys), max(xs), max(ys)
