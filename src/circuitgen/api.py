from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .ai import generate_netlist, list_providers
from .engine import CircuitError, CircuitModel, make_svg_from_netlist, validate_model
from .routing import make_routed_svg
from .symbols import BUILTIN_SYMBOLS


# RESULTS

@dataclasses.dataclass
class CircuitResult:
    svg: str
    netlist: dict[str, Any]
    model: CircuitModel | None = None
    output_path: str | None = None

    def save(self, path: str | Path) -> str:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.svg, encoding="utf-8")
        self.output_path = str(dest)
        return self.output_path


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = dataclasses.field(default_factory=list)
    model: CircuitModel | None = None


# DISCOVERY

def list_symbols() -> list[str]:
    return sorted(BUILTIN_SYMBOLS.keys())


# VALIDATION

def validate(data: dict[str, Any]) -> ValidationResult:
    try:
        model = validate_model(data)
        return ValidationResult(valid=True, errors=[], model=model)
    except (CircuitError, ValueError, TypeError, KeyError) as exc:
        return ValidationResult(valid=False, errors=[str(exc)], model=None)


# RENDERING

def render(
    model: CircuitModel | dict[str, Any],
    *,
    optimize: bool = False,
    color: str = "#000000",
) -> str:
    if isinstance(model, CircuitModel):
        return make_routed_svg(model, optimize=optimize, color=color)
    if isinstance(model, dict):
        return make_svg_from_netlist(model, optimize=optimize, color=color)
    raise TypeError(f"Expected CircuitModel or netlist dict, got {type(model).__name__}")


# WORKFLOWS

def from_netlist(
    data: dict[str, Any],
    *,
    output: str | Path | None = None,
    optimize: bool = False,
    color: str = "#000000",
) -> CircuitResult:
    model = validate_model(data)
    svg = make_svg_from_netlist(data, optimize=optimize, color=color)
    result = CircuitResult(svg=svg, netlist=data, model=model)
    if output is not None:
        result.save(output)
    return result


def generate(
    description: str,
    *,
    provider: str = "gemini",
    model: str | None = None,
    output: str | Path | None = None,
    optimize: bool = False,
    color: str = "#000000",
    correction: str = "",
) -> CircuitResult:
    netlist = generate_netlist(
        description,
        provider=provider,
        model=model,
        correction=correction,
    )
    return from_netlist(
        netlist,
        output=output,
        optimize=optimize,
        color=color,
    )
