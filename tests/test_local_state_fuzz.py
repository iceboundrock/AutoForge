"""The deserialize boundary: a state file that is not a JSON object.

The LOCAL rows of this module, one illegal LOCAL state per row and the same
question asked at random, went when the loader began refusing every LOCAL
file as an unknown mode (ADR 0009 D4, #387): they asserted nothing about the
loader any more. `tests/test_state_fuzz.py` fuzzes the loader on REMOTE
states (#386). What is left goes with this module (#388).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoforge.errors import StateError
from autoforge.state import load_state


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_state_file_that_is_not_json_is_corruption_not_a_fresh_run(tmp_path):
    """The most likely real corruption: a truncated write from an older scheme.

    Losing it silently would start a second implementation attempt against a
    tree that already holds the first one's work.
    """
    for text in ("", "{", '{"phase": ', "\x00\x00", "[]", '"a string"', "null"):
        path = write(tmp_path / "state.json", {})
        path.write_text(text, encoding="utf-8")
        with pytest.raises(StateError):
            load_state(path)
