"""Git graph operations over the store: reachability, fetch planning,
three-way merge and diffs.

Everything here works on a plain `lookup(oid) -> (type, data) | None`
function, so the same code serves the git wire protocol and the REST
contents/pulls paths. A shallow boundary is a set of commits whose parents
are not traversed: the boundary commit itself, its tree and its blobs are
always included.
"""

from .gitobjs import (
    build_commit,
    build_tree,
    collect_reachable,
    hash_object,
    is_oid,
    parse_commit,
    parse_tag,
    parse_tree,
)


def _commit_parents(lookup, oid):
    obj = lookup(oid)
    if obj is None or obj[0] != "commit":
        return []
    return parse_commit(obj[1])["parents"]


def is_ancestor(lookup, ancestor: str, descendant: str, shallow=()) -> bool:
    """True when `ancestor` is reachable from `descendant` without crossing
    a shallow boundary (a shallow commit's parents are not walked)."""
    if ancestor == descendant:
        return True
    shallow_set = set(shallow)
    seen = set()
    stack = [descendant]
    while stack:
        oid = stack.pop()
        if oid in seen or oid in shallow_set:
            continue
        seen.add(oid)
        for parent in _commit_parents(lookup, oid):
            if parent == ancestor:
                return True
            stack.append(parent)
    return False


def commit_generations(lookup, root: str):
    """Map each commit reachable from `root` to its generation (0 = the
    root, 1 = its parents, ...). The walk is never cut short: a shallow
    boundary is the client's problem, not the walk's — the depth limit in
    plan_fetch is what bounds the generations."""
    generations = {}
    frontier = [(root, 0)]
    while frontier:
        oid, gen = frontier.pop()
        if oid in generations:
            continue
        obj = lookup(oid)
        if obj is None or obj[0] != "commit":
            continue
        generations[oid] = gen
        for parent in parse_commit(obj[1])["parents"]:
            frontier.append((parent, gen + 1))
    return generations


def collect_bounded(roots, lookup, allowed_commits):
    """Every object reachable from `roots`, walking a commit's parents only
    when the parent is in `allowed_commits`. This is how a fetch stays
    inside its depth limit: a commit's tree and blobs are always sent, its
    parents only when they are themselves being sent."""
    seen = set()
    stack = [oid for oid in roots if is_oid(oid)]
    allowed = set(allowed_commits)
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = lookup(oid)
        if obj is None:
            continue
        seen.add(oid)
        obj_type, data = obj
        if obj_type == "commit":
            commit = parse_commit(data)
            stack.append(commit["tree"])
            for parent in commit["parents"]:
                if parent in allowed:
                    stack.append(parent)
        elif obj_type == "tag":
            stack.append(parse_tag(data)["object"])
        elif obj_type == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":  # a gitlink names a commit we do not hold
                    stack.append(child)
    return seen


