"""Tests for the CircuitGen CLI entrypoint and packaging."""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from circuitgen import __version__
from circuitgen.cli import build_parser, main
from circuitgen.tui import CircuitGenApp


class TestCLIEntrypoint(unittest.TestCase):
    def test_parser_version(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as cm:
            with patch("sys.stdout"):
                parser.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_parser_help(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as cm:
            with patch("sys.stdout"):
                parser.parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_main_version(self):
        with self.assertRaises(SystemExit) as cm:
            with patch("sys.stdout"):
                main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    @patch.object(CircuitGenApp, "run")
    def test_main_no_args_launches_tui(self, mock_run):
        exit_code = main([])
        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once()

    def test_app_instantiation(self):
        app = CircuitGenApp()
        self.assertEqual(app.TITLE, "CircuitGen")
        self.assertIn(__version__, app.SUB_TITLE)

    def test_cli_subprocess_version(self):
        res = subprocess.run(
            [sys.executable, "-m", "circuitgen.cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn(__version__, res.stdout)

    def test_cli_subprocess_help(self):
        res = subprocess.run(
            [sys.executable, "-m", "circuitgen.cli", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("CircuitGen: Circuit schematics", res.stdout)
