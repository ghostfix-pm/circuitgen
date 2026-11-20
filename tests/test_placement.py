import copy
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from engine import (CircuitError, make_placement_preview_svg_from_netlist,
                    make_svg_from_netlist, place_netlist, validate_model)
from symbols import pin_positions

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class TestPlacement(unittest.TestCase):
    def test_wheatstone_is_placed_but_not_routed(self):
        raw = example("wheatstone.json")
        result = place_netlist(raw)
        self.assertEqual(set(result.symbols), {"V1", "R1", "R2", "R3", "R4", "R5"})
        self.assertLess(result.symbols["R1"].x, result.symbols["R5"].x)
        self.assertLess(result.symbols["R5"].x, result.symbols["R2"].x)
        self.assertEqual(result.symbols["R5"].rotation, 90)
        self.assertLess(result.symbols["R1"].y, result.symbols["R3"].y)
        self.assertLess(result.symbols["R2"].y, result.symbols["R4"].y)
        with self.assertRaisesRegex(CircuitError, "Unsupported topology"):
            make_svg_from_netlist(raw)

    def test_wheatstone_preview_is_parseable(self):
        raw = example("wheatstone.json")
        artwork = make_placement_preview_svg_from_netlist(raw)
        ET.fromstring(artwork)
        self.assertIn("NOT a wired schematic", artwork)
        self.assertEqual(artwork.count('data-component="'), 6)
        self.assertIn('data-net="LEFT_MID"', artwork)

    def test_controller_and_custom_sensor(self):
        raw = example("controller.json")
        result = place_netlist(raw)
        self.assertEqual(len(result.symbols["U1"].pin_positions), 25)
        self.assertEqual(len(result.symbols["S1"].pin_positions), 4)
        self.assertEqual(len(result.symbols["D1"].pin_positions), 2)
        ET.fromstring(make_placement_preview_svg_from_netlist(raw))

    def test_all_component_pins_match_symbol_geometry(self):
        for name in ("controller.json", "wheatstone.json"):
            with self.subTest(name=name):
                raw = example(name)
                model = validate_model(raw)
                result = place_netlist(raw)
                for cid, item in result.symbols.items():
                    self.assertEqual(item.pin_positions, pin_positions(
                        model.symbols[cid], item.x, item.y, item.rotation, item.mirror))

    def test_placement_is_order_independent(self):
        raw = example("wheatstone.json")
        reversed_raw = copy.deepcopy(raw)
        reversed_raw["components"].reverse()
        reversed_raw["nets"].reverse()
        for net in reversed_raw["nets"]:
            net["pins"].reverse()
        self.assertEqual(place_netlist(raw), place_netlist(reversed_raw))
        self.assertEqual(make_placement_preview_svg_from_netlist(raw),
                         make_placement_preview_svg_from_netlist(reversed_raw))

    def test_no_component_bounds_overlap(self):
        for name in ("wheatstone.json", "controller.json"):
            items = list(place_netlist(example(name)).symbols.values())
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    self.assertTrue(abs(a.x - b.x) >= a.half_width + b.half_width or
                                    abs(a.y - b.y) >= a.half_height + b.half_height)

    def test_disconnected_groups_are_spaced_apart(self):
        raw = {"components": [
            {"id": "R1", "type": "resistor", "value": "1k"},
            {"id": "R2", "type": "resistor", "value": "2k"},
            {"id": "R3", "type": "resistor", "value": "3k"},
        ], "nets": [
            {"name": "PAIR", "pins": ["R1.a", "R2.a"]},
            {"name": "ISOLATED", "pins": ["R3.b"]},
        ]}
        result = place_netlist(raw)
        self.assertEqual(len(result.islands), 2)
        ET.fromstring(make_placement_preview_svg_from_netlist(raw))

    def test_multiple_sources_supported_in_placement(self):
        raw = {"components": [
            {"id": "V1", "type": "dc_source", "value": "12V"},
            {"id": "V2", "type": "ac_source", "value": "2Vac"},
            {"id": "R1", "type": "resistor", "value": "100"},
        ], "nets": [
            {"name": "A", "pins": ["V1.p", "R1.a"]},
            {"name": "B", "pins": ["V1.n", "V2.n"]},
            {"name": "C", "pins": ["R1.b", "V2.p"]},
        ]}
        self.assertEqual(set(place_netlist(raw).symbols), {"V1", "V2", "R1"})

    def test_simple_circuit_legacy_svg_does_not_change(self):
        raw = {"components": [
            {"id": "V1", "type": "battery", "value": "9V"},
            {"id": "R1", "type": "resistor", "value": "330Ω"},
            {"id": "D1", "type": "led", "value": "red"}],
            "nets": [
                {"name": "N1", "pins": ["V1.p", "R1.a"]},
                {"name": "N2", "pins": ["R1.b", "D1.a"]},
                {"name": "N3", "pins": ["D1.b", "V1.n"]},
            ]}
        self.assertIn('data-component="D1"', make_svg_from_netlist(raw))
        self.assertIn('PLACEMENT PREVIEW', make_placement_preview_svg_from_netlist(raw))

    def test_malformed_netlist_cannot_produce_preview(self):
        raw = example("wheatstone.json")
        raw["nets"][0]["pins"].append("R1.NO_SUCH_PIN")
        with self.assertRaisesRegex(CircuitError, "Unknown terminal"):
            make_placement_preview_svg_from_netlist(raw)


if __name__ == "__main__":
    unittest.main()
