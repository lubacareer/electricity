# Model faults explicitly

A fault scenario changes a generated simulation copy; it must never rewrite the source design.

- **Open component:** interrupts one modeled current path.
- **Net short:** adds a finite low-resistance path between two named nets.
- **Wrong value:** changes a component parameter by a documented amount.
- **Reversed polarity:** substitutes the approved reverse-orientation model.
- **Intermittent:** activates a fault at explicit virtual-time boundaries.

Start with one fault. Record its target, value, start time, temperature, simulator version, and
model assumptions in the saved report. Literal zero-ohm shorts and infinite opens often create
numerical problems, so concrete simulation backends should use documented finite values.
