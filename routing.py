"""Deterministic grid-based Manhattan routing for arbitrary circuit netlists."""

from __future__ import annotations

import dataclasses
import re
import heapq
import itertools
import math
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xml_escape

from placement import Placement, place_model
from symbols import GRID_PX, symbol_body_svg, symbol_pin_labels, transform_point

if TYPE_CHECKING:
    from engine import CircuitModel

BODY_CLEARANCE = 0.20
WIRE_CLEARANCE = 0.14
BEND_PENALTY = 1.1
ROUTING_MARGIN = 11.0
MAX_TRACK_NODES = 32000
MAX_ROUTING_STATES = 550000
Point = tuple[float, float]
Segment = tuple[Point, Point]


class RoutingError(ValueError):
    """A net could not be routed without hitting a symbol or unrelated net."""


@dataclasses.dataclass(frozen=True)
class NetLabel:
    net: str
    ref: str
    endpoint: Point
    label_at: Point
    side: str


@dataclasses.dataclass(frozen=True)
class RoutedCircuit:
    placement: Placement
    paths: dict[str, tuple[tuple[Point, ...], ...]]
    junctions: dict[str, tuple[Point, ...]]
    pin_to_net: dict[str, str]
    labels: tuple[NetLabel, ...] = ()


def _pt(p: Point) -> Point:
    return round(p[0], 6), round(p[1], 6)


def _bounds(item, symbol) -> tuple[float, float, float, float]:
    corners = [transform_point(i * symbol.half_width, j * symbol.half_height,
                               item.rotation, item.mirror)
               for i in (-1, 1) for j in (-1, 1)]
    return (item.x + min(x for x, _ in corners),
            item.y + min(y for _, y in corners),
            item.x + max(x for x, _ in corners),
            item.y + max(y for _, y in corners))


def _intersects_rect(a: Point, b: Point, rect) -> bool:
    left, top, right, bottom = rect
    e = 1e-7
    if abs(a[1]-b[1]) < e:
        return top-e <= a[1] <= bottom+e and max(a[0], b[0]) >= left-e and min(a[0], b[0]) <= right+e
    return left-e <= a[0] <= right+e and max(a[1], b[1]) >= top-e and min(a[1], b[1]) <= bottom+e


def _segment_clearance(a: Point, b: Point, c: Point, d: Point) -> float:
    aminx, amaxx = sorted((a[0], b[0])); aminy, amaxy = sorted((a[1], b[1]))
    bminx, bmaxx = sorted((c[0], d[0])); bminy, bmaxy = sorted((c[1], d[1]))
    dx = max(0, bminx-amaxx, aminx-bmaxx)
    dy = max(0, bminy-amaxy, aminy-bmaxy)
    return math.hypot(dx, dy)


def _rects_overlap(a, b) -> bool:
    """Conservative overlap test for label text, symbols and other labels."""
    return not (a[2] <= b[0] or b[2] <= a[0] or
                a[3] <= b[1] or b[3] <= a[1])


def _net_label_box(net: str, endpoint: Point, side: str):
    """Approximate the 12px net-label text in routing-grid coordinates.

    Keep text clear of symbols and other labels; wire-under-text may be
    visually knocked out by the SVG halo, without breaking electrical wires.
    """
    x, y = endpoint
    width = max(0.38, 0.36 * len(net))
    if side == 'left':
        return x - 0.20 - width, y - 0.80, x - 0.20, y + 0.10
    if side == 'right':
        return x + 0.20, y - 0.80, x + 0.20 + width, y + 0.10
    if side == 'top':
        return x - width/2, y - 0.95, x + width/2, y - 0.24
    return x - width/2, y + 0.18, x + width/2, y + 0.90


def _segments(points):
    return tuple(zip(points, points[1:]))


def _compress(points: tuple[Point, ...]) -> tuple[Point, ...]:
    if len(points) <= 2:
        return points
    result = [points[0]]
    for a, b, c in zip(points, points[1:], points[2:]):
        if not ((abs(a[0]-b[0]) < 1e-6 and abs(b[0]-c[0]) < 1e-6) or
                (abs(a[1]-b[1]) < 1e-6 and abs(b[1]-c[1]) < 1e-6)):
            result.append(b)
    return tuple(result + [points[-1]])


