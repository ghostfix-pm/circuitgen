"""Exact-match, conventional SVG layouts for a small set of famous circuits.

A motif activates only when the entire validated netlist matches the named
connectivity pattern. All other circuits use the existing layout and router.
"""

from __future__ import annotations

import dataclasses
import math
from xml.sax.saxutils import escape as xml_escape

from symbols import GRID_PX, symbol_body_svg


@dataclasses.dataclass(frozen=True)
class Wheatstone:
    source: str
    upper_left: str
    lower_left: str
    upper_right: str
    lower_right: str
    bridge: str
    top: str
    bottom: str
    left: str
    right: str


@dataclasses.dataclass(frozen=True)
class Rectifier:
    source: str
    upper_left: str
    lower_left: str
    upper_right: str
    lower_right: str
    load: str
    ac_left: str
    ac_right: str
    dc_plus: str
    dc_minus: str


def _complete(model) -> bool:
    """No missing pins, doubled nets, or unconnected terminals in a motif."""
    return all(len(part.pins) == len(model.symbols[cid].pins)
               for cid, part in model.components.items())


def match_wheatstone(model) -> Wheatstone | None:
    """Five resistors, four three-terminal nets, and a DC supply across arms."""
    parts = model.components
    if len(parts) != 6 or len(model.nets) != 4 or not _complete(model):
        return None
    supply = [part for part in parts.values() if part.kind in ('battery', 'dc_source')]
    arms = [part for part in parts.values() if part.kind == 'resistor']
    if len(supply) != 1 or len(arms) != 5:
        return None
    source = supply[0]
    top, bottom = source.pins.get('p'), source.pins.get('n')
    if not top or not bottom or top == bottom:
        return None
    midpoints = set(model.nets) - {top, bottom}
    if len(midpoints) != 2 or any(len(refs) != 3 for refs in model.nets.values()):
        return None
    middle = [part for part in arms if set(part.pins.values()) == midpoints]
    if len(middle) != 1:
        return None
    cross = middle[0]
    paths = []
    for net in midpoints:
        upper = [part for part in arms if part.cid != cross.cid
                 and set(part.pins.values()) == {top, net}]
        lower = [part for part in arms if part.cid != cross.cid
                 and set(part.pins.values()) == {bottom, net}]
        if len(upper) != 1 or len(lower) != 1:
            return None
        paths.append((upper[0].cid, lower[0].cid, net))
    if len({item for a, b, _ in paths for item in (a, b)}) != 4:
        return None
    paths.sort(key=lambda p: (p[0], p[1]))
    (ul, ll, left), (ur, lr, right) = paths
    return Wheatstone(source.cid, ul, ll, ur, lr, cross.cid,
                      top, bottom, left, right)


def match_rectifier(model) -> Rectifier | None:
    """Four polarized diodes, AC source, and a DC load; reject near matches."""
    parts = model.components
    if len(parts) != 6 or len(model.nets) != 4 or not _complete(model):
        return None
    sources = [part for part in parts.values() if part.kind == 'ac_source']
    diodes = [part for part in parts.values() if part.kind == 'diode']
    loads = [part for part in parts.values() if part.kind == 'resistor']
    if len(sources) != 1 or len(diodes) != 4 or len(loads) != 1:
        return None
    src, load = sources[0], loads[0]
    ac_left, ac_right = src.pins.get('p'), src.pins.get('n')
    if not ac_left or not ac_right or ac_left == ac_right:
        return None
    other = set(model.nets) - {ac_left, ac_right}
    if len(other) != 2:
        return None
    plus_candidates = [net for net in other
                       if sum(part.pins.get('b') == net for part in diodes) == 2]
    minus_candidates = [net for net in other
                        if sum(part.pins.get('a') == net for part in diodes) == 2]
    if len(plus_candidates) != 1 or len(minus_candidates) != 1:
        return None
    dc_plus, dc_minus = plus_candidates[0], minus_candidates[0]
    if dc_plus == dc_minus or set(load.pins.values()) != {dc_plus, dc_minus}:
        return None

    def diode_id(anode, cathode):
        found = [part.cid for part in diodes
                 if part.pins.get('a') == anode and part.pins.get('b') == cathode]
        return found[0] if len(found) == 1 else None

    ul = diode_id(ac_left, dc_plus)
    ur = diode_id(ac_right, dc_plus)
    ll = diode_id(dc_minus, ac_left)
    lr = diode_id(dc_minus, ac_right)
    if None in (ul, ur, ll, lr) or len({ul, ur, ll, lr}) != 4:
        return None
    if any(len(model.nets[net]) != 3 for net in model.nets):
        return None
    return Rectifier(src.cid, ul, ll, ur, lr, load.cid,
                     ac_left, ac_right, dc_plus, dc_minus)


