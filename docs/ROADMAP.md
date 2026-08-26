# Product roadmap

## Implemented foundation

- Read-only, normalized KiCad import contract and cached-bundle operation.
- Process-isolated ngspice and Renode adapter boundaries.
- Reference NTC circuit and firmware behavior.
- Versioned JSON-Lines hardware contract and loopback target.
- Native desktop workflow, contextual lessons, JSON reports, and SQLite history.

## PCB Designer alpha now implemented

- A separate editable Designer workspace; the existing Test Lab and imported
  KiCad files remain read-only.
- Stable-UUID, integer-nanometre `EdaProjectDocument` model with deterministic,
  atomic `.smdeda` save/load and application-data autosave support.
- Blank and 3.3 V divider templates, component placement, schematic wiring,
  footprint movement, front-copper 45°/90° track gestures, and one-command
  undo/redo for edits.
- Explicit-junction schematic connectivity/ERC, schematic-to-board
  synchronization services, basic two-layer DRC, and a manual-router
  preview/commit contract.
- Owned resistor/independent-voltage-source DC MNA simulation. The divider
  produces 1.65 V at `VOUT`; open, short, and wrong-value transformations are
  supported. A standalone ngspice 47 batch adapter can run the same compiled
  circuit when available.
- Read-only, lazy KiCad library indexing with source-hashed selected-asset
  snapshots and separate render/export/internal/ngspice capability flags.
- Transactional one-way KiCad 10.0.5 export into a new directory, followed by
  KiCad canonicalization, ERC, DRC, schematic parity, and semantic comparison.
  Clean exports can produce Gerbers, Excellon drill, BOM, placement CSV, and a
  hash manifest.
- A pinned Rust workspace and bounded, length-prefixed MessagePack worker
  protocol with correlation, stale-revision checks, cancellation, timeout,
  crash detection, and restart. The native worker's DRC/router capabilities
  are intentionally unavailable at this stage.

## Next Designer milestones

- Add native previews and connect capability-approved indexed KiCad parts to
  placement. The alpha browser already exposes search results as browse-only,
  keeping unsupported constructs visible but blocked from unsafe export.
- Add junction/label/property editing, footprint assignment, interactive vias,
  back-layer selection, zones, keepouts, live incremental DRC navigation, and
  richer learning lessons.
- Expand owned and ngspice circuit models, probes, overlays, sweeps, tolerances,
  and fault comparisons without inventing behavior for unmodelled parts.
- Move benchmarked geometry/connectivity/DRC work behind the Rust worker, then
  build deterministic autorouting proposals with accept/reject as one undo
  command.
- Mature managed KiCad pairing only after lossless unknown-node preservation,
  three-way merge/conflict UI, interrupted-sync recovery, and cross-version
  corpus tests pass.
- Add optional Freerouting through a user-installed DSN/SES process; it remains
  unbundled and separate from the MIT core.

## Gated external work

- Qualify the exact STM32G071 ELF in a pinned Renode release.
- Add the CFFI shared-ngspice worker only after batch simulations are stable.
- Validate firmware and protocol on a NUCLEO-G071RB.
- Select production parts and order the custom PCBA only after Nucleo validation.
- Add independent fixture measurements, record/replay, and clean-machine installer.
- Complete the third-party licensing review before distributing simulator runtimes.
