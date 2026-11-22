import copy
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from circuitgen.engine import (CircuitError, make_routed_svg_from_netlist,
                    make_svg_from_netlist, route_netlist, validate_model)
from circuitgen.routing import (_MeshRouter, RoutingError, _intersects_rect,
                     _segment_clearance, _segments, route_model)

ROOT = Path(__file__).resolve().parent.parent


def example(name):
    return json.loads((ROOT / 'examples' / name).read_text(encoding='utf-8'))


class TestRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = example('wheatstone.json')
        cls.bridge_routes = route_netlist(cls.bridge)
        cls.controller = example('controller.json')
        cls.controller_routes = route_netlist(cls.controller)

    def test_wheatstone_all_nets_have_physical_wires(self):
        result = self.bridge_routes
        self.assertEqual(set(result.paths), {x['name'] for x in self.bridge['nets']})
        self.assertEqual(result.labels, ())
        self.assertEqual(set(result.pin_to_net),
                         {ref for net in self.bridge['nets'] for ref in net['pins']})
        self.assertEqual(sum(len(p) for p in result.paths.values()), 8)

    def test_wheatstone_has_valid_branch_junctions(self):
        # The rail branches may meet at component pins; midpoint junctions
        # must be explicit dots where the bridge resistor joins each arm.
        self.assertTrue(self.bridge_routes.junctions['LEFT_MID'])
        self.assertTrue(self.bridge_routes.junctions['RIGHT_MID'])
        self.assertEqual(self.bridge_routes.labels, ())

    def test_every_wire_is_manhattan(self):
        for result in (self.bridge_routes, self.controller_routes):
            for paths in result.paths.values():
                for path in paths:
                    for a, b in _segments(path):
                        self.assertNotEqual(a, b)
                        self.assertTrue(abs(a[0]-b[0]) < 1e-5 or abs(a[1]-b[1]) < 1e-5)

    def test_no_unrelated_wire_intersections(self):
        for result in (self.bridge_routes, self.controller_routes):
            line_segments = [(net, a, b) for net, paths in result.paths.items()
                             for path in paths for a, b in _segments(path)]
            for i, (net, a, b) in enumerate(line_segments):
                for other, c, d in line_segments[i+1:]:
                    if net != other:
                        self.assertGreater(_segment_clearance(a,b,c,d), 0.14)

    def test_wires_do_not_cross_symbol_bodies(self):
        for raw, result in ((self.bridge, self.bridge_routes),
                            (self.controller, self.controller_routes)):
            router = _MeshRouter(validate_model(raw), result.placement, 11)
            for paths in result.paths.values():
                for path in paths:
                    for a, b in _segments(path):
                        self.assertFalse(any(_intersects_rect(a,b,rect)
                                             for rect in router.rects))

    def test_controller_has_physical_sensor_data_wires(self):
        result = self.controller_routes
        expected = {'GND': 3, 'VCC': 2}
        for net, count in expected.items():
            self.assertEqual(sum(label.net == net for label in result.labels), count)
        # Data pins should be joined by real wires instead of floating labels.
        self.assertFalse(any(label.net in ('SDA', 'SCL') for label in result.labels))
        self.assertTrue(result.paths['SDA'])
        self.assertTrue(result.paths['SCL'])
        self.assertTrue(result.paths['LED'])
        self.assertTrue(result.paths['LEDA'])
        for label in result.labels:
            self.assertEqual(result.pin_to_net[label.ref], label.net)

    def test_controller_svg_labels_pins_inside_symbols(self):
        svg = make_routed_svg_from_netlist(self.controller)
        ET.fromstring(svg)
        self.assertIn('data-net="SDA"', svg)
        self.assertIn('data-net="SCL"', svg)
        self.assertIn('>A4<', svg)
        self.assertIn('>GND<', svg)

    def test_auto_api_falls_back_only_when_needed(self):
        bridge_svg = make_svg_from_netlist(self.bridge)
        self.assertIn('CircuitGen routed schematic', bridge_svg)
        self.assertEqual(bridge_svg.count('data-component="'), 6)
        small = {'components': [
            {'id':'V1', 'type':'battery', 'value':'9V'},
            {'id':'R1', 'type':'resistor', 'value':'330Ω'}],
            'nets': [
                {'name':'A', 'pins':['V1.p','R1.a']},
                {'name':'B', 'pins':['R1.b','V1.n']}]}
        self.assertNotIn('CircuitGen routed schematic', make_svg_from_netlist(small))

    def test_svg_contains_real_wires_not_dashed_guides(self):
        svg = make_routed_svg_from_netlist(self.bridge)
        ET.fromstring(svg)
        self.assertIn('class="wire"', svg)
        self.assertNotIn('class="guide"', svg)
        self.assertNotIn('class="bg"', svg)
        self.assertNotIn('class="foot"', svg)
        self.assertIn('data-net="LEFT_MID"', svg)

    def test_deterministic_when_netlist_order_changes(self):
        original = make_routed_svg_from_netlist(self.bridge)
        permuted = copy.deepcopy(self.bridge)
        permuted['components'].reverse()
        permuted['nets'].reverse()
        for net in permuted['nets']:
            net['pins'].reverse()
        self.assertEqual(original, make_routed_svg_from_netlist(permuted))

    def test_multiple_source_circuit(self):
        raw = {'components': [
            {'id':'V1','type':'battery','value':'9V'},
            {'id':'V2','type':'ac_source','value':'3V'},
            {'id':'R1','type':'resistor','value':'1k'}],
            'nets': [
                {'name':'A','pins':['V1.p','R1.a']},
                {'name':'B','pins':['R1.b','V2.p']},
                {'name':'C','pins':['V2.n','V1.n']}]}
        self.assertEqual(set(route_netlist(raw).placement.symbols), {'V1','V2','R1'})

    def test_invalid_pin_is_rejected_before_route(self):
        raw = copy.deepcopy(self.bridge)
        raw['nets'][0]['pins'].append('R5.not_a_pin')
        with self.assertRaisesRegex(CircuitError, 'Unknown terminal'):
            route_netlist(raw)

    def test_grid_budget_is_explicit_failure_not_partial_svg(self):
        from unittest.mock import patch
        with patch('circuitgen.routing.MAX_TRACK_NODES', 1):
            with self.assertRaisesRegex(RoutingError, 'grid limit'):
                route_netlist(self.bridge)


if __name__ == '__main__':
    unittest.main()