def recognize_motif(model) -> str | None:
    if match_wheatstone(model) is not None:
        return 'wheatstone_diamond'
    if match_rectifier(model) is not None:
        return 'bridge_rectifier_diamond'
    return None


class _Drawing:
    def __init__(self, width: int, height: int, motif: str):
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" data-motif="{motif}">',
            '<title>CircuitGen routed schematic</title>',
            '<style><![CDATA[',
            '.symbol{fill:none;stroke:#213149;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}',
            '.mark{fill:#213149;font-family:Arial,sans-serif;font-size:16px;text-anchor:middle}',
            '.wire{fill:none;stroke:#264a6f;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}',
            '.junction{fill:#264a6f}',
            '.name{fill:#17243c;font-family:Arial,sans-serif;font-size:17px;font-weight:700}',
            '.value{fill:#3e506d;font-family:Arial,sans-serif;font-size:13px}',
            ']]></style>',
        ]

    @staticmethod
    def _text(value: str) -> str:
        return xml_escape(str(value), {'"': '&quot;', "'": '&apos;'})

    def wire(self, net: str, *points: tuple[float, float]):
        assert len(points) >= 2
        path = 'M ' + ' L '.join(f'{x:.2f} {y:.2f}' for x, y in points)
        self.parts.append(f'<path class="wire" data-net="{self._text(net)}" d="{path}"/>')

    def dot(self, net: str, point: tuple[float, float]):
        x, y = point
        self.parts.append(f'<circle class="junction" data-net="{self._text(net)}" cx="{x:.2f}" cy="{y:.2f}" r="4"/>')

    def text(self, style: str, value: str, x: float, y: float, anchor='middle'):
        if value:
            self.parts.append(f'<text class="{style}" text-anchor="{anchor}" x="{x:.2f}" y="{y:.2f}">{self._text(value)}</text>')

    def inline(self, model, cid: str, start: tuple[float, float], end: tuple[float, float],
               start_net: str, end_net: str, label: tuple[float, float],
               label_value: tuple[float, float]):
        """Draw an actual two-pin symbol along a diagonal or horizontal edge."""
        comp = model.components[cid]
        spec = model.symbols[cid]
        length = math.dist(start, end)
        terminal_radius = 2 * GRID_PX
        if length <= 2 * terminal_radius + 20:
            raise ValueError('Motif edge too short for registered symbol')
        ux = (end[0]-start[0])/length
        uy = (end[1]-start[1])/length
        cx, cy = (start[0]+end[0])/2, (start[1]+end[1])/2
        p1 = (cx-ux*terminal_radius, cy-uy*terminal_radius)
        p2 = (cx+ux*terminal_radius, cy+uy*terminal_radius)
        if set(comp.pins.values()) != {start_net, end_net}:
            raise ValueError('Motif component pin mapping differs from matched topology')
        forward = comp.pins['a'] == start_net
        angle = math.degrees(math.atan2(uy, ux)) + (0 if forward else 180)
        self.wire(start_net, start, p1)
        self.wire(end_net, p2, end)
        self.parts.append(f'<g data-component="{self._text(cid)}" data-type="{self._text(comp.kind)}">')
        self.parts.append(f'<g transform="rotate({angle:.6f} {cx:.2f} {cy:.2f})">')
        self.parts.append(symbol_body_svg(spec, cx, cy))
        self.parts.append('</g>')
        self.text('name', cid, *label)
        self.text('value', comp.value, *label_value)
        self.parts.append('</g>')

    def upright(self, model, cid: str, at: tuple[float, float], rotation: int,
                label: tuple[float, float], value: tuple[float, float]):
        comp = model.components[cid]
        self.parts.append(f'<g data-component="{self._text(cid)}" data-type="{self._text(comp.kind)}">')
        self.parts.append(symbol_body_svg(model.symbols[cid], *at, rotation=rotation))
        self.text('name', cid, *label)
        self.text('value', comp.value, *value)
        self.parts.append('</g>')

    def finish(self):
        return '\n'.join(self.parts + ['</svg>']) + '\n'


