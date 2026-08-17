# Product roadmap

## Implemented foundation

- Read-only, normalized KiCad import contract and cached-bundle operation.
- Process-isolated ngspice and Renode adapter boundaries.
- Reference NTC circuit and firmware behavior.
- Versioned JSON-Lines hardware contract and loopback target.
- Native desktop workflow, contextual lessons, JSON reports, and SQLite history.

## Gated external work

- Qualify the exact STM32G071 ELF in a pinned Renode release.
- Add the CFFI shared-ngspice worker only after batch simulations are stable.
- Validate firmware and protocol on a NUCLEO-G071RB.
- Select production parts and order the custom PCBA only after Nucleo validation.
- Add independent fixture measurements, record/replay, and clean-machine installer.
- Complete the third-party licensing review before distributing simulator runtimes.
