"""The oracle: optional help from Google's Gemini (set GEMINI_API_KEY).

Everything here fails soft: no key, a timeout or a bad answer returns None and the
pigeon carries on with its own rules.
"""

import base64
import json
import os
import sys

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ("gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemini-flash-latest")


def ask(prompt, schema, images=(), model=None, timeout=10):
    """Send a prompt (plus optional images as (mime, bytes)) and get JSON back that fits `schema`."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    parts = [{"text": prompt}]
    for mime, data in images:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                 "responseSchema": schema}}
    for m in ([model] if model else []) + list(MODELS):
        try:
            r = requests.post(API.format(model=m), params={"key": key}, json=body, timeout=timeout)
            if r.status_code in (404, 400) and "model" in r.text.lower():
                continue  # that model name isn't available: try the next
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except requests.Timeout:
            print("oracle: timeout", file=sys.stderr)
            return None
        except Exception as exc:  # never print the message: it can echo the key in the URL
            code = getattr(getattr(exc, "response", None), "status_code", "")
            print(f"oracle: {type(exc).__name__} {code}", file=sys.stderr)
            return None
    return None
