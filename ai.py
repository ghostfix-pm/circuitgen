from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from engine import CircuitError

# CONFIG

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"

# NETLIST PROMPT

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "components": {
            "type": "array", "items": {
                "type": "object", "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                }, "required": ["id", "type", "value"],
                "additionalProperties": False,
            }
        },
        "nets": {
            "type": "array", "items": {
                "type": "object", "properties": {
                    "name": {"type": "string"},
                    "pins": {"type": "array", "items": {"type": "string"}},
                }, "required": ["name", "pins"],
                "additionalProperties": False,
            }
        },
    },
    "required": ["components", "nets"],
    "additionalProperties": False,
}


PROMPT = """You are an electrical netlist translator for SchemAI.
Convert the user's circuit description into a complete, electrically connected
JSON netlist. Do not generate SVG, coordinates, Python, or explanations.

SUPPORTED COMPONENTS:
- Two-terminal: resistor, capacitor, inductor, diode, led, switch,
  lamp, fuse, battery, dc_source.
- Multi-terminal: ne555, arduino_uno, and other components
  registered in SchemAI's symbol library.

PIN CONVENTIONS:
- battery/dc_source: p (positive), n (negative)
- resistor/capacitor/inductor/switch/lamp/fuse: a, b
- diode/LED: a (anode), b (cathode)
- NE555: VCC, GND, TRIG, THRES, DISCH, RESET, CTRL, OUT
- Arduino Uno: use the exact registered pin names.

NETLIST RULES:
1. Preserve all explicitly requested components and values.
2. Preserve the user's intended electrical connections.
3. Each component must have a unique ID.
4. Each net must have a unique name.
5. Each physical pin may belong to at most one net.
6. A net may connect two or more pins, including IC pins.
7. Use shared nets for electrically identical nodes.
8. Never invent intermediate components to simplify complex circuits.
9. Support series, parallel, bridge, IC, and general graph topologies.
10. Do not reject a circuit simply because it contains an IC or
    cannot be reduced to series-parallel branches.
11. Do not invent connections for unspecified IC pins.
12. Never substitute one component type for another.
13. Return only a JSON object with components and nets arrays.
14. Component "type" must use the exact lowercase identifier from
    SchemAI's symbol registry. For example, use "ne555", never "NE555";
    use "arduino_uno", never "Arduino Uno". All pin names must exactly 
    match the registered symbol definitions.

EXAMPLE:
{
  "components": [
    {"id": "V1", "type": "battery", "value": "9V"},
    {"id": "R1", "type": "resistor", "value": "330Ω"},
    {"id": "D1", "type": "led", "value": "red"}
  ],
  "nets": [
    {"name": "N1", "pins": ["V1.p", "R1.a"]},
    {"name": "N2", "pins": ["R1.b", "D1.a"]},
    {"name": "N3", "pins": ["D1.b", "V1.n"]}
  ]
}

User description:
"""


# GEMINI

def generate_netlist(
    user_prompt: str,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
    correction: str = "",
) -> dict[str, Any]:
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
        with urllib.request.urlopen(req, timeout=45) as res:
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
