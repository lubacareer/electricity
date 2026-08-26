# Start with a known-good board

This lab connects three views of the same product:

1. **Assembly:** where a component is mounted and what AOI can see.
2. **Circuit:** which nets and electrical behavior that component influences.
3. **Firmware:** how measured voltages become states, messages, and outputs.

Run the **Nominal** scenario first. Its waveforms and firmware state are your baseline. Then
introduce one fault at a time and ask: *what changed, where did it propagate, and what evidence
would AOI or functional test observe?*

The built-in sample is intentionally software-only. Later, the same project and scenario contracts
can drive an ngspice model, a firmware emulator, or a physical test fixture.
