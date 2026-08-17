# Architecture

The GUI owns presentation state only. KiCad, ngspice, and Renode are external
processes behind small contracts. Imported data is normalized before reaching
the UI, and simulation faults are applied only to generated working copies.

```text
PySide6 UI
  -> ProjectImporter -> KiCad CLI -> normalized bundle
  -> CoSimulationSupervisor
       -> CircuitEngine -> ngspice worker
       -> FirmwareEngine -> Renode worker or deterministic reference model
  -> HardwareTarget -> loopback now, serial/fixture later
```

Canonical board coordinates are millimetres, top-view, X right, Y up, with
counter-clockwise angles. Bottom-side placement is stored unmirrored; mirroring
is a view transform.

The importer recursively stages the authorized project tree in a temporary
directory, without following symlinks and excluding generated/session folders.
KiCad receives only staged paths; normalized artifacts are atomically committed
to the application cache after source hashes are rechecked.

The companion `twin.yaml` is versioned and contains project semantics that do
not belong in the KiCad design: model coverage, probes, faults, firmware-to-net
bindings, scenarios, and contextual lessons.
