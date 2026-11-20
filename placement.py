"""Deterministic, routing-independent placement of schematic symbols.

Produces a layout proposal for arbitrary validated circuit topologies. It does
not draw electrical wires. Phase 5 will route using each placed pin endpoint.
"""

from __future__ import annotations

import collections
import dataclasses
import math
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xml_escape

from symbols import (GRID_PX, SymbolSpec, pin_positions, symbol_body_svg,
                     symbol_pin_labels, transform_point)

if TYPE_CHECKING:
    from engine import CircuitModel, Component

SOURCE_KINDS = {"battery", "dc_source", "ac_source", "current_source"}
COLUMN_GAP = 6.0
ROW_GAP = 4.5
ISLAND_GAP = 9.0


@dataclasses.dataclass(frozen=True)
class SymbolPlacement:
    cid: str
    x: float
    y: float
    rotation: int
    mirror: bool
    half_width: float
    half_height: float
    pin_positions: dict[str, tuple[float, float]]
    title_y: float
    value_y: float


@dataclasses.dataclass(frozen=True)
class Placement:
    symbols: dict[str, SymbolPlacement]
    layers: tuple[tuple[str, ...], ...]
    islands: tuple[tuple[str, ...], ...]


def _xml(s: str) -> str:
    return xml_escape(s, {'"': '&quot;', "'": '&apos;'})


def _component_graph(model: CircuitModel) -> dict[str, dict[str, float]]:
    """Weighted connectivity graph. High-fanout nets have less pairwise weight."""
    graph: dict[str, dict[str, float]] = {cid: {} for cid in model.components}
    for net in model.nets.values():
        members = sorted({pin.split(".", 1)[0] for pin in net})
        if len(members) < 2:
            continue
        weight = 1 / (len(members) - 1)
        for i, first in enumerate(members):
            for second in members[i + 1:]:
                graph[first][second] = graph[first].get(second, 0.0) + weight
                graph[second][first] = graph[second].get(first, 0.0) + weight
    return graph


def _islands(graph: dict[str, dict[str, float]]) -> list[tuple[str, ...]]:
    unseen = set(graph)
    groups = []
    while unseen:
        seed = min(unseen)
        queue = collections.deque([seed])
        unseen.remove(seed)
        group = []
        while queue:
            current = queue.popleft()
            group.append(current)
            for neighbor in sorted(graph[current]):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
        groups.append(tuple(sorted(group)))
    groups.sort(key=lambda group: (
        not any(cid.upper().startswith("V") for cid in group), group[0]
    ))
    return groups


def _net_depths(model: CircuitModel, group: tuple[str, ...], anchor: str | None) -> dict[str, int]:
    """Net graph ignores sources as shorts; levels are topological, not voltages."""
    all_nets = sorted({net for cid in group for net in model.components[cid].pins.values()})
    connections: dict[str, set[str]] = {name: set() for name in all_nets}
    for cid in group:
        part = model.components[cid]
        if part.kind in SOURCE_KINDS:
            continue
        attached = sorted(set(part.pins.values()))
        # A star connects an arbitrary-pin part's nets without quadratic edges.
        if attached:
            for name in attached[1:]:
                connections[attached[0]].add(name)
                connections[name].add(attached[0])
    roots = []
    if anchor:
        first = model.components[anchor].pins.get("p")
        if first in connections:
            roots.append(first)
    roots += sorted(all_nets, key=lambda n: (not any(t in n.upper() for t in
        ("INPUT", "VIN", "VCC", "PWR")), n))
    depth = {}
    for root in roots:
        if root in depth:
            continue
        base = max(depth.values(), default=-2) + 2
        depth[root] = base
        queue = collections.deque([root])
        while queue:
            current = queue.popleft()
            for neighbor in sorted(connections[current]):
                if neighbor not in depth:
                    depth[neighbor] = depth[current] + 1
                    queue.append(neighbor)
    return depth


def _geometry(symbol: SymbolSpec, comp: Component, rotation: int, mirror: bool):
    """Conservative bounding box, including pin labels and id/value text."""
    corners = [transform_point(i * symbol.half_width, j * symbol.half_height,
                               rotation, mirror)
               for i in (-1, 1) for j in (-1, 1)]
    points = corners + list(pin_positions(symbol, rotation=rotation, mirror=mirror).values())
    left, top = min(x for x, _ in points), min(y for _, y in points)
    right, bottom = max(x for x, _ in points), max(y for _, y in points)

    if len(symbol.pins) > 2:
        for label, lx, baseline, anchor, _, _, _ in symbol_pin_labels(symbol, rotation, mirror):
            x, y = lx / GRID_PX, baseline / GRID_PX
            text_width = len(label) * 6.3 / GRID_PX
            label_left = x if anchor == "start" else (x - text_width if anchor == "end"
                                                         else x - text_width / 2)
            left = min(left, label_left)
            right = max(right, label_left + text_width)
            top = min(top, y - 10 / GRID_PX)
            bottom = max(bottom, y + 3 / GRID_PX)

    title_y = top - 23 / GRID_PX
    value_y = bottom + 29 / GRID_PX
    id_width = len(comp.cid) * 10.5 / GRID_PX
    value_width = len(comp.value) * 7.8 / GRID_PX
    left = min(left, -id_width / 2, -value_width / 2)
    right = max(right, id_width / 2, value_width / 2)
    top = min(top, title_y - 17 / GRID_PX)
    bottom = max(bottom, value_y + 4 / GRID_PX)
    return (max(abs(left), abs(right)) + 1,
            max(abs(top), abs(bottom)) + 1,
            title_y, value_y)


