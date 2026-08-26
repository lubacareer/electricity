# PCB Designer alpha

PCB Designer is a learning-first, editable workspace beside the existing Test
Lab. Test Lab imports remain immutable; Designer stores its own document and
exports only into a new, application-managed directory.

## Try the divider

1. Start SMD Twin Lab and select the **PCB Designer** top-level tab.
2. Choose **New > 3.3 V divider**. The template contains a 3.3 V source and two
   10 kΩ resistors.
3. Open **Schematic**, select **Wire**, and click two endpoints to add a wire.
   A crossing is not a connection unless the document contains an explicit
   junction.
4. Open **PCB**, select a route net, choose **Route**, and click two endpoints.
   The alpha snaps the segment to a 45° or 90° direction on front copper.
5. Drag symbols or footprints to move them. A gesture becomes one undoable
   command. `Ctrl+Z`/`Ctrl+Y` undo and redo; `Delete` removes the selected item,
   `R` rotates it, and `Home` fits the active view.
6. Press `F7` to run design checks and `F6` to simulate. The divider should
   show `VOUT = 1.65 V`. The application prefers standalone ngspice 47 when it
   is present and otherwise uses the owned DC solver.
7. Save the editable document as `.smdeda`. **Verify in KiCad** requires KiCad
   10.0.5 and a new or empty destination.

Use **Index KiCad libraries** above the component palette to index and search
installed native libraries. These results are intentionally browse-only in the
alpha; the four owned palette primitives are the only directly placeable
items.

The **Blank project** is currently a schematic canvas and starts without a PCB
outline. Because the alpha does not yet include an outline editor, use the
voltage-divider template for the complete PCB-to-KiCad export lesson.

The English/Russian language switch retranslates the Designer while preserving
the current document, selected item, active tool, view state, and undo history.

## Current capability boundary

| Area | Available in this alpha | Deliberately not claimed yet |
| --- | --- | --- |
| Schematic | Built-in resistor, voltage-source, LED, and ground placement; drag, rotate, delete, two-click wires, connectivity/ERC | Full KiCad symbol semantics, hierarchy, complete junction/label/property UI, arbitrary model behavior |
| Simulation | Deterministic DC MNA for resistors and independent voltage sources; open/short/wrong-value transformations; ngspice 47 batch parity | General SPICE models, transient/sweep UI, mixed-signal or firmware co-simulation for Designer documents |
| PCB | Closed outline model, front/back data model, footprint drag, front-layer 45°/90° tracks, net classes, via data model, basic two-layer DRC | Board-outline editor; interactive via/back-layer/zone/keepout tools; push-and-shove, differential pairs, tuning, impedance, autorouting |
| Libraries | Searchable, browse-only results from a read-only SQLite index of reachable native KiCad symbols and footprints; lazy exact-source resolution and provenance snapshots | Native previews and safe placement/export of every KiCad construct; unsupported parts must remain blocked rather than approximated |
| KiCad | One-way generation, canonicalization, clean ERC/DRC/parity/semantic validation, and manufacturing package from a clean project | Overwriting arbitrary projects, editable round-trip, lossless three-way merge, multi-version adapter guarantees |
| Rust worker | Pinned workspace; bounded MessagePack protocol; request/revision correlation, cancellation, timeout, crash/restart tests | Native DRC, geometry acceleration, or routing implementation |

## External tools

- KiCad 10.0.5: needed only for verified KiCad/manufacturing output.
- ngspice 47: optional; set `SMD_TWIN_NGSPICE`, put it on `PATH`, or unpack the
  official Windows build so `Spice64\bin\ngspice_con.exe` exists in the
  checkout.
- Rust 1.97.1: needed for contributors building the optional native worker.
  `cargo build --workspace --locked` uses the repository pin and creates a
  binary the Python client can discover.

KiCad, ngspice, and Rust are not required to open, edit, save, or run the owned
divider solver. Missing tools produce capability messages instead of changing
the design.

## Persistence and safe export

`.smdeda` is a versioned ZIP package with deterministic JSON design data,
stable UUIDs, integer-nanometre coordinates, selected asset snapshots and
provenance, rules, teaching metadata, and reserved KiCad bridge state. Saves
are atomic; autosaves are kept under the user's application-data directory.

KiCad export happens in a temporary staging directory. The bridge runs KiCad
10.0.5 canonicalization, JSON ERC, JSON DRC with schematic parity, and a
semantic reference/net comparison before committing. A failed validation
leaves the selected destination unmodified. Fabrication files are educational
outputs, not evidence of electrical safety, EMC, regulatory compliance, or
production readiness.