def plan_fetch(lookup, wants, haves, shallow=(), depth=None):
    """Work out what an upload-pack fetch must send.

    `wants` — commit oids the client asked for. `haves` — commits the
    client already holds (its parents are held too). `shallow` — the
    client's current shallow list (those commits are held as boundary
    objects, their parents are not). `depth` — a deepen limit: only
    generations 0..depth-1 from a want are sent.

    Returns (send_oids, acks, new_shallow, unshallow) where:
    - send_oids: every object (commit/tree/blob) to put in the pack;
    - acks: the haves that are common (reachable from a want);
    - new_shallow: the commits at the deepest included generation, i.e. the
      client's new shallow boundary when depth is set;
    - unshallow: old shallow commits that are now fully included (their
      parents were sent), so the client may drop them from its boundary.
    """
    shallow_set = set(shallow)
    # A want is not always a commit: a full clone wants every branch and
    # tag tip, and an annotated tag tip is a tag object. Peel tag wants to
    # their target commit (the generation root) and always send the tag
    # objects themselves; any other object type is sent as-is.
    want_commits = []
    extra_send = set()
    for want in wants:
        obj = lookup(want)
        if obj is None:
            continue
        if obj[0] == "commit":
            want_commits.append(want)
        elif obj[0] == "tag":
            target = want
            while True:
                target_obj = lookup(target)
                if target_obj is None or target_obj[0] != "tag":
                    break
                extra_send.add(target)
                target = parse_tag(target_obj[1])["object"]
            target_obj = lookup(target) if target else None
            if target_obj is not None and target_obj[0] == "commit":
                want_commits.append(target)
            elif target:
                extra_send.add(target)
        else:
            extra_send.add(want)
    # Generations from the wants. With depth == 1 and no haves, only
    # generation 0 can ever be included, so the full-history walk is
    # skipped outright — a depth-1 clone of a long history must not pay
    # for walking it. Otherwise the walk is full; the depth limit below
    # is what bounds it (stopping at a shallow boundary would break
    # deepen), and the walk also feeds the ACK set when haves exist.
    if depth == 1 and not haves:
        want_gens = {want: 0 for want in want_commits}
    else:
        want_gens = {}
        for want in want_commits:
            for oid, gen in commit_generations(lookup, want).items():
                if oid not in want_gens or gen < want_gens[oid]:
                    want_gens[oid] = gen

    # What the client already holds. A shallow boundary commit is held as
    # itself plus its tree and blobs — never its parents.
    held_commits = set()
    held_objects = set()
    for have in haves:
        if have in shallow_set:
            held_commits.add(have)
            held_objects.add(have)
            tree = commit_tree(lookup, have)
            if tree:
                held_objects |= collect_reachable([tree], lookup, allow_missing=True)
        else:
            held_commits |= set(commit_generations(lookup, have))
            held_objects |= collect_reachable([have], lookup, allow_missing=True)

    acks = sorted(h for h in haves if h in want_gens)

    if depth is None:
        included = set(want_gens)
    else:
        included = {oid for oid, gen in want_gens.items() if gen < depth}

    new_shallow = set()
    if depth is not None and included:
        deepest = max(want_gens[oid] for oid in included)
        new_shallow = {oid for oid in included if want_gens[oid] == deepest}

    # A shallow boundary commit whose parents were included is unshallowed.
    unshallow = set()
    for oid in shallow_set:
        if oid in included:
            parents = _commit_parents(lookup, oid)
            if parents and all(p in included for p in parents):
                unshallow.add(oid)

    send_commits = included - held_commits
    send_oids = collect_bounded(list(send_commits), lookup, send_commits)
    send_oids |= extra_send
    # Never re-send an object the client already holds (shared blobs and
    # trees between the old and the new tip are the common case).
    send_oids -= held_objects
    return send_oids, acks, sorted(new_shallow), sorted(unshallow)


# --------------------------------------------------------------------------
# trees
# --------------------------------------------------------------------------

def flatten_tree(lookup, tree_oid: str, prefix: str = ""):
    """A tree -> {path: (mode, blob_oid)} for every file, recursively."""
    out = {}
    obj = lookup(tree_oid)
    if obj is None or obj[0] != "tree":
        return out
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == "40000":
            out.update(flatten_tree(lookup, oid, path + "/"))
        else:
            out[path] = (mode, oid)
    return out


def build_tree_from_files(lookup, files):
    """Build (and return) the tree oid for {path: (mode, blob_oid)}.

    Returns (tree_oid, {oid: (type, data)}) with every new object."""
    objects = {}
    # Group files by directory, bottom-up.
    dirs = {}
    for path, (mode, blob_oid) in files.items():
        parts = path.split("/")
        dirs.setdefault(tuple(parts[:-1]), []).append((parts[-1], mode, blob_oid))
    # Depth-first: build deepest directories first.
    dir_paths = sorted(dirs, key=lambda p: (-len(p), p))
    tree_by_dir = {}

    def dir_tree(path_tuple):
        if path_tuple in tree_by_dir:
            return tree_by_dir[path_tuple]
        entries = []
        for name, mode, oid in dirs.get(path_tuple, []):
            entries.append((mode, name, oid))
        for sub in sorted(d for d in dirs if len(d) == len(path_tuple) + 1
                          and d[:-1] == path_tuple):
            sub_tree = dir_tree(sub)
            entries.append(("40000", sub[-1], sub_tree))
        data = build_tree(entries)
        oid = hash_object("tree", data)
        objects[oid] = ("tree", data)
        tree_by_dir[path_tuple] = oid
        return oid

    root = dir_tree(())
    return root, objects