def _choose_orientation(comp: Component, symbol: SymbolSpec,
                        depth: dict[str, int], anchor: bool) -> tuple[int, bool]:
    if comp.kind in SOURCE_KINDS or anchor or len(symbol.pins) != 2:
        return 0, False
    first, second = symbol.pin_names
    if first not in comp.pins or second not in comp.pins:
        return 0, False
    a, b = depth[comp.pins[first]], depth[comp.pins[second]]
    if a == b:
        return 90, False
    return 0, a > b


def _layer_order(layer_ids: list[str], previous: dict[str, int],
                 graph: dict[str, dict[str, float]]) -> list[str]:
    if not previous:
        return sorted(layer_ids)
    def score(cid: str):
        linked = [(previous[other], weight) for other, weight in graph[cid].items()
                  if other in previous]
        if not linked:
            return (float("inf"), cid)
        barycenter = sum(pos * weight for pos, weight in linked) / sum(weight for _, weight in linked)
        return (barycenter, cid)
    return sorted(layer_ids, key=score)


def _one_island(model: CircuitModel, group: tuple[str, ...],
                graph: dict[str, dict[str, float]]):
    source = next((cid for cid in sorted(group) if model.components[cid].kind in SOURCE_KINDS), None)
    depths = _net_depths(model, group, source)
    stages: dict[int, list[str]] = collections.defaultdict(list)
    geometry = {}
    for cid in group:
        comp, symbol = model.components[cid], model.symbols[cid]
        if cid == source:
            stage = 0
        else:
            distances = sorted(depths[n] for n in comp.pins.values())
            stage = 1 + distances[0] + distances[-1] if distances else 1
        stages[stage].append(cid)
        rot, mirror = _choose_orientation(comp, symbol, depths, cid == source)
        hw, hh, name_y, value_y = _geometry(symbol, comp, rot, mirror)
        geometry[cid] = (rot, mirror, hw, hh, name_y, value_y)

    levels = sorted(stages)
    ordered = []
    previous: dict[str, int] = {}
    for stage in levels:
        cids = _layer_order(stages[stage], previous, graph)
        ordered.append(cids)
        previous = {cid: idx for idx, cid in enumerate(cids)} | previous

    # Each layer is spaced according to its widest part, not a fixed cell width.
    x_by_level = {}
    right = 0.0
    for idx, (stage, cids) in enumerate(zip(levels, ordered)):
        half_width = max(geometry[cid][2] for cid in cids)
        x = half_width if idx == 0 else right + COLUMN_GAP + half_width
        x_by_level[stage] = x
        right = x + half_width

    layout = {}
    for stage, cids in zip(levels, ordered):
        heights = [geometry[cid][3] for cid in cids]
        total = sum(2 * h for h in heights) + ROW_GAP * (len(cids) - 1)
        y = -total / 2
        for cid, half_height in zip(cids, heights):
            rot, mirror, hw, hh, name_y, value_y = geometry[cid]
            y += half_height
            x = x_by_level[stage]
            pins = pin_positions(model.symbols[cid], x, y, rot, mirror)
            layout[cid] = SymbolPlacement(cid, x, y, rot, mirror, hw, hh,
                                          pins, name_y, value_y)
            y += half_height + ROW_GAP
    return layout, [tuple(items) for items in ordered]


def place_model(model: CircuitModel) -> Placement:
    """Place every component deterministically; returns pin endpoints for routing."""
    graph = _component_graph(model)
    groups = _islands(graph)
    items = {}
    layers = []
    island_y = 0.0
    for group in groups:
        partial, partial_layers = _one_island(model, group, graph)
        top = min(p.y - p.half_height for p in partial.values())
        bottom = max(p.y + p.half_height for p in partial.values())
        shift = island_y - top
        for cid, p in partial.items():
            items[cid] = dataclasses.replace(
                p, y=p.y + shift,
                pin_positions={pin: (x, y + shift) for pin, (x, y) in p.pin_positions.items()})
        island_y += bottom - top + ISLAND_GAP
        layers.extend(partial_layers)

    # Reject any accidental symbol/label bounding-box overlap at placement time.
    for i, cid in enumerate(sorted(items)):
        a = items[cid]
        for oid in sorted(items)[i + 1:]:
            b = items[oid]
            if (abs(a.x - b.x) < a.half_width + b.half_width - 1e-8 and
                    abs(a.y - b.y) < a.half_height + b.half_height - 1e-8):
                raise ValueError(f"Placement collision between {cid} and {oid}")
    return Placement(items, tuple(layers), tuple(groups))


