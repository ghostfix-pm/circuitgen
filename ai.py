"""Gemini natural-language-to-netlist translation for built-in and custom components.

Custom components use an explicit ``pins`` list. This is a logical schematic
symbol, not a verified physical component pinout.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from engine import CircuitError

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"

# The component-level pins field is OPTIONAL because registered symbols already
# define their pins. Unknown component types must supply it.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                    "pins": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "type", "value"],
                "additionalProperties": False,
            },
        },
        "nets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "pins": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "pins"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["components", "nets"],
    "additionalProperties": False,
}

PROMPT = """You are an electrical netlist translator for SchemAI.
Convert the user's educational circuit description into a complete JSON
netlist. Output only the JSON object; no diagrams, Python, SVG, or commentary.

REGISTERED COMPONENT TYPES (use these exact lowercase type strings):
- Two-terminal: resistor, capacitor, inductor, diode, led, switch, lamp,
  fuse, battery, dc_source, ac_source, current_source.
- Three-/multi-terminal: npn, pnp, nmos, pmos, opamp, ne555, arduino_uno.
- Custom components: types not listed above are supported using an explicit
  component-level "pins" array (see below).

PIN NAMES FOR REGISTERED COMPONENTS:
- battery/dc_source/ac_source/current_source: p (positive), n (negative)
- resistor/capacitor/inductor/switch/lamp/fuse: a, b
- led/diode: a (anode), b (cathode)
- npn/pnp: B, C, E
- nmos/pmos: G, D, S
- opamp: IN+, IN-, OUT, V+, V-
- ne555: VCC, GND, TRIG, THRES, DISCH, RESET, CTRL, OUT
  (IMPORTANT: pin 6 is THRES, NEVER THRESH)
- arduino_uno: D0 through D13, A0 through A5, 3V3, 5V, VIN, GND, RESET

CUSTOM COMPONENT RULES (CRITICAL):
1. If a component type is not registered, provide a "pins" array listing
   unique logical pin names for that component. Never omit this field.
2. Use the SAME case-sensitive pin names in the nets (e.g. S1.SDA).
3. Example generic I2C sensor:
   {"id":"S1","type":"i2c_sensor","value":"generic",
    "pins":["SDA","SCL","VCC","GND"]}
   These names are a logical interface for a user-described generic sensor;
   they are NOT a verified physical package pinout.
4. Pin names may contain letters, digits, underscore, + and - (1-24 chars).
   Keep component types lowercase with underscores, e.g. i2c_sensor.
5. For a *specific* unfamiliar IC/module whose pinout was not supplied,
   do not invent a real-world physical pinout. Use only the logical pins
   clearly specified by the user. Do not claim physical pin numbers.
6. Registered components normally omit "pins" because SchemAI already
   knows them. If supplied, the list must match the registered pins exactly.

GENERAL NETLIST RULES:
1. Preserve explicitly requested components, values, and connections.
2. Assign a unique ID to every component and unique name to every net.
3. Each physical/logical pin may belong to AT MOST one net.
4. A net connects electrically identical nodes, potentially many pins.
5. Never create different nets for points directly wired together.
6. Never invent intermediate components or silently change a part's type.
7. Unused pins on multi-pin components may remain unconnected: do not
   invent connections simply to fill their pins.
8. Support series, parallel, bridges, feedback, transistors, and ICs.
9. Match polarity and directions, especially LED anode/cathode.
10. Return exactly "components" and "nets" at the top level.
11. For every net terminal reference, use "componentID.pinName" and ensure
    that pinName is defined by the registered symbol or the custom pin list.

EXAMPLE — Arduino Uno with custom I2C sensor:
{
  "components": [
    {"id":"U1","type":"arduino_uno","value":"Arduino Uno"},
    {"id":"S1","type":"i2c_sensor","value":"generic",
     "pins":["SDA","SCL","VCC","GND"]}
  ],
  "nets": [
    {"name":"SDA_LINE","pins":["U1.A4","S1.SDA"]},
    {"name":"SCL_LINE","pins":["U1.A5","S1.SCL"]},
    {"name":"VCC","pins":["U1.5V","S1.VCC"]},
    {"name":"GND","pins":["U1.GND","S1.GND"]}
  ]
}

User description:
"""


def generate_netlist(
    user_prompt: str,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
    correction: str = "",
) -> dict[str, Any]:
    """Request a netlist from Gemini. Validation/rendering remains in engine.py."""
    if provider != "gemini":
        raise CircuitError(f"Unsupported AI provider: {provider}")
    model = model or DEFAULT_MODEL
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise CircuitError("GEMINI_API_KEY is not set")
    full_text = PROMPT + user_prompt
    if correction:
        full_text += (
            "\n\nPREVIOUS OUTPUT WAS REJECTED. Return a corrected full netlist. "
            "Do not claim unsupported circuit is possible. Error: " + correction[:900]
        )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_text}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseJsonSchema": SCHEMA,
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            response = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:900]
        raise CircuitError(f"Gemini API HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise CircuitError(f"Gemini API connection problem: {e}") from e
    try:
        parts = response["candidates"][0]["content"]["parts"]
        raw = "".join(p.get("text", "") for p in parts)
        decoded = json.loads(raw)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise CircuitError("Gemini did not return a valid netlist JSON object") from e
    if not isinstance(decoded, dict):
        raise CircuitError("Gemini JSON was not an object")
    return decoded
