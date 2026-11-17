"""Smart HTTP git handlers: upload-pack (protocol v2 and v0) and
receive-pack (v0).

The wire format is pkt-lines throughout; the packfile travels inside
sideband band-1 packets. All negotiation runs on the store's object graph
via gitcore.plan_fetch, so a fetch never re-sends an object the client
already holds and a deepen returns exactly one more generation.
"""

from . import gitcore
from .consts import DEFAULT_BRANCH
from .gitobjs import ZERO_OID, is_oid, parse_tag, read_pack, write_pack, PackError
from .pktline import Decoder, PktLineError, encode_line, encode_lines

_AGENT = "forge/1.0.0"
_SIDEBAND_CHUNK = 32768


def _not_found():
    return 404, "application/json", b'{"message":"Not Found"}'


def _bad_request(message: str):
    body = ('{"message":%s}' % _json_string(message)).encode("utf-8")
    return 400, "application/json", body


def _json_string(value: str) -> str:
    import json
    return json.dumps(value)


def _sideband(data: bytes) -> bytes:
    """Wrap bytes as sideband band-1 packets, then a flush.

    The band byte is the raw byte 0x01 (band 1 = pack data), not the ASCII
    digit '1' — git rejects the ASCII digit as "bad band #49"."""
    parts = []
    for start in range(0, len(data), _SIDEBAND_CHUNK):
        parts.append(encode_line(b"\x01" + data[start:start + _SIDEBAND_CHUNK]))
    if not parts:
        parts.append(encode_line(b"\x01"))
    parts.append(b"0000")
    return b"".join(parts)


def _ref_map(store, owner: str, name: str):
    return store.list_refs(owner, name)


def _head_line(store, owner: str, name: str, repo, peel: bool):
    """The ls-refs HEAD line: `<oid> HEAD symref-target:<default>` or
    `unborn HEAD symref-target:<default>` when the default branch is empty."""
    default_ref = "refs/heads/" + repo["default_branch"]
    refs = _ref_map(store, owner, name)
    tip = refs.get(default_ref)
    if tip is None:
        return "unborn HEAD symref-target:%s" % default_ref
    return "%s HEAD symref-target:%s" % (tip, default_ref)


def _ls_refs_lines(store, owner: str, name: str, repo, prefixes, peel: bool,
                   want_symrefs: bool):
    """The ls-refs response lines (without the trailing flush)."""
    refs = _ref_map(store, owner, name)
    lines = []
    default_ref = "refs/heads/" + repo["default_branch"]

    def matches(refname: str) -> bool:
        return not prefixes or any(refname.startswith(p) for p in prefixes)

    if matches("HEAD"):
        lines.append(_head_line(store, owner, name, repo, peel))
    for refname in sorted(refs):
        if not matches(refname):
            continue
        oid = refs[refname]
        line = "%s %s" % (oid, refname)
        if peel:
            obj = store.get_object(oid)
            if obj is not None and obj[0] == "tag":
                target = parse_tag(obj[1])["object"]
                line += " peeled:%s" % target
        lines.append(line)
    return lines


# --------------------------------------------------------------------------
# info/refs
# --------------------------------------------------------------------------

def handle_info_refs(store, owner: str, name: str, service: str, headers):
    """GET /info/refs?service=... — returns (status, content-type, body)."""
    repo = store.get_repo(owner, name)
    if repo is None:
        return _not_found()
    proto = (headers.get("git-protocol") or "").strip()
    if service == "git-upload-pack" and proto == "version=2":
        lines = ["version 2", "agent=%s" % _AGENT, "ls-refs=unborn",
                 "fetch=shallow wait-for-done", "object-format=sha1"]
        body = encode_lines([l.encode("utf-8") + b"\n" for l in lines] + ["flush"])
        return 200, "application/x-git-upload-pack-advertisement", body
    if service == "git-upload-pack":
        return _info_refs_v0(store, owner, name, repo, "git-upload-pack")
    if service == "git-receive-pack":
        return _info_refs_v0(store, owner, name, repo, "git-receive-pack")
    return _not_found()