class _MeshRouter:
    def __init__(self, model: CircuitModel, placement: Placement, margin: float,
                 named_nets: frozenset[str] = frozenset()):
        self.model = model
        self.placement = placement
        self.named_nets = named_nets
        self.pins = {f'{cid}.{pin}': _pt(pos)
                     for cid, item in placement.symbols.items()
                     for pin, pos in item.pin_positions.items()}
        self.terminal_nets = {ref: name for name, refs in model.nets.items() for ref in refs}
        extents = [(p.x-p.half_width, p.y-p.half_height,
                    p.x+p.half_width, p.y+p.half_height)
                   for p in placement.symbols.values()]
        minx = min(a for a, _, _, _ in extents)-margin
        maxx = max(c for _, _, c, _ in extents)+margin
        miny = min(b for _, b, _, _ in extents)-margin
        maxy = max(d for _, _, _, d in extents)+margin
        xs = set(range(math.floor(minx), math.ceil(maxx)+1))
        ys = set(range(math.floor(miny), math.ceil(maxy)+1))
        for x, y in self.pins.values():
            xs.update((x, round(x-0.5, 6), round(x+0.5, 6)))
            ys.update((y, round(y-0.5, 6), round(y+0.5, 6)))
        self.xs, self.ys = sorted(xs), sorted(ys)
        self.width, self.height = len(self.xs), len(self.ys)
        if self.width*self.height > MAX_TRACK_NODES:
            raise RoutingError('Circuit exceeds router grid limits')
        xidx = {x: i for i, x in enumerate(self.xs)}
        yidx = {y: i for i, y in enumerate(self.ys)}
        self.pin_nodes = {ref: (xidx[p[0]], yidx[p[1]]) for ref, p in self.pins.items()}
        self.rects = []
        for cid, item in placement.symbols.items():
            symbol = model.symbols[cid]
            l, t, r, b = _bounds(item, symbol)
            self.rects.append((l-BODY_CLEARANCE, t-BODY_CLEARANCE,
                               r+BODY_CLEARANCE, b+BODY_CLEARANCE))
            tw = max(0.48, len(cid)*10.5/GRID_PX/2)
            tx = item.x + item.title_x
            if item.label_anchor == 'start':
                bounds = (tx-0.15, tx+tw*2+0.15)
            else:
                bounds = (tx-tw-0.2, tx+tw+0.2)
            self.rects.append((bounds[0], item.y+item.title_y-17/GRID_PX,
                               bounds[1], item.y+item.title_y+5/GRID_PX))
            value = model.components[cid].value
            if value:
                vw = max(0.4, len(value)*7.8/GRID_PX/2)
                vx = item.x + item.value_x
                if item.label_anchor == 'start':
                    bounds = (vx-0.15, vx+vw*2+0.15)
                else:
                    bounds = (vx-vw-0.2, vx+vw+0.2)
                self.rects.append((bounds[0], item.y+item.value_y-14/GRID_PX,
                                   bounds[1], item.y+item.value_y+5/GRID_PX))
        # Every pin is reserved, including deliberately unconnected pins.
        self.reserved_pins = tuple((pos, self.terminal_nets.get(ref))
                                   for ref, pos in self.pins.items())
        self.pin_leads = []
        for cid, item in placement.symbols.items():
            symbol = model.symbols[cid]
            l, t, r, b = _bounds(item, symbol)
            for pin, endpoint in item.pin_positions.items():
                endpoint = _pt(endpoint)
                x, y = endpoint
                if x < l: body_point = (l, y)
                elif x > r: body_point = (r, y)
                elif y < t: body_point = (x, t)
                elif y > b: body_point = (x, b)
                else: continue
                self.pin_leads.append((endpoint, body_point,
                                       self.terminal_nets.get(f'{cid}.{pin}')))
        self.existing: dict[str, list[Segment]] = defaultdict(list)
        self.labels: list[NetLabel] = []
        occupied_text = []
        self.label_boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        for net in sorted(named_nets):
            for ref in sorted(model.nets[net]):
                cid, _ = ref.split('.', 1)
                item, symbol = placement.symbols[cid], model.symbols[cid]
                start = self.pins[ref]
                x, y = start
                l, t, r, b = _bounds(item, symbol)
                if x < l:
                    side, dx, dy = 'left', -1, 0
                elif x > r:
                    side, dx, dy = 'right', 1, 0
                elif y < t:
                    side, dx, dy = 'top', 0, -1
                elif y > b:
                    side, dx, dy = 'bottom', 0, 1
                else:
                    raise RoutingError(f'Cannot label pin {ref} inside its component')
                # Start with a readable stub, then try longer or shorter ones
                # if another component, text, pin or named net obstructs it.
                choice = None
                for length in (1.6, 2.4, 0.95, 3.2, 0.55):
                    end = _pt((x + dx*length, y + dy*length))
                    box = _net_label_box(net, end, side)
                    if any(_intersects_rect(start, end, rect) or
                           _rects_overlap(box, rect) for rect in self.rects):
                        continue
                    if any(_rects_overlap(box, old) for old in occupied_text):
                        continue
                    # No named stub may touch an unrelated physical pin/lead.
                    if any(owner != net and
                           _segment_clearance(start, end, pos, pos) <= WIRE_CLEARANCE
                           for pos, owner in self.reserved_pins):
                        continue
                    if any(owner != net and
                           _segment_clearance(start, end, lead, body) <= WIRE_CLEARANCE
                           for lead, body, owner in self.pin_leads):
                        continue
                    if any(other != net and
                           _segment_clearance(start, end, a, b) <= WIRE_CLEARANCE
                           for other, lines in self.existing.items() for a, b in lines):
                        continue
                    # Prevent another label's stub running through this text.
                    if any(_intersects_rect(a, b, box)
                           for other, lines in self.existing.items()
                           for a, b in lines):
                        continue
                    choice = (end, box)
                    break
                if choice is None:
                    raise RoutingError(f'No collision-free net label at {ref} ({net})')
                end, box = choice
                self.existing[net].append((start, end))
                self.labels.append(NetLabel(net, ref, end, end, side))
                occupied_text.append(box)
                self.label_boxes.append((net, box))
        # The checks above are geometric checks, not a license to connect
        # unrelated nets. Verify every drawn path after routing below.
        self.edge_geometry_clear = lru_cache(maxsize=200000)(self._edge_geometry_clear)

    def _p(self, node):
        return self.xs[node[0]], self.ys[node[1]]

    def _edge_geometry_clear(self, u, v):
        a, b = self._p(u), self._p(v)
        return not any(_intersects_rect(a, b, rect) for rect in self.rects)

    def _valid(self, u, v, net):
        key = (u, v) if u < v else (v, u)
        if not self.edge_geometry_clear(*key):
            return False
        a, b = self._p(u), self._p(v)
        # Text knockout is for readability, not permission to route an
        # unrelated signal directly behind a named-net label.
        if any(owner != net and _intersects_rect(a, b, box)
               for owner, box in self.label_boxes):
            return False
        for point, owner in self.reserved_pins:
            if owner != net and _segment_clearance(a, b, point, point) <= WIRE_CLEARANCE:
                return False
        for endpoint, body_point, owner in self.pin_leads:
            if owner != net and _segment_clearance(a, b, endpoint, body_point) <= WIRE_CLEARANCE:
                return False
        for owner, segments in self.existing.items():
            if owner != net:
                for c, d in segments:
                    if _segment_clearance(a, b, c, d) <= WIRE_CLEARANCE:
                        return False
        return True

    def _neighbors(self, node):
        x, y = node
        if x: yield (x-1, y), 0
        if x+1 < self.width: yield (x+1, y), 0
        if y: yield (x, y-1), 1
        if y+1 < self.height: yield (x, y+1), 1

    def _search(self, start, goals, net):
        if start in goals:
            return (start,)
        gx0, gx1 = min(n[0] for n in goals), max(n[0] for n in goals)
        gy0, gy1 = min(n[1] for n in goals), max(n[1] for n in goals)
        def heuristic(node):
            x, y = self._p(node)
            return max(self.xs[gx0]-x, 0, x-self.xs[gx1]) + max(self.ys[gy0]-y, 0, y-self.ys[gy1])
        seq = itertools.count()
        frontier = [(heuristic(start), 0.0, next(seq), start, -1)]
        distance = {(start, -1): 0.0}
        previous = {}
        expanded = 0
        while frontier:
            _, cost, _, node, direction = heapq.heappop(frontier)
            state = (node, direction)
            if cost > distance[state]+1e-8:
                continue
            if node in goals:
                result = [node]
                while state in previous:
                    state = previous[state]
                    result.append(state[0])
                return tuple(reversed(result))
            expanded += 1
            if expanded > MAX_ROUTING_STATES:
                raise RoutingError(f'Routing search limit exceeded on net {net}')
            for nxt, d in self._neighbors(node):
                if not self._valid(node, nxt, net):
                    continue
                a, b = self._p(node), self._p(nxt)
                cost2 = cost + abs(a[0]-b[0]) + abs(a[1]-b[1])
                if direction not in (-1, d):
                    cost2 += BEND_PENALTY
                newstate = (nxt, d)
                if cost2 < distance.get(newstate, float('inf'))-1e-8:
                    distance[newstate] = cost2
                    previous[newstate] = state
                    heapq.heappush(frontier, (cost2+heuristic(nxt), cost2,
                                              next(seq), nxt, d))
        raise RoutingError(f'No collision-free path for net {net}')

    def route(self, order):
        paths_by_net = {}
        raw_by_net = {}
        for net in order:
            if net in self.named_nets:
                paths_by_net[net] = tuple(((self.pins[label.ref], label.endpoint)
                                           for label in self.labels if label.net == net))
                raw_by_net[net] = []
                continue
            refs = sorted(self.model.nets[net])
            nodes = {self.pin_nodes[ref] for ref in refs}
            if len(nodes) <= 1:
                paths_by_net[net] = ()
                raw_by_net[net] = []
                continue
            tree = {self.pin_nodes[refs[0]]}
            remaining = nodes-tree
            raw_paths = []
            while remaining:
                target = min(remaining, key=lambda node: (
                    min(abs(self.xs[node[0]]-self.xs[c[0]]) +
                        abs(self.ys[node[1]]-self.ys[c[1]]) for c in tree), node))
                path = self._search(target, tree, net)
                raw_paths.append(path)
                tree.update(path)
                remaining -= tree
            raw_by_net[net] = raw_paths
            geometries = [tuple(self._p(node) for node in path) for path in raw_paths]
            paths_by_net[net] = tuple(_compress(path) for path in geometries)
            for path in geometries:
                self.existing[net].extend(_segments(_compress(path)))
        self._verify(raw_by_net, paths_by_net)
        junctions = {}
        for net, paths in raw_by_net.items():
            adjacent = defaultdict(set)
            for path in paths:
                for a, b in _segments(path):
                    adjacent[a].add(b); adjacent[b].add(a)
            # A component lead is another branch at a pin: if two external
            # wire segments meet at that terminal, render the junction dot.
            pins_for_net = {self.pin_nodes[ref] for ref in self.model.nets[net]}
            junctions[net] = tuple(sorted(self._p(n) for n, links in adjacent.items()
                                          if len(links) >= 3 or
                                          (len(links) >= 2 and n in pins_for_net)))
        result = RoutedCircuit(self.placement, paths_by_net, junctions,
                               dict(self.terminal_nets), tuple(self.labels))
        verify_display_connectivity(self.model, result)
        return result

    def _verify(self, raw_paths, paths_by_net):
        for net, refs in self.model.nets.items():
            if net in self.named_nets:
                if {label.ref for label in self.labels if label.net == net} != set(refs):
                    raise RoutingError(f'Missing named-net terminal on {net}')
                continue
            pins = {self.pin_nodes[ref] for ref in refs}
            if len(pins) <= 1:
                continue
            links = defaultdict(set)
            for path in raw_paths[net]:
                for a, b in _segments(path):
                    links[a].add(b); links[b].add(a)
            reached = {min(pins)}
            pending = [min(pins)]
            while pending:
                for next_node in links[pending.pop()]:
                    if next_node not in reached:
                        reached.add(next_node)
                        pending.append(next_node)
            if not pins.issubset(reached):
                raise RoutingError(f'Connectivity validation failed on net {net}')
        segments = [(net, a, b) for net, paths in paths_by_net.items()
                    for path in paths for a, b in _segments(path)]
        for i, (net, a, b) in enumerate(segments):
            if any(owner != net and _intersects_rect(a, b, box)
                   for owner, box in self.label_boxes):
                raise RoutingError(f'Wire from {net} obscures a different net label')
            if any(_intersects_rect(a, b, rect) for rect in self.rects):
                raise RoutingError(f'Wire touches a component body on net {net}')
            for pos, owner in self.reserved_pins:
                if owner != net and _segment_clearance(a, b, pos, pos) <= WIRE_CLEARANCE:
                    raise RoutingError(f'Wire from {net} touches an unrelated pin')
            for endpoint, body_point, owner in self.pin_leads:
                if owner != net and _segment_clearance(a, b, endpoint, body_point) <= WIRE_CLEARANCE:
                    raise RoutingError(f'Wire from {net} touches an unrelated pin lead')
            for other, c, d in segments[i+1:]:
                if net != other and _segment_clearance(a, b, c, d) <= WIRE_CLEARANCE:
                    raise RoutingError(f'Wires from {net} and {other} intersect')



