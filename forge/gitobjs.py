"""Git objects and packfiles, standard library only.

Object ids are SHA-1 over ``"<type> <size>\\0" + data`` exactly as git
computes them. Packfiles are version 2, written with plain (undeltified)
objects; reading resolves OFS_DELTA and REF_DELTA so a delta-carrying pack
from any client still unpacks. The zlib stream's own consumed length marks
each entry's end, so pack boundaries are never guessed.
"""

import hashlib
import zlib

from .consts import VIEWER_EMAIL, VIEWER_LOGIN, VIEWER_NAME

ZERO_OID = "0" * 40

_TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}

_HEX40 = set("0123456789abcdef")


def is_oid(value) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX40 for c in value)


def hash_object(obj_type: str, data: bytes) -> str:
    h = hashlib.sha1()
    h.update(("%s %d\0" % (obj_type, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


# --------------------------------------------------------------------------
# object parsing and building
# --------------------------------------------------------------------------

def parse_tree(data: bytes):
    """A tree object -> [(mode, name, oid)] in stored order."""
    entries = []
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        nul = data.index(b"\0", space + 1)
        mode = data[pos:space].decode("ascii")
        name = data[space + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    # git sorts a directory as if its name ended in '/'
    key = name + "/" if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries) -> bytes:
    """Build a tree object from [(mode, name, oid)], sorted git's way."""
    parts = []
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        parts.append(("%s %s\0" % (mode, name)).encode("utf-8"))
        parts.append(bytes.fromhex(oid))
    return b"".join(parts)


def parse_commit(data: bytes):
    """A commit object -> dict(tree, parents, author, committer, message)."""
    text = data.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    out = {"tree": "", "parents": [], "author": "", "committer": "", "message": message}
    for line in head.split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key == "tree":
            out["tree"] = value
        elif key == "parent":
            out["parents"].append(value)
        elif key == "author":
            out["author"] = value
        elif key == "committer":
            out["committer"] = value
    return out


def build_commit(tree: str, parents, message: str, timestamp: int,
                 author: str | None = None, committer: str | None = None) -> bytes:
    """Build a commit object's bytes. `author`/`committer` are full identity
    lines without the timestamp (``Name <email>``); the timestamp is appended
    here so every commit this forge makes is reproducible."""
    ident = author or "%s <%s>" % (VIEWER_NAME, VIEWER_EMAIL)
    commit_ident = committer or ident
    lines = ["tree %s" % tree]
    for parent in parents:
        lines.append("parent %s" % parent)
    lines.append("author %s %d +0000" % (ident, timestamp))
    lines.append("committer %s %d +0000" % (commit_ident, timestamp))
    lines.append("")
    text = "\n".join(lines) + "\n" + message
    return text.encode("utf-8")


def parse_tag(data: bytes):
    text = data.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    out = {"object": "", "type": "", "tag": "", "tagger": "", "message": message}
    for line in head.split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key in out:
            out[key] = value
    return out


def build_tag(obj: str, obj_type: str, tag: str, message: str, timestamp: int,
              tagger: str | None = None) -> bytes:
    who = tagger or "%s <%s>" % (VIEWER_NAME, VIEWER_EMAIL)
    text = ("object %s\ntype %s\ntag %s\ntagger %s %d +0000\n\n%s"
            % (obj, obj_type, tag, who, timestamp, message))
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# reachability
# --------------------------------------------------------------------------

def collect_reachable(roots, lookup, allow_missing=False):
    """Every oid reachable from `roots` (commit -> tree + parents,
    tree -> entries, tag -> target). Raises KeyError on a missing object
    unless allow_missing."""
    seen = set()
    stack = [oid for oid in roots if is_oid(oid)]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = lookup(oid)
        if obj is None:
            if allow_missing:
                continue
            raise KeyError("missing object %s" % oid)
        seen.add(oid)
        obj_type, data = obj
        if obj_type == "commit":
            commit = parse_commit(data)
            stack.append(commit["tree"])
            stack.extend(commit["parents"])
        elif obj_type == "tag":
            stack.append(parse_tag(data)["object"])
        elif obj_type == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":  # a gitlink names a commit we do not hold
                    stack.append(child)
    return seen


# --------------------------------------------------------------------------
# packfiles
# --------------------------------------------------------------------------

class PackError(ValueError):
    """A malformed packfile."""


def _read_varint(buf: bytes, pos: int):
    """Read a pack size varint; return (size, next_pos, first_byte).

    The object type lives in the first byte only (bits 4-6), so the first
    byte is returned separately from the accumulated size."""
    first = buf[pos]
    byte = first
    size = byte & 0x0F
    shift = 4
    pos += 1
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("truncated pack size varint")
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return size, pos, first


def _read_ofs_base(buf: bytes, pos: int):
    byte = buf[pos]
    pos += 1
    value = byte & 0x7F
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("truncated ofs-delta base offset")
        byte = buf[pos]
        pos += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, pos


def _inflate_at(buf: bytes, pos: int, trailer: int):
    """Inflate one zlib stream starting at `pos`; return (data, next_pos)."""
    decomp = zlib.decompressobj()
    try:
        data = decomp.decompress(buf[pos:trailer])
    except zlib.error as err:
        raise PackError("zlib stream did not inflate: %s" % err)
    if not decomp.eof:
        raise PackError("truncated zlib stream in pack")
    consumed = len(buf[pos:trailer]) - len(decomp.unused_data)
    return data, pos + consumed


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            if pos >= len(delta):
                raise PackError("truncated delta varint")
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    source_size = read_varint()
    target_size = read_varint()
    if source_size != len(base):
        raise PackError("delta expects a %d-byte base, got %d" % (source_size, len(base)))
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    copy_off |= delta[pos] << (bit * 8)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    copy_size |= delta[pos] << (bit * 8)
                    pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            if copy_off + copy_size > len(base):
                raise PackError("delta copy runs past its base")
            out += base[copy_off:copy_off + copy_size]
        elif op:
            if pos + op > len(delta):
                raise PackError("truncated delta insert")
            out += delta[pos:pos + op]
            pos += op
        else:
            raise PackError("delta opcode 0 is invalid")
    if len(out) != target_size:
        raise PackError("delta produced %d of %d bytes" % (len(out), target_size))
    return bytes(out)


def read_pack(pack: bytes, external_lookup=None):
    """Read a packfile -> {oid: (type, data)}. Resolves OFS_DELTA and
    REF_DELTA entries; verifies every object's hash and the trailer.
    `external_lookup(oid)` (optional) resolves thin-pack bases that live in
    the receiver's store instead of in the pack."""
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise PackError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise PackError("unsupported pack version %d" % version)
    count = int.from_bytes(pack[8:12], "big")
    trailer = len(pack) - 20
    if hashlib.sha1(pack[:trailer]).digest() != pack[trailer:]:
        raise PackError("pack trailer checksum mismatch")
    raw = []  # (offset, type_code, data, base_ref)
    by_offset = {}
    pos = 12
    for _ in range(count):
        entry_offset = pos
        size, pos, first = _read_varint(pack, pos)
        type_code = (first >> 4) & 0x07
        base_ref = None
        if type_code == 6:  # OFS_DELTA
            back, pos = _read_ofs_base(pack, pos)
            base_ref = ("ofs", entry_offset - back)
        elif type_code == 7:  # REF_DELTA
            base_ref = ("ref", pack[pos:pos + 20].hex())
            pos += 20
        elif type_code not in _CODE_TYPE:
            raise PackError("unknown pack entry type %d" % type_code)
        data, pos = _inflate_at(pack, pos, trailer)
        if len(data) != size:
            raise PackError("pack entry declares %d bytes, inflated to %d" % (size, len(data)))
        entry = (entry_offset, type_code, data, base_ref)
        raw.append(entry)
        by_offset[entry_offset] = entry
    if pos != trailer:
        raise PackError("pack has stray bytes before its trailer")

    # A ref-delta names its base by the resolved object's oid, so non-delta
    # entries resolve first; deltas then resolve in fixpoint order as their
    # bases become known (a base may also live in the receiver's store).
    resolved = {}   # offset -> (type, data)
    by_oid = {}     # oid -> (type, data)
    pending = list(raw)
    while pending:
        progressed = False
        remaining = []
        for offset, type_code, data, base_ref in pending:
            if type_code in (6, 7):
                base = None
                if type_code == 6:
                    base_entry = by_offset.get(base_ref[1])
                    if base_entry is not None and base_entry[0] in resolved:
                        base = resolved[base_entry[0]]
                else:
                    base = by_oid.get(base_ref[1])
                    if base is None and external_lookup is not None:
                        base = external_lookup(base_ref[1])
                if base is None:
                    remaining.append((offset, type_code, data, base_ref))
                    continue
                obj_type, obj_data = base[0], _apply_delta(base[1], data)
            else:
                obj_type, obj_data = _CODE_TYPE[type_code], data
            resolved[offset] = (obj_type, obj_data)
            by_oid[hash_object(obj_type, obj_data)] = (obj_type, obj_data)
            progressed = True
        if not progressed:
            raise PackError("unresolvable delta base in pack")
        pending = remaining

    objects = {}
    for obj_type, obj_data in resolved.values():
        objects.setdefault(hash_object(obj_type, obj_data), (obj_type, obj_data))
    return objects


def _pack_object_header(obj_type: str, size: int) -> bytes:
    code = _TYPE_CODE[obj_type]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects) -> bytes:
    """Build a version-2 packfile from [(type, data)] with plain objects."""
    items = list(objects)
    parts = [b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")]
    for obj_type, data in items:
        parts.append(_pack_object_header(obj_type, len(data)))
        parts.append(zlib.compress(data, 6))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def pack_checksum_ok(pack: bytes) -> bool:
    if len(pack) < 20:
        return False
    return hashlib.sha1(pack[:-20]).digest() == pack[-20:]
