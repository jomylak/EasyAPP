import gzip
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "langfuse_filter", Path(__file__).parent.parent / "scripts" / "langfuse_filter.py")
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)

FIRST = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "job prompt"}]}])
LATER = json.dumps([{"role": "user", "parts": []}, {"role": "assistant", "parts": []}])


def batch(*msgs):
    return {"batch": [{"body": {"metadata": {lf.KEY: m, "keep": "x"}}} for m in msgs]}


def test_keeps_first_call_drops_later_ones():
    b = batch(FIRST, LATER)
    lf.strip_history(b)
    assert lf.KEY in b["batch"][0]["body"]["metadata"]
    assert lf.KEY not in b["batch"][1]["body"]["metadata"]
    assert b["batch"][1]["body"]["metadata"]["keep"] == "x"


def test_handles_list_valued_field_and_tool_role():
    b = {"metadata": {lf.KEY: [{"role": "user"}, {"role": "tool"}]}}
    lf.strip_history(b)
    assert lf.KEY not in b["metadata"]


def test_gzip_roundtrip_and_garbage_passthrough():
    body, enc = lf.filter_body(gzip.compress(json.dumps(batch(LATER)).encode()), "gzip")
    assert enc is None and lf.KEY not in body.decode()
    assert lf.filter_body(b"not json", None) == (b"not json", None)
