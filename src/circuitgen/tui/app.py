"""CircuitGen Textual Application Shell and Brand Palette."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.widgets import Footer, Header, Static

from circuitgen import __version__

# BRAND PALETTE DESIGN TOKENS (from §2.1 of CLI_PLAN.md)
PALETTE = {
    "background": "#0D1117",
    "surface": "#161B22",
    "selection_bg": "#163D2B",
    "primary_green": "#3fbf6e",
    "green_dim": "#267A49",
    "text": "#E5E7EB",
    "muted": "#9CA3AF",
    "border": "#30363D",
    "warning": "#FBBF24",
    "error": "#F87171",
    "info": "#60A5FA",
}

APP_CSS = f"""
Screen {{
    background: {PALETTE['background']};
    color: {PALETTE['text']};
}}

Header {{
    background: {PALETTE['surface']};
    color: {PALETTE['primary_green']};
    dock: top;
}}

Footer {{
    background: {PALETTE['surface']};
    color: {PALETTE['muted']};
    dock: bottom;
}}

#main-container {{
    width: 100%;
    height: 100%;
    align: center middle;
}}

#placeholder-card {{
    background: {PALETTE['surface']};
    border: solid {PALETTE['border']};
    padding: 2 4;
    width: 60;
    height: auto;
    content-align: center middle;
}}

.title-text {{
    color: {PALETTE['primary_green']};
    text-style: bold;
    text-align: center;
    margin-bottom: 1;
}}

.subtitle-text {{
    color: {PALETTE['muted']};
    text-align: center;
    margin-bottom: 1;
}}

.info-text {{
    color: {PALETTE['text']};
    text-align: center;
}}
"""


class CircuitGenApp(App[None]):
    """Main Textual Application for CircuitGen."""

    TITLE = "CircuitGen"
    SUB_TITLE = f"v{__version__}"
    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
        Binding("escape", "quit", "Exit", show=True, priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="main-container"):
            with Vertical(id="placeholder-card"):
                yield Static("CircuitGen", classes="title-text")
                yield Static("Circuit schematics from text or netlists.", classes="subtitle-text")
                yield Static("Press Esc or Ctrl+Q to exit.", classes="info-text")
        yield Footer()
