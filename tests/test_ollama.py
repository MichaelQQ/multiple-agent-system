import json
import re

def test_ollama_json_extraction_regex():
    fenced_pattern = re.compile(r"```(?:json)?[ \t\n]*(\{[^\x00-\x1f]*?\})[ \t\n]*```")
    bare_pattern = re.compile(r"(\{[\s\S]*\})")

    cases = [
        ("```json\n{\"status\": \"success\"}\n```", fenced_pattern, {"status": "success"}),
        ("```\n{\"status\": \"success\"}\n```", fenced_pattern, {"status": "success"}),
        ("plain {\"status\": \"success\"} text", bare_pattern, {"status": "success"}),
        ("{\"status\": \"success\"}", bare_pattern, {"status": "success"}),
        ("no json here", bare_pattern, None),
    ]

    for raw, pattern, expected in cases:
        m = pattern.search(raw)
        if expected is None:
            assert m is None, f"Expected no match for: {raw!r}"
        else:
            assert m is not None, f"Expected match for: {raw!r}"
            candidate = m.group(1)
            data = json.loads(candidate)
            assert data == expected, f"Failed for {raw!r}: got {data}, expected {expected}"