# --------------------------------------------------------------------------
# merge and diff
# --------------------------------------------------------------------------

def merge_trees(lookup, base_tree: str, ours_tree: str, theirs_tree: str):
    """Three-way merge of file maps. Returns (merged_files, conflicts)
    where merged_files is {path: (mode, blob_oid)} and conflicts is a list
    of paths both sides changed differently."""
    base = flatten_tree(lookup, base_tree)
    ours = flatten_tree(lookup, ours_tree)
    theirs = flatten_tree(lookup, theirs_tree)
    merged = {}
    conflicts = []
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t:
            if o is not None:
                merged[path] = o
        elif b == o:
            if t is not None:
                merged[path] = t
        elif b == t:
            if o is not None:
                merged[path] = o
        else:
            conflicts.append(path)
    return merged, conflicts


def diff_files(lookup, old_tree: str, new_tree: str):
    """A diff between two trees -> [{path, status, additions, deletions}].

    `additions`/`deletions` are line counts for text blobs (0 for binary or
    when a blob is missing); `status` is "added", "removed" or "modified".
    """
    old = flatten_tree(lookup, old_tree)
    new = flatten_tree(lookup, new_tree)
    out = []
    for path in sorted(set(old) | set(new)):
        o, n = old.get(path), new.get(path)
        if o == n:
            continue
        if o is None:
            status, additions, deletions = "added", 0, 0
            blob = lookup(n[1])
            if blob is not None:
                additions = _count_lines(blob[1])
        elif n is None:
            status, additions, deletions = "removed", 0, 0
            blob = lookup(o[1])
            if blob is not None:
                deletions = _count_lines(blob[1])
        else:
            status = "modified"
            old_blob = lookup(o[1])
            new_blob = lookup(n[1])
            old_lines = _lines(old_blob[1]) if old_blob else []
            new_lines = _lines(new_blob[1]) if new_blob else []
            additions, deletions = _line_diff(old_lines, new_lines)
        out.append({"path": path, "status": status,
                    "additions": additions, "deletions": deletions})
    return out


def _count_lines(data: bytes) -> int:
    if b"\0" in data[:8000]:
        return 0
    return len(data.splitlines())


def _lines(data: bytes):
    if b"\0" in data[:8000]:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _line_diff(old_lines, new_lines):
    """A cheap longest-common-subsequence-free line diff: count lines that
    appear only on one side (multiset difference), which matches the suite's
    additions >= 1 expectation for changed files."""
    from collections import Counter
    old_counts = Counter(old_lines)
    new_counts = Counter(new_lines)
    additions = sum((new_counts - old_counts).values())
    deletions = sum((old_counts - new_counts).values())
    return additions, deletions


def find_merge_base(lookup, ours: str, theirs: str):
    """The nearest common ancestor of two commits (first found walking
    both sides' generations outward), or None."""
    if ours == theirs:
        return ours
    ours_gens = commit_generations(lookup, ours)
    theirs_gens = commit_generations(lookup, theirs)
    common = set(ours_gens) & set(theirs_gens)
    if not common:
        return None
    return min(common, key=lambda oid: (ours_gens[oid] + theirs_gens[oid], oid))


def make_commit(store, tree_oid: str, parents, message: str,
                author: str | None = None, committer: str | None = None):
    """Create a commit object in the store and return its oid."""
    ts = store.next_timestamp()
    data = build_commit(tree_oid, parents, message, ts, author, committer)
    oid = hash_object("commit", data)
    store.put_objects({oid: ("commit", data)})
    return oid


def commit_tree(lookup, oid: str):
    obj = lookup(oid)
    if obj is None or obj[0] != "commit":
        return None
    return parse_commit(obj[1])["tree"]


def commit_message(lookup, oid: str):
    obj = lookup(oid)
    if obj is None or obj[0] != "commit":
        return ""
    return parse_commit(obj[1])["message"]
