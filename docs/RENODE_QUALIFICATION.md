# STM32G071 Renode qualification gate

Renode is not considered an available firmware engine merely because its
executable is installed. A numbered release must pass this gate with the exact
reference firmware ELF:

1. Load and reset the STM32G071-family platform deterministically.
2. Observe the expected boot UART line.
3. Inject ADC values representing 25 °C, 35 °C, 32.9 °C, and an open sensor.
4. Observe green/red/buzzer GPIO transitions and the acknowledge input.
5. Exercise one timer/PWM output.
6. Repeat the scenario ten times and compare ordered virtual-time event traces.

If UART, GPIO, ADC injection, or deterministic stepping cannot pass without
patching Renode, the first plugin switches to STM32F072. Unsupported native USB,
clock/low-power behavior, and exact cycle timing remain explicitly unavailable.

The desktop application uses the deterministic reference firmware model until
this gate passes and must label the active engine in every report.
