"""Atheris target: ``DDPHeader.decode`` on arbitrary datagrams (what ``DDPSender`` reads from the network).

Contract: input shorter than the header raises ``ValueError``; anything longer decodes, re-encodes to the same first
10 bytes, and decodes back to an equal header. Any other exception is a finding.
"""

from __future__ import annotations

import sys

from aioddp.const import HEADER_SIZE
from aioddp.protocol import DDPHeader


def test_one_input(data: bytes) -> None:
    try:
        header = DDPHeader.decode(data)
    except ValueError:
        if len(data) >= HEADER_SIZE:
            raise
        return
    wire = header.encode()
    if wire != data[:HEADER_SIZE]:
        raise AssertionError(f"re-encoded header {wire.hex()} != input {data[:HEADER_SIZE].hex()}")
    if DDPHeader.decode(wire) != header:
        raise AssertionError(f"decode(encode(h)) != h for {header!r}")


def main() -> None:
    import atheris

    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
