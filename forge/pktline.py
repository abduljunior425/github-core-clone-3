"""The git pkt-line codec.

A pkt-line is 4 hex digits of total length (payload + 4) followed by the
payload. The three control packets are 0000 (flush), 0001 (delim) and 0002
(response-end). This module only frames bytes; the protocol layers above it
decide what the lines mean.
"""

from .consts import PKT_LINE_MAX

FLUSH = b"0000"
DELIM = b"0001"
RESPONSE_END = b"0002"

_HEX = b"0123456789abcdef"


def encode_line(payload: bytes) -> bytes:
    """One data pkt-line: 4 hex length bytes then the payload."""
    n = len(payload) + 4
    if n > PKT_LINE_MAX:
        raise ValueError(f"pkt-line payload of {len(payload)} bytes exceeds the maximum")
    return ("%04x" % n).encode("ascii") + payload


def encode_lines(items) -> bytes:
    """Encode a sequence of payloads and control packets.

    Each item is bytes (a data packet) or one of the strings "flush",
    "delim", "response-end".
    """
    parts = []
    for item in items:
        if item == "flush":
            parts.append(FLUSH)
        elif item == "delim":
            parts.append(DELIM)
        elif item == "response-end":
            parts.append(RESPONSE_END)
        else:
            parts.append(encode_line(item))
    return b"".join(parts)


class PktLineError(ValueError):
    """A malformed pkt-line stream."""


class Decoder:
    """Incremental pkt-line decoder over a buffered byte stream."""

    def __init__(self, data: bytes = b""):
        self._buf = bytearray(data)
        self._pos = 0

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next(self):
        """Return the next packet as ("data", payload), ("flush", None),
        ("delim", None) or ("response-end", None); None when more bytes are
        needed. Raises PktLineError on a malformed header."""
        if len(self._buf) - self._pos < 4:
            return None
        head = bytes(self._buf[self._pos:self._pos + 4])
        if not all(c in b"0123456789abcdefABCDEF" for c in head):
            raise PktLineError(f"invalid pkt-line length {head!r}")
        length = int(head.decode("ascii"), 16)
        if length == 0:
            self._pos += 4
            return ("flush", None)
        if length == 1:
            self._pos += 4
            return ("delim", None)
        if length == 2:
            self._pos += 4
            return ("response-end", None)
        if length == 3 or length > PKT_LINE_MAX:
            raise PktLineError(f"invalid pkt-line length {length}")
        if len(self._buf) - self._pos < length:
            return None
        payload = bytes(self._buf[self._pos + 4:self._pos + length])
        self._pos += length
        return ("data", payload)

    def rest(self) -> bytes:
        """The bytes not yet consumed as pkt-lines (a packfile follows a flush)."""
        out = bytes(self._buf[self._pos:])
        self._pos = len(self._buf)
        return out

    def consumed(self) -> int:
        return self._pos


def decode_all(data: bytes):
    """Decode a whole buffer into a list of packets; raises on truncation."""
    decoder = Decoder(data)
    packets = []
    while True:
        packet = decoder.next()
        if packet is None:
            break
        packets.append(packet)
    if decoder.consumed() != len(data):
        raise PktLineError("truncated pkt-line stream")
    return packets


def lines_of(packets):
    """The text of every data packet, control packets dropped."""
    return [payload.decode("utf-8", "replace") for kind, payload in packets if kind == "data"]
