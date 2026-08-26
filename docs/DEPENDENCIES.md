# Third-party dependency and license ledger

This ledger preserves a future commercial distribution option. It is not a
substitute for a release-time legal review. Python dependencies are installed
from PyPI; native simulators are user-provided and are not bundled in 0.1.0.

| Dependency | Tested / accepted version | Role | License and distribution policy |
| --- | --- | --- | --- |
| CPython | 3.13.7 / `>=3.13,<3.14` | Application runtime | PSF-2.0; regular 64-bit CPython only |
| PySide6 / Qt | 6.11.1 / `>=6.10,<7` | Desktop UI | LGPL-3.0/GPL-3.0/commercial; dynamically linked, ship LGPL notices/source offer as required |
| NumPy | 2.5.2 / `>=2.2,<3` | Numeric arrays | BSD-3-Clause |
| pyqtgraph | 0.14.0 / `>=0.13.7,<1` | Waveform display | MIT |
| PyYAML | 6.0.3 / `>=6.0.2,<7` | Companion manifest | MIT |
| platformdirs | 4.11.3 / `>=4.3,<5` | User cache/data paths | MIT |
| msgpack for Python | 1.2.1 / exact pin | EDA worker protocol codec | Apache-2.0 |
| Rust toolchain | 1.97.1 / exact channel | Builds the optional native EDA worker | MIT OR Apache-2.0; build-time only, not required for normal Python fallback use |
| `rmp-serde` | 1.3.1 / exact pin | Rust MessagePack serialization | MIT |
| `serde_json` | 1.0.145 / exact pin | Typed generic values inside the Rust worker | MIT OR Apache-2.0 |
| KiCad CLI | 10.0.5 / KiCad 10 | Read-only EDA import | GPL-3.0; invoke a user-installed executable in a separate process |
| KiCad official libraries | User-installed KiCad 10 corpus | Read-only search and selected-asset snapshots | CC-BY-SA-4.0 with KiCad libraries exception; do not bundle the corpus, retain per-snapshot provenance and notice |
| ngspice | 47 | Circuit solver | Modified BSD family; pin and audit the complete official Windows runtime before bundling |
| Renode | Qualification pending | Firmware emulator | MIT; invoke a user-installed executable in a separate process |

The distributable core intentionally does not use GPL SPICE wrappers. Before an
installer bundles Qt, ngspice, or Renode, record the exact archive URL, SHA-256,
transitive DLL set, license texts, and corresponding source/notice obligations.
The Rust `Cargo.lock` and `rust-toolchain.toml` are committed so CI tests the
same compiler and crate graph; a release still needs a generated third-party
notice covering all transitive crates. Freerouting is GPL-3.0, optional,
user-installed, and not linked or bundled by the MIT core.
