# USB sensor/status reference board

This project is the first SMD Twin Lab teaching fixture. It represents a future
fully assembled, USB-powered STM32G071 board with a thermistor input, status
outputs, diagnostics UART, and accessible test points.

The checked-in normalized bundle lets the application run without KiCad. The
KiCad source remains read-only during import, and `sensor_status_board.cir` is a
small simulation fixture used by the first vertical slice.

The normalized bundle is the authoritative teaching model for this release.
The KiCad board supplies the placement/outline import fixture, while the modern
schematic is intentionally a geometry-only placeholder until the reference
firmware passes its Renode qualification gate. This is not a manufacturable PCB
design or a fabrication release.

The initial fault is `thermistor_open`: the thermistor's effective resistance
becomes finite but very large, the ADC node rises close to 3.3 V, and firmware
enters its fail-safe `SENSOR_FAULT` state.