def _info_refs_v0(store, owner: str, name: str, repo, service: str):
    default_ref = "refs/heads/" + repo["default_branch"]
    if service == "git-upload-pack":
        caps = ("side-band-64k thin-pack ofs-delta shallow multi_ack "
                "symref=HEAD:%s agent=%s" % (default_ref, _AGENT))
        ctype = "application/x-git-upload-pack-advertisement"
    else:
        caps = ("report-status side-band-64k delete-refs ofs-delta "
                "symref=HEAD:%s agent=%s" % (default_ref, _AGENT))
        ctype = "application/x-git-receive-pack-advertisement"
    refs = _ref_map(store, owner, name)
    preamble = ("# service=%s\n" % service).encode("utf-8")
    lines = []
    if not refs:
        lines.append("%s capabilities^{}\0%s\n" % (ZERO_OID, caps))
    else:
        tip = refs.get(default_ref)
        if tip is not None:
            # HEAD first, carrying the capabilities, then every ref.
            lines.append("%s HEAD\0%s\n" % (tip, caps))
        for index, refname in enumerate(sorted(refs)):
            oid = refs[refname]
            line = "%s %s" % (oid, refname)
            if tip is None and index == 0:
                line += "\0" + caps
            lines.append(line + "\n")
            obj = store.get_object(oid)
            if obj is not None and obj[0] == "tag":
                target = parse_tag(obj[1])["object"]
                lines.append("%s %s^{}\n" % (target, refname))
    body = (encode_line(preamble) + b"0000"
            + b"".join(encode_line(l.encode("utf-8")) for l in lines) + b"0000")
    return 200, ctype, body


# --------------------------------------------------------------------------
# upload-pack v2
# --------------------------------------------------------------------------

def handle_upload_pack_v2(store, owner: str, name: str, body: bytes):
    repo = store.get_repo(owner, name)
    if repo is None:
        return _not_found()
    try:
        packets = _decode_or_none(body)
    except PktLineError:
        return _bad_request("malformed pkt-line stream")
    if packets is None:
        return _bad_request("malformed pkt-line stream")
    lines = [p.decode("utf-8", "replace").rstrip("\n")
             for kind, p in packets if kind == "data"]
    if not lines or not lines[0].startswith("command="):
        return _bad_request("expected a v2 command")
    command = lines[0][len("command="):].strip()
    args = lines[1:]
    if command == "ls-refs":
        return _v2_ls_refs(store, owner, name, repo, args)
    if command == "fetch":
        return _v2_fetch(store, owner, name, args)
    return _bad_request("unknown v2 command %r" % command)


def _decode_or_none(body: bytes):
    decoder = Decoder(body)
    packets = []
    while True:
        packet = decoder.next()
        if packet is None:
            break
        packets.append(packet)
    return packets


def _v2_ls_refs(store, owner: str, name: str, repo, args):
    prefixes = []
    peel = False
    for arg in args:
        if arg.startswith("ref-prefix "):
            prefixes.append(arg[len("ref-prefix "):])
        elif arg == "peel":
            peel = True
    lines = _ls_refs_lines(store, owner, name, repo, prefixes, peel, True)
    body = encode_lines([l.encode("utf-8") + b"\n" for l in lines] + ["flush"])
    return 200, "application/x-git-upload-pack-result", body


def _v2_fetch(store, owner: str, name: str, args):
    wants, haves, shallow, depth = [], [], [], None
    for arg in args:
        if arg.startswith("want "):
            oid = arg[5:].split(" ")[0]
            if is_oid(oid):
                wants.append(oid)
        elif arg.startswith("have "):
            oid = arg[5:].split(" ")[0]
            if is_oid(oid):
                haves.append(oid)
        elif arg.startswith("shallow "):
            oid = arg[8:].split(" ")[0]
            if is_oid(oid):
                shallow.append(oid)
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                depth = None
    send_oids, acks, new_shallow, unshallow = gitcore.plan_fetch(
        store.get_object, wants, haves, shallow, depth)
    objects = []
    for oid in sorted(send_oids):
        obj = store.get_object(oid)
        objects.append((obj[0], obj[1]))
    pack = write_pack(objects)
    # Sections are present only when they carry something (this is what
    # real git sends, and what the client's reader expects): an
    # acknowledgments section only when the client sent haves — its lines
    # are the ACKs (or NAK) followed by "ready" — and a shallow-info
    # section only when there are shallow/unshallow lines. The packfile
    # section is always present.
    parts = []
    if haves:
        parts.append(encode_line(b"acknowledgments\n"))
        if acks:
            for oid in acks:
                parts.append(encode_line(("ACK %s\n" % oid).encode("utf-8")))
        else:
            parts.append(encode_line(b"NAK\n"))
        parts.append(encode_line(b"ready\n"))
        parts.append(b"0001")
    if new_shallow or unshallow:
        parts.append(encode_line(b"shallow-info\n"))
        for oid in new_shallow:
            parts.append(encode_line(("shallow %s\n" % oid).encode("utf-8")))
        for oid in unshallow:
            parts.append(encode_line(("unshallow %s\n" % oid).encode("utf-8")))
        parts.append(b"0001")
    parts.append(encode_line(b"packfile\n"))
    parts.append(_sideband(pack))
    return 200, "application/x-git-upload-pack-result", b"".join(parts)


# --------------------------------------------------------------------------
# upload-pack v0
# --------------------------------------------------------------------------