def verify_display_connectivity(model: CircuitModel, routed: RoutedCircuit) -> None:
    """Independent geometric audit of the paths that will actually be drawn.

    Unlike `_MeshRouter._verify`, this checks the *compressed displayed paths*
    directly. A net's wires must form one connected geometric component, unless
    every disconnected part has an explicit, matching net label. Unrelated nets
    cannot touch, cross, or pass through each other's terminals.

    This is a connectivity/geometry audit, not a SPICE behavior simulation.
    """
    expected = {ref: net for net, refs in model.nets.items() for ref in refs}
    if routed.pin_to_net != expected:
        raise RoutingError('Display pin-to-net mapping differs from original netlist')
    if set(routed.paths) != set(model.nets):
        raise RoutingError('Displayed set of nets differs from the original netlist')
    if set(routed.placement.symbols) != set(model.components):
        raise RoutingError('Displayed component set differs from the original netlist')

    pin_positions = {f'{cid}.{pin}': _pt(pos)
                     for cid, part in routed.placement.symbols.items()
                     for pin, pos in part.pin_positions.items()}
    labelled = defaultdict(set)
    for label in routed.labels:
        if label.net not in model.nets or label.ref not in model.nets[label.net]:
            raise RoutingError(f'Incorrect displayed net label: {label.net} at {label.ref}')
        if label.ref in labelled[label.net]:
            raise RoutingError(f'Duplicate net label at {label.ref}')
        labelled[label.net].add(label.ref)
        # The label's stub must be explicitly present in the displayed paths.
        pin = pin_positions[label.ref]
        if not any(len(path) >= 2 and _pt(path[0]) == pin and
                   _pt(path[-1]) == _pt(label.endpoint)
                   for path in routed.paths[label.net]):
            raise RoutingError(f'Net label at {label.ref} has no drawn stub')

    all_segments: list[tuple[str, Point, Point]] = []
    for net, paths in routed.paths.items():
        for path in paths:
            if len(path) < 2:
                raise RoutingError(f'Invalid displayed path on {net}')
            for a, b in _segments(path):
                if not all(math.isfinite(v) for v in (*a, *b)):
                    raise RoutingError(f'Non-finite displayed wire on {net}')
                if not (abs(a[0]-b[0]) <= 1e-5 or abs(a[1]-b[1]) <= 1e-5):
                    raise RoutingError(f'Diagonal displayed wire on {net}')
                if _pt(a) != _pt(b):
                    all_segments.append((net, a, b))
    # Recheck disjoint nets independently of the router's occupancy bookkeeping.
    for i, (name, a, b) in enumerate(all_segments):
        for other, c, d in all_segments[i+1:]:
            if name != other and _segment_clearance(a, b, c, d) <= WIRE_CLEARANCE:
                raise RoutingError(f'Displayed wires from {name} and {other} touch')
        for ref, pos in pin_positions.items():
            owner = expected.get(ref)
            if owner != name and _segment_clearance(a, b, pos, pos) <= WIRE_CLEARANCE:
                raise RoutingError(f'Displayed wire {name} touches unrelated pin {ref}')

    for net, refs in model.nets.items():
        segments = [(a, b) for owner, a, b in all_segments if owner == net]
        if not segments:
            if len(refs) > 1:
                raise RoutingError(f'No displayed connections for net {net}')
            continue
        # Each displayed segment is a graph vertex. Same-net segments physically
        # intersecting (including a T-junction on a long compressed line) merge.
        parents = list(range(len(segments)))
        def find(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]
                i = parents[i]
            return i
        def union(i, j):
            parents[find(i)] = find(j)
        for i, (a, b) in enumerate(segments):
            for j in range(i+1, len(segments)):
                c, d = segments[j]
                if _segment_clearance(a, b, c, d) < 1e-5:
                    union(i, j)
        # Every source terminal must touch its declared wire or a net label.
        groups = defaultdict(set)
        for ref in refs:
            point = pin_positions[ref]
            touching = [i for i, (a, b) in enumerate(segments)
                        if _segment_clearance(a, b, point, point) < 1e-5]
            if not touching:
                raise RoutingError(f'Pin {ref} is not connected in displayed geometry')
            roots = {find(i) for i in touching}
            if len(roots) > 1:
                raise RoutingError(f'Ambiguous unconnected paths at pin {ref}')
            groups[next(iter(roots))].add(ref)
        if len(groups) > 1:
            # Label continuity is a virtual connection between isolated stubs.
            # A label on only one disconnected group would be misleading.
            if any(not (group & labelled[net]) for group in groups.values()):
                raise RoutingError(f'Unlabelled disconnection in displayed net {net}')
            if set(refs) != labelled[net]:
                raise RoutingError(f'Incomplete labelled connections for net {net}')
        elif labelled[net] and labelled[net] != set(refs):
            raise RoutingError(f'Partial net labels on {net}')



