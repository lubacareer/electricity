# Architecture

SMD Twin Lab has two deliberately separate document paths:

- **Test Lab** treats imported KiCad sources as immutable. It normalizes staged
  exports into an EDA-independent bundle and applies faults only to generated
  simulation inputs.
- **PCB Designer** edits a repository-owned `EdaProjectDocument`. It saves a
  versioned `.smdeda` package and may export a new, managed KiCad project after
  independent validation. It never turns an arbitrary imported project into an
  editable document in place.

The GUI owns presentation and undo state. KiCad, ngspice, Renode, and the Rust
EDA worker run behind process boundaries so cancellation or a native crash does
not freeze the main window.

```text
PySide6 UI
  -> Test Lab
       -> ProjectImporter -> staged KiCad CLI -> immutable normalized bundle
       -> CoSimulationSupervisor
       -> CircuitEngine -> ngspice worker
       -> FirmwareEngine -> Renode worker or deterministic reference model
       -> HardwareTarget -> loopback now, serial/fixture later
  -> PCB Designer
       -> .smdeda repository -> editable schematic and board documents
       -> SchematicCompiler -> connectivity graph and ERC
       -> CircuitCompiler -> owned DC MNA solver or ngspice 47 batch adapter
       -> DrcEngine / ManualRouter -> current Python alpha
       -> MessagePack worker -> Rust health/protocol scaffold
       -> KiCad10Bridge -> staged validation -> new managed output
```

Imported board coordinates are millimetres, top-view, X right, Y up, with
counter-clockwise angles. Editable design geometry uses integer nanometres to
avoid cumulative floating-point drift. Bottom-side placement is stored
unmirrored; mirroring is a view transform.

The importer recursively stages the authorized project tree in a temporary
directory, without following symlinks and excluding generated/session folders.
KiCad receives only staged paths; normalized artifacts are atomically committed
to the application cache after source hashes are rechecked.

The companion `twin.yaml` is versioned and contains project semantics that do
not belong in the KiCad design: model coverage, probes, faults, firmware-to-net
bindings, scenarios, and contextual lessons.

## Editable document and persistence

`EdaProjectDocument` holds stable UUIDs, a monotonic revision, schematic and
board documents, design rules, selected library snapshots, teaching metadata,
and the future KiCad synchronization baseline. A `.smdeda` file is currently a
deterministic ZIP containing a schema-1 JSON envelope. Saves use a sibling
temporary file, `fsync`, and atomic replacement; autosaves live under the user
application-data directory. Corrupt, oversized, and unsupported packages fail
closed.

The installed KiCad library catalog is indexed read-only in SQLite. Search and
resolution are separate: resolving a selected asset captures only that symbol
or footprint source, its hash, provenance, and independent render/export/model
capability flags. Unsupported constructs remain identifiable and are never
silently approximated.

## Simulation and design checks

The owned alpha solver supports DC modified nodal analysis for resistors and
independent voltage sources. Opens, shorts, and wrong values are deterministic
circuit transformations. The standalone ngspice 47 adapter compiles the same
supported circuit into a temporary batch deck, validates the executable major
version, and maps sanitized SPICE node names back to canonical design nets.

The current Python DRC checks a closed outline, track/via dimensions, 45°/90°
segments, copper and edge clearances, courtyard overlap, schematic-to-pad net
parity, wrong-net termination, and unrouted groups. The manual router contract
previews before committing a single document revision. Zones, an interactive
via tool, push-and-shove, advanced constraints, and autorouting are not yet
implemented.

## Rust worker boundary

Protocol version 1 uses a four-byte big-endian length followed by bounded
MessagePack. Every message carries a request ID, document revision, method,
payload, and type; the client handles progress, cancellation, stale revisions,
timeouts, malformed output, crashes, and restart. The Rust binary currently
implements protocol/health behavior only. DRC and routing remain explicitly
unavailable so the Python implementation is not mistaken for completed Rust
acceleration.

## KiCad export boundary

The first adapter is pinned to KiCad 10.0.5. It generates schematic, board, and
project files in a temporary staging directory, lets KiCad canonicalize them,
runs JSON ERC and DRC with schematic parity, exports a semantic netlist for
reference/net comparison, and commits only a clean result to a new or empty
destination. Manufacturing output (Gerbers, drill, BOM, placement CSV, hashes,
and manifest) is built only from that validated project. Editable round-trip
and three-way merge return a gated capability result instead of risking source
loss.
