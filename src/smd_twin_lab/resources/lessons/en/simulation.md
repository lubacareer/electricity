# Read the evidence, not only the pass badge

The pass/fail banner is a summary. The learning value is in the evidence beneath it:

- Compare waveform shape, limits, and timing with the nominal run.
- Check whether the firmware state change follows the electrical event.
- Read UART output as another observable test channel.
- Use the timeline to distinguish a modeled fault from an infrastructure failure.

Simulation accuracy depends on the netlist, device models, solver settings, and assumptions.
Repeatability does not by itself prove physical accuracy. When hardware becomes available, keep
the same named probes and scenario intent so simulated and measured results can be compared.
