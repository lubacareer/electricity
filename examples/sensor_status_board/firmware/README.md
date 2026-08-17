# STM32G071 firmware target

`controller.c` is the hardware-independent firmware state machine used by both
the host-side reference model and the future STM32 application. The eventual
board support package must provide ADC sampling, GPIO writes, the acknowledge
button, a monotonic tick, and UART output.

The first Renode qualification gate expects an ARM ELF at
`build/sensor_status_board.elf`. Board-startup and linker files are deliberately
not fabricated here: they must be generated for the selected STM32G071 toolchain
and verified first against a NUCLEO-G071RB. Until that gate passes, the desktop
application labels the deterministic Python implementation as the reference
firmware model rather than claiming MCU fidelity.