def handle_upload_pack_v0(store, owner: str, name: str, body: bytes):
    repo = store.get_repo(owner, name)
    if repo is None:
        return _not_found()
    try:
        packets = _decode_or_none(body)
    except PktLineError:
        return _bad_request("malformed pkt-line stream")
    if packets is None:
        return _bad_request("malformed pkt-line stream")
    wants, haves, shallow, depth = [], [], [], None
    seen_flush = False
    for kind, payload in packets:
        if kind == "flush":
            seen_flush = True
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            rest = line[5:]
            oid = rest.split(" ")[0].split("\0")[0]
            if is_oid(oid):
                wants.append(oid)
        elif line.startswith("have "):
            oid = line[5:].split(" ")[0]
            if is_oid(oid):
                haves.append(oid)
        elif line.startswith("shallow "):
            oid = line[8:].split(" ")[0]
            if is_oid(oid):
                shallow.append(oid)
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
    send_oids, acks, new_shallow, unshallow = gitcore.plan_fetch(
        store.get_object, wants, haves, shallow, depth)
    objects = []
    for oid in sorted(send_oids):
        obj = store.get_object(oid)
        objects.append((obj[0], obj[1]))
    pack = write_pack(objects)
    # v0 has no section headers: ACK/NAK lines only when the client sent
    # haves, then shallow/unshallow lines, then the sideband pack.
    parts = []
    if haves:
        if acks:
            for oid in acks:
                parts.append(encode_line(("ACK %s\n" % oid).encode("utf-8")))
        else:
            parts.append(encode_line(b"NAK\n"))
    for oid in new_shallow:
        parts.append(encode_line(("shallow %s\n" % oid).encode("utf-8")))
    for oid in unshallow:
        parts.append(encode_line(("unshallow %s\n" % oid).encode("utf-8")))
    parts.append(_sideband(pack))
    return 200, "application/x-git-upload-pack-result", b"".join(parts)


# --------------------------------------------------------------------------
# receive-pack v0
# --------------------------------------------------------------------------

def handle_receive_pack(store, owner: str, name: str, body: bytes):
    repo = store.get_repo(owner, name)
    if repo is None:
        return _not_found()
    decoder = Decoder(body)
    commands = []
    pack = b""
    try:
        while True:
            packet = decoder.next()
            if packet is None:
                break
            kind, payload = packet
            if kind == "flush":
                break
            if kind == "data":
                commands.append(payload)
        pack = decoder.rest()
    except PktLineError:
        # The packfile itself starts with "PACK", which is not a pkt-line
        # header — reaching it means the command section is over.
        pack = decoder.rest()
    parsed = []
    for raw in commands:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        line = line.split("\0")[0]
        fields = line.split(" ")
        if len(fields) != 3 or not is_oid(fields[0]) or not is_oid(fields[1]):
            return _bad_request("malformed receive-pack command")
        parsed.append((fields[0], fields[1], fields[2]))
    if not parsed and not pack.startswith(b"PACK"):
        return _bad_request("malformed receive-pack body")

    objects = {}
    if pack.startswith(b"PACK"):
        try:
            objects = read_pack(pack, external_lookup=store.get_object)
        except PackError as err:
            return _bad_request("malformed packfile: %s" % err)
        store.put_objects(objects)

    results = []  # (refname, ok, reason)
    with store.lock:
        for old, new, refname in parsed:
            current = store.get_ref(owner, name, refname)
            if new == ZERO_OID:
                # Delete.
                if current is None:
                    results.append((refname, False, "ref does not exist"))
                elif current != old:
                    results.append((refname, False, "old value does not match"))
                else:
                    store.delete_ref(owner, name, refname)
                    results.append((refname, True, ""))
                continue
            if not store.has_object(new):
                results.append((refname, False, "object not found"))
                continue
            if old == ZERO_OID:
                # Create.
                if current is not None:
                    results.append((refname, False, "already exists"))
                    continue
            else:
                if current is None:
                    results.append((refname, False, "ref does not exist"))
                    continue
                if current != old:
                    results.append((refname, False, "old value does not match"))
                    continue
                if not gitcore.is_ancestor(store.get_object, old, new):
                    results.append((refname, False, "non-fast-forward"))
                    continue
            store.set_ref(owner, name, refname, new)
            results.append((refname, True, ""))

    report = [b"unpack ok\n"]
    for refname, ok, reason in results:
        if ok:
            report.append(("ok %s\n" % refname).encode("utf-8"))
        else:
            report.append(("ng %s %s\n" % (refname, reason)).encode("utf-8"))
    report_stream = encode_lines(report + ["flush"])
    body_out = _sideband(report_stream)
    return 200, "application/x-git-receive-pack-result", body_out
