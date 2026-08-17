"""Headless commands for CI, diagnostics, and the no-hardware demonstration."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .models import CapabilityStatus, FaultKind, FaultSpec, Scenario
from .services import build_runtime_services


def _fault_from_args(args: argparse.Namespace) -> FaultSpec:
    if args.fault == "none":
        return FaultSpec(FaultKind.NONE)
    if args.fault == "thermistor_open":
        return FaultSpec(FaultKind.COMPONENT_OPEN, reference="RT1")
    if args.fault == "sensor_to_ground":
        return FaultSpec(FaultKind.NET_SHORT, net_a="ADC_SENSE", net_b="GND")
    if args.fault == "sensor_to_vdd":
        return FaultSpec(FaultKind.NET_SHORT, net_a="ADC_SENSE", net_b="VDD")
    if args.fault == "wrong_value":
        return FaultSpec(FaultKind.WRONG_VALUE, reference="RT1", value=args.value)
    if args.fault == "intermittent":
        return FaultSpec(
            FaultKind.INTERMITTENT,
            reference="RT1",
            start_s=0.025,
            duration_s=0.05,
        )
    raise ValueError(f"unsupported fault: {args.fault}")


def _demo(args: argparse.Namespace) -> int:
    services = build_runtime_services(prefer_external_spice=args.engine == "auto")
    project = services.load_sample()
    scenario = Scenario(
        scenario_id=f"cli-{args.fault}-{args.temperature:g}c",
        name=args.fault.replace("_", " ").title(),
        temperature_c=args.temperature,
        fault=_fault_from_args(args),
        acknowledge=args.acknowledge,
    )
    report = services.run_scenario(project, scenario)
    if args.output:
        report.write_json(args.output)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"Project: {project.name}")
        print(f"Engines: {services.circuit_engine_name} + {services.firmware_engine_name}")
        print(f"Scenario: {scenario.name} at {scenario.temperature_c:.1f} C")
        print(f"Result: {'PASS' if report.passed else 'FAIL'} / {report.firmware_state.value}")
        print(f"ADC: {report.measurements.get('adc_voltage_v', float('nan')):.6g} V")
        for explanation in report.explanations:
            print(f"- {explanation}")
        if args.output:
            print(f"Report: {args.output.resolve()}")
    return 2 if report.infrastructure_error else int(not report.passed)


def _import_project(args: argparse.Namespace) -> int:
    services = build_runtime_services(prefer_external_spice=False)
    project = services.importer.import_project(args.project, variant=args.variant)
    print(f"Project: {project.name} ({project.project_id})")
    print(f"Cache: {project.cache_dir}")
    print(f"Components: {len(project.components)}; nets: {len(project.nets)}")
    for name in ("geometry", "circuit", "firmware", "hardware"):
        capability = getattr(project.capabilities, name)
        print(f"{name}: {capability.status.value} - {capability.detail}")
    for diagnostic in project.diagnostics:
        print(
            f"{diagnostic.severity.value.upper()} {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
    statuses = (
        project.capabilities.geometry.status,
        project.capabilities.circuit.status,
        project.capabilities.firmware.status,
        project.capabilities.hardware.status,
    )
    return 2 if all(status is CapabilityStatus.INVALID for status in statuses) else 0


def _tools(_args: argparse.Namespace) -> int:
    services = build_runtime_services(prefer_external_spice=False)
    print(f"KiCad CLI: {services.tools.kicad_cli or 'not found'}")
    print(f"ngspice: {services.tools.ngspice or 'not found (reference engine available)'}")
    print(f"Renode: {services.tools.renode or 'not found (qualification pending)'}")
    print(f"Sample: {services.load_sample().name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smd-twin-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the bundled no-hardware scenario")
    demo.add_argument(
        "--fault",
        choices=(
            "none",
            "thermistor_open",
            "sensor_to_ground",
            "sensor_to_vdd",
            "wrong_value",
            "intermittent",
        ),
        default="none",
    )
    demo.add_argument("--temperature", type=float, default=25.0)
    demo.add_argument("--value", type=float, default=47_000.0, help="wrong-value ohms")
    demo.add_argument("--acknowledge", action="store_true")
    demo.add_argument("--engine", choices=("auto", "reference"), default="auto")
    demo.add_argument("--output", type=Path)
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(handler=_demo)

    import_command = subparsers.add_parser("import", help="import a KiCad 10 project")
    import_command.add_argument("project", type=Path)
    import_command.add_argument("--variant", default="default")
    import_command.set_defaults(handler=_import_project)

    tools = subparsers.add_parser("tools", help="show external-tool availability")
    tools.set_defaults(handler=_tools)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