def _net_guides(model: CircuitModel, placement: Placement):
    """Spanning connections for visualization only; never treat as real wires."""
    for name in sorted(model.nets):
        pins = sorted(model.nets[name])
        positions = []
        for ref in pins:
            cid, pin = ref.split(".", 1)
            positions.append(placement.symbols[cid].pin_positions[pin])
        if len(positions) <= 1:
            continue
        covered = {0}
        while len(covered) < len(positions):
            _, a, b = min((abs(positions[i][0] - positions[j][0]) +
                            abs(positions[i][1] - positions[j][1]), i, j)
                           for i in sorted(covered) for j in range(len(positions)) if j not in covered)
            covered.add(b)
            yield name, positions[a], positions[b]


def make_placement_preview_svg(model: CircuitModel) -> str:
    """Draw symbol placements and light dashed connectivity hints (not a schematic)."""
    placement = place_model(model)
    all_p = list(placement.symbols.values())
    min_x = min(p.x - p.half_width for p in all_p) * GRID_PX - 26
    max_x = max(p.x + p.half_width for p in all_p) * GRID_PX + 26
    min_y = min(p.y - p.half_height for p in all_p) * GRID_PX - 65
    max_y = max(p.y + p.half_height for p in all_p) * GRID_PX + 32
    width, height = max_x - min_x, max_y - min_y

    def pt(p):
        return (p[0] * GRID_PX - min_x, p[1] * GRID_PX - min_y)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.1f} {height:.1f}" width="{width:.1f}" height="{height:.1f}">',
        '<title>SchemAI placement preview — NOT a wired schematic</title>',
        '<desc>Dashed lines indicate shared nets, not verified electrical wires. Routing is not implemented here.</desc>',
        '<style><![CDATA[',
        '.symbol{fill:none;stroke:#213149;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}',
        '.mark{fill:#213149;font-family:Arial,sans-serif;font-size:16px;text-anchor:middle}',
        '.name{fill:#17243c;font-family:Arial,sans-serif;font-size:17px;font-weight:700;text-anchor:middle}',
        '.value{fill:#3e506d;font-family:Arial,sans-serif;font-size:13px;text-anchor:middle}',
        '.pin{fill:#394b66;font-family:Arial,sans-serif;font-size:10px}',
        '.guide{stroke:#869bb4;stroke-width:1.8;stroke-dasharray:5 7;opacity:.60}',
        '.endpoint{fill:#4c6988}',
        '.hint{font-family:Arial,sans-serif;font-size:13px;fill:#6e7f93}',
        ']]></style>',
        '<text class="hint" x="18" y="27">PLACEMENT PREVIEW · dashed lines are NOT electrical wires</text>',
    ]
    for name, start, end in _net_guides(model, placement):
        x1, y1 = pt(start)
        x2, y2 = pt(end)
        out.append(f'<line class="guide" data-net="{_xml(name)}" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}"/>')

    for cid in sorted(placement.symbols):
        item = placement.symbols[cid]
        comp = model.components[cid]
        symbol = model.symbols[cid]
        cx, cy = pt((item.x, item.y))
        out.append(f'<g data-component="{_xml(cid)}" data-type="{_xml(comp.kind)}">')
        out.append(symbol_body_svg(symbol, cx, cy, item.rotation, item.mirror))
        out.append(f'<text class="name" x="{cx:.2f}" y="{cy + item.title_y*GRID_PX:.2f}">{_xml(cid)}</text>')
        if comp.value:
            out.append(f'<text class="value" x="{cx:.2f}" y="{cy + item.value_y*GRID_PX:.2f}">{_xml(comp.value)}</text>')
        for pin, (x, y) in item.pin_positions.items():
            px, py = pt((x, y))
            out.append(f'<circle class="endpoint" data-pin="{_xml(pin)}" cx="{px:.2f}" cy="{py:.2f}" r="3"/>')
        if len(symbol.pins) > 2:
            for label, lx, ly, anchor, _, _, _ in symbol_pin_labels(symbol, item.rotation, item.mirror):
                out.append(f'<text class="pin" text-anchor="{anchor}" x="{cx+lx:.2f}" y="{cy+ly:.2f}">{_xml(label)}</text>')
        out.append('</g>')
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# LOCAL PREVIEW COMMAND

if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Preview component placement (no electrical wire routing)")
    parser.add_argument("netlist", type=Path, help="JSON netlist file")
    parser.add_argument("output", type=Path, help="Destination SVG file")
    args = parser.parse_args()
    from engine import validate_model

    raw = json.loads(args.netlist.read_text(encoding="utf-8"))
    result = make_placement_preview_svg(validate_model(raw))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8")
    print(f"Saved placement preview: {args.output}")
