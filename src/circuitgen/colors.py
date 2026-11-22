"""Consistent, configurable schematic ink color."""
import re

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_HEX_IN_SVG = re.compile(r"#[0-9a-fA-F]{6}\b")


def apply_ink_color(svg: str, color: str = "#000000") -> str:
    """Recolor schematic ink while preserving white symbol fills."""
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise ValueError("color must be a six-digit hex string, e.g. '#000000'")
    return _HEX_IN_SVG.sub(lambda m: m.group(0) if m.group(0).lower() == '#ffffff' else color, svg)