def _expanded_placement(placement: Placement, scale: float) -> Placement:
    """Expand center-to-center spacing while preserving individual symbol size.

    Merely enlarging the SVG viewBox cannot create routing channels. This
    modifies actual placements and pin endpoints without moving pins relative
    to their components or changing the circuit.
    """
    ox = min(item.x for item in placement.symbols.values())
    oy = min(item.y for item in placement.symbols.values())
    moved = {}
    for cid, item in placement.symbols.items():
        nx = round(ox + (item.x - ox)*scale, 6)
        ny = round(oy + (item.y - oy)*scale, 6)
        dx, dy = nx-item.x, ny-item.y
        moved[cid] = dataclasses.replace(
            item, x=nx, y=ny,
            pin_positions={pin: _pt((x+dx, y+dy))
                           for pin, (x, y) in item.pin_positions.items()}
        )
    return dataclasses.replace(placement, symbols=moved)


def _route_model_base(model: CircuitModel, placement: Placement | None = None) -> RoutedCircuit:
    """Route locally before long power rails; label only genuinely difficult nets.

    Every successful result undergoes an independent check of its *drawn*
    geometry, not merely the validated input netlist. The fallback is explicit
    and bounded, with no changes to electrical connectivity or wire collisions.
    """
    if placement is None:
        placement = place_model(model)
    pins = {ref: _pt(placement.symbols[ref.split('.', 1)[0]].pin_positions[
                     ref.split('.', 1)[1]])
            for refs in model.nets.values() for ref in refs}

    def wire_span(net):
        locations = [pins[ref] for ref in model.nets[net]]
        if len(locations) < 2:
            return 0.0
        return (max(x for x, y in locations) - min(x for x, y in locations) +
                max(y for x, y in locations) - min(y for x, y in locations))

    # Local two-pin signal connections should be committed before wide buses.
    local_first = sorted(model.nets, key=lambda n: (wire_span(n), len(model.nets[n]), n))
    wide_first = sorted(model.nets, key=lambda n: (-len(model.nets[n]), n))
    power_names = {'GND', 'VCC', 'VDD', 'VSS', '3V3', '5V', 'VIN', 'VBAT', 'AGND', 'DGND'}
    power = frozenset(n for n in model.nets if n.upper() in power_names)
    errors = []

    def try_route(order, named):
        return _MeshRouter(model, placement, ROUTING_MARGIN, frozenset(named)).route(order)

    # Trying the short connections first drastically improves IC layouts; the
    # second order still handles unusual high-fanout and parallel geometries.
    for order in (local_first, wide_first):
        try:
            return try_route(order, ())
        except RoutingError as error:
            errors.append('all-wire: ' + str(error))

    # Power labels are conventional, unlike using labels on every signal path.
    # We try more than one order, but stop once a fully signal-wired plan works.
    starts = (power, frozenset()) if power else (frozenset(),)
    for start in starts:
        named = set(start)
        for _ in range(len(model.nets) + 1):
            failure = None
            for order in (local_first, wide_first):
                try:
                    return try_route(order, named)
                except RoutingError as error:
                    errors.append(f'named={sorted(named)}: {error}')
                    if failure is None:
                        failure = error
            # Name the genuinely unreachable signal, then retry remaining local
            # wires. If labels themselves collide, try a different layout.
            match = re.fullmatch(
                r'(?:No collision-free path for net|Routing search limit exceeded on net) (.+)',
                str(failure)
            ) if failure else None
            if not match or match.group(1) not in model.nets or match.group(1) in named:
                break
            named.add(match.group(1))

    raise RoutingError('Unable to construct an electrically verified schematic. ' +
                       ' | '.join(errors[-4:]))