_TOP = (410, 98)
_BOTTOM = (410, 480)
_LEFT = (232, 289)
_RIGHT = (588, 289)
_UPPER_LEFT = ((290, 143), (290, 162))
_LOWER_LEFT = ((290, 422), (290, 442))
_UPPER_RIGHT = ((530, 143), (530, 162))
_LOWER_RIGHT = ((530, 422), (530, 442))


def _diamond_arms(d: _Drawing, model, roles, edges):
    for cid, start, end, start_net, end_net, label, value in edges:
        d.inline(model, cid, start, end, start_net, end_net, label, value)


def _wheatstone_svg(model, roles: Wheatstone) -> str:
    d = _Drawing(720, 590, 'wheatstone_diamond')
    source_x, source_y = 88, 289
    source_terminal = 2 * GRID_PX
    d.wire(roles.top, (source_x, source_y-source_terminal), (source_x, _TOP[1]), _TOP)
    d.wire(roles.bottom, (source_x, source_y+source_terminal), (source_x, _BOTTOM[1]), _BOTTOM)
    d.upright(model, roles.source, (source_x, source_y), 0, (48, 229), (48, 378))
    d.inline(model, roles.upper_left, _TOP, _LEFT, roles.top, roles.left, *_UPPER_LEFT)
    d.inline(model, roles.lower_left, _LEFT, _BOTTOM, roles.left, roles.bottom, *_LOWER_LEFT)
    d.inline(model, roles.upper_right, _TOP, _RIGHT, roles.top, roles.right, *_UPPER_RIGHT)
    d.inline(model, roles.lower_right, _RIGHT, _BOTTOM, roles.right, roles.bottom, *_LOWER_RIGHT)
    d.inline(model, roles.bridge, _LEFT, _RIGHT, roles.left, roles.right,
             (410, 266), (410, 331))
    for net, at in ((roles.top, _TOP), (roles.bottom, _BOTTOM),
                    (roles.left, _LEFT), (roles.right, _RIGHT)):
        d.dot(net, at)
    return d.finish()


def _rectifier_svg(model, roles: Rectifier) -> str:
    d = _Drawing(945, 590, 'bridge_rectifier_diamond')
    d.inline(model, roles.upper_left, _LEFT, _TOP, roles.ac_left, roles.dc_plus, *_UPPER_LEFT)
    d.inline(model, roles.lower_left, _LEFT, _BOTTOM, roles.ac_left, roles.dc_minus, *_LOWER_LEFT)
    d.inline(model, roles.upper_right, _TOP, _RIGHT, roles.dc_plus, roles.ac_right, *_UPPER_RIGHT)
    d.inline(model, roles.lower_right, _BOTTOM, _RIGHT, roles.dc_minus, roles.ac_right, *_LOWER_RIGHT)

    # The AC source occupies the center rail and the load occupies an outer DC rail.
    source_at = (410, 289)
    d.wire(roles.ac_left, _LEFT, (source_at[0]-2*GRID_PX, source_at[1]))
    d.wire(roles.ac_right, (source_at[0]+2*GRID_PX, source_at[1]), _RIGHT)
    d.upright(model, roles.source, source_at, 270, (410, 246), (410, 353))

    load_at = (798, 289)
    load_rotation = 90 if model.components[roles.load].pins['a'] == roles.dc_plus else 270
    d.wire(roles.dc_plus, _TOP, (load_at[0], _TOP[1]),
           (load_at[0], load_at[1]-2*GRID_PX))
    d.wire(roles.dc_minus, _BOTTOM, (load_at[0], _BOTTOM[1]),
           (load_at[0], load_at[1]+2*GRID_PX))
    d.upright(model, roles.load, load_at, load_rotation, (859, 277), (859, 301))
    for net, at in ((roles.dc_plus, _TOP), (roles.dc_minus, _BOTTOM),
                    (roles.ac_left, _LEFT), (roles.ac_right, _RIGHT)):
        d.dot(net, at)
    return d.finish()


def render_motif(model) -> str | None:
    """Return a specialized SVG, or None for the untouched regular pipeline."""
    wheatstone = match_wheatstone(model)
    if wheatstone is not None:
        return _wheatstone_svg(model, wheatstone)
    rectifier = match_rectifier(model)
    if rectifier is not None:
        return _rectifier_svg(model, rectifier)
    return None
