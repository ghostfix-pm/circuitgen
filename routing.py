"""Deterministic grid-based Manhattan routing for arbitrary circuit netlists."""

from __future__ import annotations

import dataclasses
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
        for net in sorted(named_nets):
            for ref in sorted(model.nets[net]):
                cid, pin = ref.split('.', 1)
                item, symbol = placement.symbols[cid], model.symbols[cid]
                x, y = self.pins[ref]
                l, t, r, b = _bounds(item, symbol)
                if x < l:
                    side, end = 'left', (x-1.6, y)
                elif x > r:
                    side, end = 'right', (x+1.6, y)
                elif y < t:
                    side, end = 'top', (x, y-1.6)
                elif y > b:
                    side, end = 'bottom', (x, y+1.6)
                else:
                    raise RoutingError(f'Cannot label pin {ref} inside its component')
                end = _pt(end)
                if any(_intersects_rect((x, y), end, rect) for rect in self.rects):
                    raise RoutingError(f'No room for net label at {ref}')
                self.existing[net].append(((x, y), end))
                self.labels.append(NetLabel(net, ref, end, end, side))
        for net, segments in self.existing.items():
            for a, b in segments:
                for position, owner in self.reserved_pins:
                    if owner != net and _segment_clearance(a, b, position, position) <= WIRE_CLEARANCE:
                        raise RoutingError(f'Net label for {net} touches an unrelated pin')
                for c, d, owner in self.pin_leads:
                    if owner != net and _segment_clearance(a, b, c, d) <= WIRE_CLEARANCE:
                        raise RoutingError(f'Net label for {net} touches an unrelated lead')
                for other, their_segments in self.existing.items():
                    if other != net:
                        for c, d in their_segments:
                            if _segment_clearance(a, b, c, d) <= WIRE_CLEARANCE:
                                raise RoutingError(f'Named nets {net} and {other} overlap')
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
            junctions[net] = tuple(sorted(self._p(n) for n, links in adjacent.items()
                                          if len(links) >= 3))
        return RoutedCircuit(self.placement, paths_by_net, junctions,
                             dict(self.terminal_nets), tuple(self.labels))

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


def _route_model_base(model: CircuitModel, placement: Placement | None = None) -> RoutedCircuit:
    """Route every net, or raise rather than drawing a misleading partial circuit."""
    if placement is None:
        placement = place_model(model)
    primary = sorted(model.nets, key=lambda n: (-len(model.nets[n]), n))
    # Dense multi-pin controllers benefit from named power/data rails upfront.
    # Avoid several expensive impossible full-wire attempts.
    controller = any(len(symbol.pins) >= 16 for symbol in model.symbols.values())
    preferred = frozenset(n for n in model.nets if n.upper() in
                          {'GND', 'VCC', 'VDD', 'VSS', '3V3', '5V', 'SDA', 'SCL', 'VIN'})
    if controller and preferred:
        # Keep signal nets physically drawn when possible; use labels only
        # for conventional power rails. Broaden labels if routing fails.
        power = frozenset(n for n in preferred if n.upper() in
                          {'GND', 'VCC', 'VDD', 'VSS', '3V3', '5V', 'VIN'})
        for named in (power, preferred):
            if not named:
                continue
            for margin in (ROUTING_MARGIN, ROUTING_MARGIN+9):
                try:
                    return _MeshRouter(model, placement, margin, named).route(primary)
                except RoutingError:
                    pass
    attempts = (primary, sorted(model.nets), list(reversed(primary)))
    last_error = None
    for order in attempts:
        for margin in (ROUTING_MARGIN, ROUTING_MARGIN+9):
            try:
                return _MeshRouter(model, placement, margin).route(order)
            except RoutingError as error:
                last_error = error
    # Named nets avoid misleading crossings in dense pin arrays.
    if preferred:
        for margin in (ROUTING_MARGIN, ROUTING_MARGIN+9):
            try:
                router = _MeshRouter(model, placement, margin, preferred)
                return router.route(primary)
            except RoutingError as error:
                last_error = error
    raise RoutingError(f'General routing failed without crossing unrelated nets: {last_error}')


def route_model(model: CircuitModel, placement: Placement | None = None,
                *, optimize: bool = False) -> RoutedCircuit:
    """Route a netlist. Optional improvement never accepts an unverified layout."""
    result = _route_model_base(model, placement)
    if optimize:
        from optimization import choose_better_layout
        return choose_better_layout(model, result, _route_model_base)
    return result


def make_routed_svg(model: CircuitModel, *, optimize: bool = False) -> str:
    """Use historical motif rendering only for exact matches; otherwise route normally."""
    from motifs import render_motif
    specialized = render_motif(model)
    if specialized is not None:
        return specialized
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
        out.append(f'<text class="netlabel" data-net="{xml(label.net)}" data-pin="{xml(label.ref)}" text-anchor="{anchor}" x="{x:.2f}" y="{y:.2f}">{xml(label.net)}</text>')
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
    return '\n'.join(out)+'\n'


if __name__ == '__main__':
    import argparse
    import json
    from engine import validate_model
    parser = argparse.ArgumentParser(description='Render a verified routed SVG schematic')
    parser.add_argument('netlist', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--optimize', action='store_true', help='Try verified compact placement candidates')
    args = parser.parse_args()
    data = json.loads(args.netlist.read_text(encoding='utf-8'))
    artwork = make_routed_svg(validate_model(data), optimize=args.optimize)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(artwork, encoding='utf-8')
    print(f'Saved routed schematic: {args.output}')
