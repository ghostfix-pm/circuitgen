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

PROMPT = """You are a strict ELECTRICAL NETLIST TRANSLATOR, not a drawing AI.
Translate the user's low-voltage educational circuit description into a NETLIST.
DO NOT generate Python, SVG, coordinates, paths, or explanations.

SUPPORTED PARTS: resistor, capacitor, inductor, diode, led, switch, lamp,
fuse, battery, dc_source. Every part has EXACTLY TWO electrical pins:
- battery and dc_source: p (positive), n (negative)
- all others: a, b (for diode/LED, a=anode and b=cathode)

RULES:
1. Always use the user's explicitly requested part counts/values.
2. Exactly ONE battery or dc_source is supported. Do not invent extra parts.
3. Each physical terminal appears in exactly ONE net's pins array.
4. Every net must have at least TWO terminals. Do not invent loose ends.
5. Series: join the end of one component to start of the next with a net.
6. Parallel: connect all branch starts to a shared net and branch ends
   to another shared net. Preserve internal intermediate nets in branches.
7. Close the circuit back to the source's negative terminal. If the user's
   description is ambiguous, choose a minimal conventional arrangement;
   do NOT silently add safety-critical parts without explicit request.
8. If the user requests unsupported multi-terminal or non-series-parallel
   circuit structures, DO NOT fake them with components of other types.
   Output an empty components array and empty nets array instead.
9. Assign distinct IDs, e.g. V1, R1, R2, C1, D1; net names N1, N2, ...
10. For a polarized diode/LED, pin a is the anode and pin b is the cathode.

EXAMPLE (9V battery, 330-ohm resistor, LED in series):
{"components":[{"id":"V1","type":"battery","value":"9V"},
{"id":"R1","type":"resistor","value":"330Ω"},
{"id":"D1","type":"led","value":"red"}],
"nets":[{"name":"N1","pins":["V1.p","R1.a"]},
{"name":"N2","pins":["R1.b","D1.a"]},
{"name":"N3","pins":["D1.b","V1.n"]}]}

User description:\n"""


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