def route_model(model: CircuitModel, placement: Placement | None = None,
                *, optimize: bool = False) -> RoutedCircuit:
    """Route a netlist. Optional improvement never accepts an unverified layout."""
    if placement is not None:
        # Callers supplying placement explicitly control the geometry.
        result = _route_model_base(model, placement)
    else:
        original = place_model(model)
        last_error = None
        for candidate in (original, _expanded_placement(original, 1.25),
                          _expanded_placement(original, 1.5)):
            try:
                result = _route_model_base(model, candidate)
                break
            except RoutingError as error:
                last_error = error
        else:
            raise RoutingError(f'Routing failed after spacing expansion: {last_error}')
    if optimize:
        from optimization import choose_better_layout
        return choose_better_layout(model, result, _route_model_base)
    return result


def make_routed_svg(model: CircuitModel, *, optimize: bool = False, color: str = "#000000") -> str:
    """Use historical motif rendering only for exact matches; otherwise route normally."""
    from motifs import render_motif
    from colors import apply_ink_color
    specialized = render_motif(model)
    if specialized is not None:
        return apply_ink_color(specialized, color)
    result = route_model(model, optimize=optimize)
    placed = result.placement
    bounds = []
    for item in placed.symbols.values():
        bounds.extend(((item.x-item.half_width, item.y-item.half_height),
                       (item.x+item.half_width, item.y+item.half_height)))
    for paths in result.paths.values():
        for path in paths:
            bounds.extend(path)
    for label in result.labels:
        x, y = label.endpoint
        size = 0.34*len(label.net)+0.5
        if label.side == 'left': bounds.extend(((x-size, y-0.65), (x, y+0.5)))
        elif label.side == 'right': bounds.extend(((x, y-0.65), (x+size, y+0.5)))
        elif label.side == 'top': bounds.extend(((x-size/2, y-1), (x+size/2, y)))
        else: bounds.extend(((x-size/2, y), (x+size/2, y+1)))
    x0 = min(x for x, _ in bounds)*GRID_PX-26
    y0 = min(y for _, y in bounds)*GRID_PX-26
    x1 = max(x for x, _ in bounds)*GRID_PX+26
    y1 = max(y for _, y in bounds)*GRID_PX+26
    def loc(p):
        return p[0]*GRID_PX-x0, p[1]*GRID_PX-y0
    def xml(value):
        return xml_escape(value, {'"': '&quot;', "'": '&apos;'})
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x1-x0:.1f} {y1-y0:.1f}" width="{x1-x0:.1f}" height="{y1-y0:.1f}">',
           '<title>SchemAI routed schematic</title>',
           '<style><![CDATA[',
           '.symbol{fill:none;stroke:#213149;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}',
           '.mark{fill:#213149;font-family:Arial,sans-serif;font-size:16px;text-anchor:middle}',
           '.name{fill:#17243c;font-family:Arial,sans-serif;font-size:17px;font-weight:700;text-anchor:middle}',
           '.value{fill:#3e506d;font-family:Arial,sans-serif;font-size:13px;text-anchor:middle}',
           '.pin{fill:#394b66;font-family:Arial,sans-serif;font-size:10px}',
           '.wire{fill:none;stroke:#264a6f;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}',
           '.junction{fill:#264a6f}',
           '.netlabel{fill:#176b75;font-family:Arial,sans-serif;font-size:12px;font-weight:700}',
           ']]></style>']
    # Mask wire ink, not the SVG background. Black glyphs in the mask make
    # genuinely transparent gaps beneath net labels on any background.
    label_drawings = []
    for label in result.labels:
        x, y = loc(label.endpoint)
        if label.side == 'left':
            x -= 4; y -= 4; anchor = 'end'
        elif label.side == 'right':
            x += 4; y -= 4; anchor = 'start'
        elif label.side == 'top':
            y -= 8; anchor = 'middle'
        else:
            y += 18; anchor = 'middle'
        markup = (f'data-net="{xml(label.net)}" data-pin="{xml(label.ref)}" '
                  f'text-anchor="{anchor}" x="{x:.2f}" y="{y:.2f}"')
        label_drawings.append((markup, xml(label.net)))
    if label_drawings:
        out.append('<defs><mask id="schemai-label-cutout" maskUnits="userSpaceOnUse" '
                   f'x="0" y="0" width="{x1-x0:.1f}" height="{y1-y0:.1f}">')
        out.append(f'<rect x="0" y="0" width="{x1-x0:.1f}" height="{y1-y0:.1f}" fill="white"/>')
        for markup, value in label_drawings:
            out.append(f'<text class="netlabel" {markup} fill="black" stroke="black" '
                       f'stroke-width="5" stroke-linejoin="round" '
                       f'style="fill:black;stroke:black">{value}</text>')
        out.append('</mask></defs>')
        out.append('<g mask="url(#schemai-label-cutout)">')
    for net in sorted(result.paths):
        for path in result.paths[net]:
            if len(path) < 2:
                continue
            coords = [loc(p) for p in path]
            d = 'M ' + ' L '.join(f'{x:.2f} {y:.2f}' for x, y in coords)
            out.append(f'<path class="wire" data-net="{xml(net)}" d="{d}"/>')
    for net in sorted(result.junctions):
        for p in result.junctions[net]:
            x, y = loc(p)
            out.append(f'<circle class="junction" data-net="{xml(net)}" cx="{x:.2f}" cy="{y:.2f}" r="4.2"/>')
    if label_drawings:
        out.append('</g>')
    for markup, value in label_drawings:
        out.append(f'<text class="netlabel" {markup}>{value}</text>')
    for cid in sorted(placed.symbols):
        item = placed.symbols[cid]
        comp = model.components[cid]
        symbol = model.symbols[cid]
        cx, cy = loc((item.x, item.y))
        out.append(f'<g data-component="{xml(cid)}" data-type="{xml(comp.kind)}">')
        out.append(symbol_body_svg(symbol, cx, cy, item.rotation, item.mirror))
        out.append(f'<text class="name" text-anchor="{item.label_anchor}" x="{cx+item.title_x*GRID_PX:.2f}" y="{cy+item.title_y*GRID_PX:.2f}">{xml(cid)}</text>')
        if comp.value:
            out.append(f'<text class="value" text-anchor="{item.label_anchor}" x="{cx+item.value_x*GRID_PX:.2f}" y="{cy+item.value_y*GRID_PX:.2f}">{xml(comp.value)}</text>')
        if len(symbol.pins) > 2:
            if symbol.shape == 'ic' and item.rotation in (0, 180):
                # Names inside the IC body leave outside pin leads unobstructed.
                left, top, right, bottom = _bounds(item, symbol)
                for pin in symbol.pins:
                    px, py = item.pin_positions[pin.name]
                    label = pin.name + (f' ({pin.number})' if pin.number else '')
                    if px < left:
                        lx, ly, anchor = cx-symbol.half_width*GRID_PX+6, (py-item.y)*GRID_PX+3, 'start'
                    elif px > right:
                        lx, ly, anchor = cx+symbol.half_width*GRID_PX-6, (py-item.y)*GRID_PX+3, 'end'
                    elif py < top:
                        lx, ly, anchor = cx+(px-item.x)*GRID_PX, -symbol.half_height*GRID_PX+13, 'middle'
                    elif py > bottom:
                        lx, ly, anchor = cx+(px-item.x)*GRID_PX, symbol.half_height*GRID_PX-7, 'middle'
                    else:
                        continue
                    out.append(f'<text class="pin" text-anchor="{anchor}" x="{lx:.2f}" y="{cy+ly:.2f}">{xml(label)}</text>')
            else:
                for label, lx, baseline, anchor, side, _, _ in symbol_pin_labels(symbol, item.rotation, item.mirror):
                    if side in ('left', 'right'):
                        baseline -= 8
                    out.append(f'<text class="pin" text-anchor="{anchor}" x="{cx+lx:.2f}" y="{cy+baseline:.2f}">{xml(label)}</text>')
        out.append('</g>')
    out.append('</svg>')
    return apply_ink_color('\n'.join(out)+'\n', color)


if __name__ == '__main__':
    import argparse
    import json
    from engine import validate_model
    parser = argparse.ArgumentParser(description='Render a verified routed SVG schematic')
    parser.add_argument('netlist', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--color', default='#000000', help='Ink color, e.g. #000000 or #cc0000')
    parser.add_argument('--optimize', action='store_true', help='Try verified compact placement candidates')
    args = parser.parse_args()
    data = json.loads(args.netlist.read_text(encoding='utf-8'))
    artwork = make_routed_svg(validate_model(data), optimize=args.optimize, color=args.color)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(artwork, encoding='utf-8')
    print(f'Saved routed schematic: {args.output}')
