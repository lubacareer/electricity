"""Immutable, editor-owned EDA document model.

The imported KiCad model in :mod:`smd_twin_lab.models` is intentionally a
read-only snapshot.  These types form a separate, versioned document that can
be edited by the Designer workspace without ever mutating an imported project.
All design-space coordinates are integer nanometres.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import cos, radians, sin
from uuid import uuid4

EDA_SCHEMA_VERSION = 1
NM_PER_MM = 1_000_000


def new_id() -> str:
    """Return a stable identifier for a newly created design object."""

    return str(uuid4())


def mm(value: float) -> int:
    """Convert millimetres to the canonical integer-nanometre unit."""

    return round(value * NM_PER_MM)


class AssetKind(StrEnum):
    SYMBOL = "symbol"
    FOOTPRINT = "footprint"
    MODEL = "model"


class PinElectricalType(StrEnum):
    PASSIVE = "passive"
    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    POWER_INPUT = "power_input"
    POWER_OUTPUT = "power_output"
    NO_CONNECT = "no_connect"


class BoardSide(StrEnum):
    FRONT = "front"
    BACK = "back"


class CopperLayer(StrEnum):
    FRONT = "F.Cu"
    BACK = "B.Cu"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True, order=True)
class PointNm:
    x_nm: int
    y_nm: int

    def __post_init__(self) -> None:
        if isinstance(self.x_nm, bool) or not isinstance(self.x_nm, int):
            raise TypeError("x_nm must be an integer")
        if isinstance(self.y_nm, bool) or not isinstance(self.y_nm, int):
            raise TypeError("y_nm must be an integer")

    def translated(self, delta: PointNm) -> PointNm:
        return PointNm(self.x_nm + delta.x_nm, self.y_nm + delta.y_nm)


@dataclass(frozen=True, slots=True)
class LibraryAssetSnapshot:
    """Exact provenance and payload for an asset selected from a library."""

    asset_id: str
    kind: AssetKind
    name: str
    source: str
    source_hash: str
    license_spdx: str = "NOASSERTION"
    payload_json: str = "{}"


@dataclass(frozen=True, slots=True)
class SchematicPin:
    pin_id: str
    number: str
    name: str
    offset: PointNm
    electrical_type: PinElectricalType = PinElectricalType.PASSIVE
    required: bool = True


@dataclass(frozen=True, slots=True)
class SchematicSymbol:
    symbol_id: str
    reference: str
    value: str
    library_id: str
    kind: str
    position: PointNm
    pins: tuple[SchematicPin, ...]
    footprint_id: str = ""
    rotation_deg: int = 0

    def pin_position(self, pin: SchematicPin) -> PointNm:
        angle = radians(self.rotation_deg % 360)
        x_nm = round(pin.offset.x_nm * cos(angle) - pin.offset.y_nm * sin(angle))
        y_nm = round(pin.offset.x_nm * sin(angle) + pin.offset.y_nm * cos(angle))
        return PointNm(self.position.x_nm + x_nm, self.position.y_nm + y_nm)


@dataclass(frozen=True, slots=True)
class SchematicWire:
    wire_id: str
    points: tuple[PointNm, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a schematic wire needs at least two points")
        if any(
            first == second for first, second in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("a schematic wire cannot contain a zero-length segment")


@dataclass(frozen=True, slots=True)
class SchematicJunction:
    junction_id: str
    position: PointNm


@dataclass(frozen=True, slots=True)
class SchematicLabel:
    label_id: str
    text: str
    position: PointNm


@dataclass(frozen=True, slots=True)
class SchematicDocument:
    symbols: tuple[SchematicSymbol, ...] = ()
    wires: tuple[SchematicWire, ...] = ()
    junctions: tuple[SchematicJunction, ...] = ()
    labels: tuple[SchematicLabel, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardPad:
    pad_id: str
    pin_number: str
    offset: PointNm
    width_nm: int
    height_nm: int
    net: str = ""
    shape: str = "rect"
    layers: tuple[CopperLayer, ...] = (CopperLayer.FRONT,)
    drill_nm: int = 0

    def __post_init__(self) -> None:
        if self.width_nm <= 0 or self.height_nm <= 0:
            raise ValueError("pad dimensions must be positive")
        if self.drill_nm < 0:
            raise ValueError("pad drill cannot be negative")
        if not self.layers:
            raise ValueError("a pad must be present on at least one copper layer")


@dataclass(frozen=True, slots=True)
class BoardFootprint:
    footprint_id: str
    reference: str
    library_id: str
    symbol_id: str
    position: PointNm
    pads: tuple[BoardPad, ...]
    rotation_deg: int = 0
    side: BoardSide = BoardSide.FRONT
    courtyard_width_nm: int = 0
    courtyard_height_nm: int = 0

    def pad_position(self, pad: BoardPad) -> PointNm:
        angle = radians(self.rotation_deg % 360)
        x_nm = round(pad.offset.x_nm * cos(angle) - pad.offset.y_nm * sin(angle))
        y_nm = round(pad.offset.x_nm * sin(angle) + pad.offset.y_nm * cos(angle))
        if self.side is BoardSide.BACK:
            x_nm = -x_nm
        return PointNm(self.position.x_nm + x_nm, self.position.y_nm + y_nm)


@dataclass(frozen=True, slots=True)
class BoardTrack:
    track_id: str
    net: str
    start: PointNm
    end: PointNm
    width_nm: int
    layer: CopperLayer = CopperLayer.FRONT

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("a track cannot have zero length")
        if self.width_nm <= 0:
            raise ValueError("track width must be positive")


@dataclass(frozen=True, slots=True)
class BoardVia:
    via_id: str
    net: str
    position: PointNm
    diameter_nm: int
    drill_nm: int

    def __post_init__(self) -> None:
        if self.diameter_nm <= 0 or self.drill_nm <= 0:
            raise ValueError("via diameter and drill must be positive")
        if self.drill_nm >= self.diameter_nm:
            raise ValueError("via drill must be smaller than its diameter")


@dataclass(frozen=True, slots=True)
class NetClass:
    name: str = "Default"
    nets: tuple[str, ...] = ()
    clearance_nm: int = mm(0.2)
    track_width_nm: int = mm(0.25)
    via_diameter_nm: int = mm(0.6)
    via_drill_nm: int = mm(0.3)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("net class name must not be empty")
        if (
            min(
                self.clearance_nm,
                self.track_width_nm,
                self.via_diameter_nm,
                self.via_drill_nm,
            )
            <= 0
        ):
            raise ValueError("net class dimensions must be positive")
        if self.via_drill_nm >= self.via_diameter_nm:
            raise ValueError("net class via drill must be smaller than its diameter")


@dataclass(frozen=True, slots=True)
class BoardDocument:
    outline: tuple[PointNm, ...] = ()
    footprints: tuple[BoardFootprint, ...] = ()
    tracks: tuple[BoardTrack, ...] = ()
    vias: tuple[BoardVia, ...] = ()
    net_classes: tuple[NetClass, ...] = (NetClass(),)
    copper_layers: tuple[CopperLayer, ...] = (CopperLayer.FRONT, CopperLayer.BACK)

    def __post_init__(self) -> None:
        if not self.copper_layers:
            raise ValueError("a board needs at least one copper layer")
        if len(self.copper_layers) != len(set(self.copper_layers)):
            raise ValueError("board copper layers must be unique")


@dataclass(frozen=True, slots=True)
class DesignRulesProfile:
    minimum_track_width_nm: int = mm(0.2)
    minimum_clearance_nm: int = mm(0.2)
    minimum_via_diameter_nm: int = mm(0.5)
    minimum_via_drill_nm: int = mm(0.25)
    copper_to_edge_nm: int = mm(0.25)

    def __post_init__(self) -> None:
        values = (
            self.minimum_track_width_nm,
            self.minimum_clearance_nm,
            self.minimum_via_diameter_nm,
            self.minimum_via_drill_nm,
            self.copper_to_edge_nm,
        )
        if min(values) <= 0:
            raise ValueError("design rule dimensions must be positive")
        if self.minimum_via_drill_nm >= self.minimum_via_diameter_nm:
            raise ValueError("minimum via drill must be smaller than its diameter")


@dataclass(frozen=True, slots=True)
class TeachingMetadata:
    mode: str = "learning"
    lesson_ids: tuple[str, ...] = ()
    template_id: str = "blank"


@dataclass(frozen=True, slots=True)
class KiCadBridgeState:
    managed_project_path: str | None = None
    baseline_hash: str | None = None
    uuid_mappings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    schema_version: int
    project_id: str
    name: str
    revision: int = 0
    created_at: str = ""
    generator: str = "smd_twin_lab"

    def __post_init__(self) -> None:
        if self.schema_version != EDA_SCHEMA_VERSION:
            raise ValueError(f"unsupported EDA schema version: {self.schema_version}")
        if not self.project_id:
            raise ValueError("project_id must not be empty")
        if not self.name.strip():
            raise ValueError("project name must not be empty")
        if self.revision < 0:
            raise ValueError("revision cannot be negative")


@dataclass(frozen=True, slots=True)
class EdaProjectDocument:
    manifest: ProjectManifest
    schematic: SchematicDocument
    board: BoardDocument
    rules: DesignRulesProfile = field(default_factory=DesignRulesProfile)
    library_assets: tuple[LibraryAssetSnapshot, ...] = ()
    teaching: TeachingMetadata = field(default_factory=TeachingMetadata)
    kicad_bridge: KiCadBridgeState = field(default_factory=KiCadBridgeState)
    compliance_case_ids: tuple[str, ...] = ()

    @property
    def schema_version(self) -> int:
        return self.manifest.schema_version

    @property
    def project_id(self) -> str:
        return self.manifest.project_id

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def revision(self) -> int:
        return self.manifest.revision

    def revised(self, **changes: object) -> EdaProjectDocument:
        """Return an edited document with a monotonically increased revision."""

        return replace(
            self,
            manifest=replace(self.manifest, revision=self.revision + 1),
            **changes,
        )
