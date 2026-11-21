"""Phase 7: conservative ERC and backward-compatibility checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from engine import (CircuitError, check_electrical_rules, make_checked_svg_from_netlist,
                    make_svg_from_netlist)

ROOT = Path(__file__).resolve().parents[1]


def example(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def netlist(components, nets):
    return {"components": components, "nets": nets}


def component(cid, kind, value=""):
    return {"id": cid, "type": kind, "value": value}


def source_short():
    return netlist([component("V1", "battery", "9V")],
                   [{"name": "SAME", "pins": ["V1.p", "V1.n"]}])


class TestElectricalRules(unittest.TestCase):
    def test_existing_wheatstone_passes(self):
        report = check_electrical_rules(example("wheatstone.json"))
        self.assertTrue(report.ok)
        self.assertFalse(report.warnings)

    def test_existing_rectifier_passes(self):
        report = check_electrical_rules(example("bridge_rectifier.json"))
        self.assertTrue(report.ok)
        self.assertFalse(report.warnings)

    def test_existing_controller_has_no_definite_faults(self):
        report = check_electrical_rules(example("controller.json"))
        self.assertTrue(report.ok)
        self.assertEqual([], list(report.errors))
        self.assertIn("UNVERIFIED_CUSTOM_PINOUT", {i.code for i in report.infos})

    def test_sources_in_series_are_not_flagged_as_parallel(self):
        report = check_electrical_rules(example("layout_optimization.json"))
        self.assertNotIn("CONFLICTING_VOLTAGE_SOURCES", {i.code for i in report.issues})
        self.assertTrue(report.ok)

    def test_source_direct_short_is_error(self):
        report = check_electrical_rules(source_short())
        self.assertFalse(report.ok)
        self.assertIn("SHORTED_VOLTAGE_SOURCE", {i.code for i in report.errors})

    def test_bypassed_resistor_is_warning(self):
        raw = netlist([component("R1", "resistor", "1kΩ")],
                      [{"name": "N", "pins": ["R1.a", "R1.b"]}])
        report = check_electrical_rules(raw)
        self.assertTrue(report.ok)
        self.assertIn("BYPASSED_COMPONENT", {i.code for i in report.warnings})

    def test_dangling_net_and_missing_power_pin(self):
        raw = netlist([component("U1", "ne555", "NE555")],
                      [{"name": "N", "pins": ["U1.TRIG"]}])
        report = check_electrical_rules(raw)
        codes = {i.code for i in report.warnings}
        self.assertIn("DANGLING_NET", codes)
        self.assertIn("UNCONNECTED_POWER_PIN", codes)

    def test_same_device_supply_ground_is_error(self):
        raw = netlist([component("U1", "ne555", "NE555")],
                      [{"name": "N", "pins": ["U1.VCC", "U1.GND"]}])
        report = check_electrical_rules(raw)
        self.assertIn("SHORTED_DEVICE_SUPPLY", {i.code for i in report.errors})

    def test_suspicious_rail_name_is_warning_not_error(self):
        raw = netlist([component("U1", "arduino_uno", "Uno")],
                      [{"name": "GND", "pins": ["U1.5V"]}])
        report = check_electrical_rules(raw)
        self.assertIn("SUPPLY_ON_GROUND_NET", {i.code for i in report.warnings})
        self.assertTrue(report.ok)

    def test_multiple_digital_outputs_share_net_warning(self):
        raw = netlist([component("U1", "opamp", "A"), component("U2", "opamp", "B")],
                      [{"name": "BUS", "pins": ["U1.OUT", "U2.OUT"]}])
        report = check_electrical_rules(raw)
        self.assertIn("OUTPUT_CONFLICT", {i.code for i in report.warnings})

    def test_parallel_sources_different_voltages_are_error(self):
        report = check_electrical_rules(example("erc_issues.json"))
        self.assertIn("CONFLICTING_VOLTAGE_SOURCES", {i.code for i in report.errors})
        self.assertIn("OUTPUT_CONFLICT", {i.code for i in report.warnings})
        self.assertIn("BYPASSED_COMPONENT", {i.code for i in report.warnings})

    def test_matching_parallel_sources_are_warning(self):
        raw = netlist([component("V1", "battery", "9V"), component("V2", "battery", "9V")],
                      [{"name": "P", "pins": ["V1.p", "V2.p"]},
                       {"name": "N", "pins": ["V1.n", "V2.n"]}])
        report = check_electrical_rules(raw)
        self.assertIn("PARALLEL_VOLTAGE_SOURCES", {i.code for i in report.warnings})
        self.assertFalse(report.errors)

    def test_mixed_rails_warns(self):
        raw = netlist([component("U1", "arduino_uno", "Uno")],
                      [{"name": "P", "pins": ["U1.5V", "U1.3V3"]}])
        report = check_electrical_rules(raw)
        self.assertIn("MIXED_POWER_RAILS", {i.code for i in report.warnings})

    def test_opamp_positive_and_negative_rails_short(self):
        raw = netlist([component("U1", "opamp", "LM358")],
                      [{"name": "S", "pins": ["U1.V+", "U1.V-"]}])
        report = check_electrical_rules(raw)
        self.assertIn("SHORTED_DEVICE_SUPPLY", {i.code for i in report.errors})

    def test_parallel_ac_and_dc_sources_remain_warning(self):
        raw = netlist([component("V1", "ac_source", "12V"),
                       component("V2", "battery", "9V")],
                      [{"name": "P", "pins": ["V1.p", "V2.p"]},
                       {"name": "N", "pins": ["V1.n", "V2.n"]}])
        report = check_electrical_rules(raw)
        self.assertNotIn("CONFLICTING_VOLTAGE_SOURCES", {i.code for i in report.errors})
        self.assertIn("PARALLEL_VOLTAGE_SOURCES", {i.code for i in report.warnings})

    def test_multiple_disconnected_groups_are_info(self):
        raw = netlist([component("R1", "resistor", "1k"), component("R2", "resistor", "2k")],
                      [{"name": "A", "pins": ["R1.a", "R1.b"]},
                       {"name": "B", "pins": ["R2.a", "R2.b"]}])
        report = check_electrical_rules(raw)
        self.assertIn("DISCONNECTED_GROUPS", {i.code for i in report.infos})

    def test_invalid_netlist_raises_circuit_error(self):
        with self.assertRaises(CircuitError):
            check_electrical_rules({"components": [], "nets": []})

    def test_order_independent_report(self):
        raw = example("erc_issues.json")
        shuffled = {"components": list(reversed(raw["components"])),
                    "nets": list(reversed(raw["nets"]))}
        self.assertEqual(check_electrical_rules(raw).as_dict(),
                         check_electrical_rules(shuffled).as_dict())

    def test_optional_checked_render_does_not_change_svg(self):
        raw = example("controller.json")
        svg, report = make_checked_svg_from_netlist(raw, color="#A1B2C3")
        self.assertTrue(report.ok)
        self.assertEqual(make_svg_from_netlist(raw, color="#A1B2C3"), svg)

    def test_checked_render_blocks_definite_errors(self):
        with self.assertRaisesRegex(CircuitError, "SHORTED_VOLTAGE_SOURCE"):
            make_checked_svg_from_netlist(source_short())

    def test_strict_mode_blocks_warnings(self):
        raw = netlist([component("V1", "battery", "9V"), component("V2", "battery", "9V")],
                      [{"name": "P", "pins": ["V1.p", "V2.p"]},
                       {"name": "N", "pins": ["V1.n", "V2.n"]}])
        with self.assertRaisesRegex(CircuitError, "PARALLEL_VOLTAGE_SOURCES"):
            make_checked_svg_from_netlist(raw, strict=True)

    def test_json_report_serializable(self):
        report = check_electrical_rules(example("erc_issues.json"))
        result = json.loads(json.dumps(report.as_dict()))
        self.assertFalse(result["ok"])
        self.assertGreater(result["summary"]["errors"], 0)

    def test_cli_returns_error_code_and_json(self):
        proc = subprocess.run([sys.executable, "erc.py", "examples/erc_issues.json", "--json"],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertIn("issues", report)

    def test_cli_clean_example(self):
        proc = subprocess.run([sys.executable, "erc.py", "examples/wheatstone.json"],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
