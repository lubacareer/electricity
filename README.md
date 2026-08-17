# SMD Twin Lab

SMD Twin Lab is a Windows-first, local desktop application for learning how an
SMD PCB behaves, how manufacturing faults change its signals, and how the same
test scenario can later move from simulation to physical hardware.

The current vertical slice is entirely software-only. It opens an included
USB/STM32G071 sensor-board teaching model, visualizes its SMD placement and
nets, runs a deterministic circuit/firmware model, injects an open thermistor,
plots the ADC waveform, explains the result, and saves a JSON report. A
standalone ngspice 47 installation is used automatically when available.

## Start here on Windows

Prerequisites: regular 64-bit CPython 3.13 and PowerShell. No soldering tools,
PCB, KiCad, ngspice, or Renode are needed for the first lesson.

```powershell
git clone https://github.com/lubacareer/electricity.git
cd electricity
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m smd_twin_lab
```

In the app:

1. Run the default nominal scenario. At 25 °C it should report `NORMAL`, green
   on, red off, buzzer off, and about 1.65 V at `ADC_SENSE`.
2. Choose **Component open**. `RT1` is selected by default.
3. Run again. The app highlights RT1, shows the affected `ADC_SENSE` net near
   3.3 V, and reports the expected fail-safe `SENSOR_FAULT` state.
4. Use **File > Save report** to keep the complete JSON evidence.

The same lesson can be run without the GUI:

```powershell
.\.venv\Scripts\smd-twin-cli.exe tools
.\.venv\Scripts\smd-twin-cli.exe demo --engine reference --fault none
.\.venv\Scripts\smd-twin-cli.exe demo --engine reference --fault thermistor_open --output open.json
.\.venv\Scripts\python.exe -m pytest
```

## Optional external tools

KiCad 10 is needed only to import a new `.kicad_pro`; the included normalized
sample always opens offline. Import is read-only and runs against a temporary
staged copy so KiCad cannot create session files beside the source project.

```powershell
.\.venv\Scripts\smd-twin-cli.exe import C:\path\board.kicad_pro
```

Tool discovery checks `PATH`, KiCad's standard Windows location, and these
optional overrides:

```powershell
$env:SMD_TWIN_KICAD_CLI = 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
$env:SMD_TWIN_NGSPICE = 'C:\tools\ngspice\bin\ngspice_con.exe'
$env:SMD_TWIN_RENODE = 'C:\tools\Renode\renode.exe'
```

Normalized import caches live under the Windows local application-data folder;
run reports and `runs.sqlite3` live under the user application-data folder.
Source KiCad files are never used as output locations.

## Honest current status

- The bundled normalized teaching model completes the nominal and RT1-open
  end-to-end scenarios with either the reference solver or ngspice 47.
- Arbitrary KiCad 10 projects can be imported and inspected. Simulation is
  deliberately disabled unless a supported board plugin and valid circuit and
  firmware capabilities exist; the app will not fabricate a reference-board
  PASS for an unrelated PCB.
- The checked-in KiCad board is a placement/outline fixture. Its modern
  schematic is intentionally minimal, so importing that source currently gives
  geometry-only capability. The bundled normalized model is the first-release
  lesson source.
- The process-isolated Renode adapter and deterministic qualification API are
  implemented, but no STM32G071 ELF/integration script is claimed as qualified
  yet. The reference firmware model remains active until that gate passes.
- The reference design is educational, not manufacturable. No custom PCBA
  should be ordered from these files.

See [architecture](docs/ARCHITECTURE.md), the [roadmap](docs/ROADMAP.md), the
[Renode gate](docs/RENODE_QUALIFICATION.md), and the
[dependency/license ledger](docs/DEPENDENCIES.md). The project itself is MIT
licensed.

## Product boundaries

- Generated results are educational and diagnostic; they are not proof of
  electrical safety, manufacturability, solder quality, production yield, or
  certification.
- Employer or customer board files should only be loaded when their use is
  explicitly authorized.
- Native USB emulation, RF, high voltage, signal integrity, AI/ML, and optical
  AOI are outside the initial release.
