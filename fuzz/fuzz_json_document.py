"""Atheris target: ``decode_json_document`` on arbitrary reply payloads (a controller's destination 250/251 answer).

Contract: an empty or NUL/whitespace-only payload is ``None``; anything else is a JSON object (``dict``) or raises
``DDPQueryError``. An accepted document re-serialises and decodes back to an equal document. Any other exception
is a finding.
"""

from __future__ import annotations

import json
import sys

from aioddp.exceptions import DDPQueryError
from aioddp.protocol import decode_json_document


def test_one_input(data: bytes) -> None:
    try:
        document = decode_json_document(data, what="fuzz")
    except DDPQueryError:
        return
    if document is None:
        if data.rstrip(b"\x00").strip():
            raise AssertionError(f"non-empty payload {data[:40]!r} decoded to None")
        return
    if not isinstance(document, dict):
        raise AssertionError(f"decoded {type(document).__name__}, expected dict")
    if decode_json_document(json.dumps(document).encode(), what="fuzz") != document:
        raise AssertionError("document does not survive a dumps/decode round trip")


def main() -> None:
    import atheris

    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
