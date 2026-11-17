"""REST v3 handlers (the /api/v3 surface).

Every handler returns (status, payload, extra_headers); the server layer
serialises the payload to JSON and adds X-GitHub-Request-Id. A payload of
None means an empty body (204). Errors are JSON {"message": ...} with the
status GitHub itself would send: 404 for a missing repository or a
repository under another owner (never 403), 422 for a duplicate name, a
duplicate open pull request or an unknown label, 409 for a stale blob sha.
"""

import base64

from . import gitcore
from .consts import (
    DEFAULT_BRANCH,
    FIXED_ISO,
    FORGE_VERSION,
    VIEWER_EMAIL,
    VIEWER_ID,
    VIEWER_LOGIN,
    VIEWER_NAME,
)
from .gitobjs import build_tree, hash_object, is_oid, parse_commit

# The labels GitHub seeds on every new repository; an issue may name any of
# these without the caller creating the label first.
DEFAULT_LABELS = [
    ("bug", "d73a4a"),
    ("enhancement", "a2eeef"),
    ("documentation", "0075ca"),
    ("question", "d876e3"),
    ("good first issue", "7057ff"),
    ("help wanted", "008672"),
    ("duplicate", "cfd3d7"),
    ("invalid", "e4e669"),
    ("wontfix", "ffffff"),
]


class RestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _not_found(message="Not Found"):
    raise RestError(404, message)


def _unprocessable(message):
    raise RestError(422, message)


def _conflict(message):
    raise RestError(409, message)


# --------------------------------------------------------------------------
# serialisers
# --------------------------------------------------------------------------

def _viewer():
    return {"login": VIEWER_LOGIN, "id": VIEWER_ID, "name": VIEWER_NAME,
            "email": VIEWER_EMAIL, "type": "User"}


def _repo_view(repo):
    return {"id": repo["id"], "name": repo["name"],
            "full_name": repo["full_name"], "private": repo["private"],
            "description": repo["description"],
            "default_branch": repo["default_branch"],
            "owner": {"login": repo["owner"], "id": VIEWER_ID, "type": "User"},
            "created_at": repo["created_at"], "updated_at": repo["created_at"],
            "html_url": "https://example.invalid/%s" % repo["full_name"],
            "url": "/api/v3/repos/%s" % repo["full_name"]}


def _issue_view(store, owner: str, repo, issue):
    return {"id": repo["id"] * 1000000 + issue["number"],
            "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "state": issue["state"],
            "labels": [{"name": name} for name in issue["labels"]],
            "user": {"login": VIEWER_LOGIN}, "assignee": None,
            "created_at": issue["created_at"], "updated_at": issue["created_at"],
            "html_url": "https://example.invalid/%s/issues/%d"
                        % (repo["full_name"], issue["number"])}


def _pr_view(store, owner: str, repo, pr):
    return {"id": repo["id"] * 1000000 + pr["number"],
            "number": pr["number"], "title": pr["title"], "body": pr["body"],
            "state": pr["state"], "merged": pr["merged"],
            "head": {"ref": pr["head_ref"], "sha": pr["head_sha"],
                     "label": "%s:%s" % (owner, pr["head_ref"])},
            "base": {"ref": pr["base_ref"], "sha": pr["base_sha"],
                     "label": "%s:%s" % (owner, pr["base_ref"])},
            "merged_at": pr["merged_at"],
            "merge_commit_sha": pr["merge_commit_sha"],
            "user": {"login": VIEWER_LOGIN},
            "created_at": pr["created_at"], "updated_at": pr["created_at"]}


def _comment_view(comment):
    return {"id": comment["id"], "body": comment["body"],
            "user": {"login": VIEWER_LOGIN},
            "created_at": comment["created_at"],
            "updated_at": comment["created_at"]}


def _branch_view(name: str, sha: str):
    return {"name": name, "commit": {"sha": sha},
            "protected": False}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _require_repo(store, owner: str, name: str):
    """A repository under any owner other than the viewer is a 404."""
    if owner != VIEWER_LOGIN:
        _not_found()
    repo = store.get_repo(owner, name)
    if repo is None:
        _not_found()
    return repo


def _paginate(items, query, host: str, raw_path: str):
    """Slice a list per ?page=&per_page=; returns (page, link_header)."""
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    per_page = max(1, min(per_page, 100))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    sliced = items[start:start + per_page]
    link = None
    if start + per_page < len(items):
        base = raw_path.split("?")[0]
        link = ('<http://%s%s?per_page=%d&page=%d>; rel="next"'
                % (host, base, per_page, page + 1))
    return sliced, link


def _branch_tip(store, owner: str, name: str, branch: str):
    return store.get_ref(owner, name, "refs/heads/" + branch)


def _resolve_ref(store, owner: str, name: str, ref: str):
    """Resolve a ?ref= / ?sha= value to a commit oid: a branch name, a full
    ref, or a bare oid."""
    if not ref:
        return None
    if is_oid(ref):
        return ref if store.has_object(ref) else None
    if ref.startswith("refs/"):
        return store.get_ref(owner, name, ref)
    return store.get_ref(owner, name, "refs/heads/" + ref)


def _commit_list(store, owner: str, name: str, tip: str):
    """Commits reachable from `tip`, newest first (breadth-first by
    generation, so a merge commit precedes its parents)."""
    out = []
    seen = set()
    frontier = [tip]
    while frontier:
        nxt = []
        for oid in frontier:
            if oid in seen:
                continue
            obj = store.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            seen.add(oid)
            commit = parse_commit(obj[1])
            out.append({"sha": oid,
                        "commit": {"message": commit["message"],
                                   "author": {"name": VIEWER_NAME,
                                              "email": VIEWER_EMAIL},
                                   "committer": {"name": VIEWER_NAME,
                                                 "email": VIEWER_EMAIL}},
                        "parents": [{"sha": p} for p in commit["parents"]]})
            nxt.extend(commit["parents"])
        frontier = nxt
    return out


def _tree_files_at(store, owner: str, name: str, tip: str):
    """{path: (mode, blob_oid)} for the tree at `tip` ({} when unborn)."""
    if not tip:
        return {}
    tree = gitcore.commit_tree(store.get_object, tip)
    if not tree:
        return {}
    return gitcore.flatten_tree(store.get_object, tree)


def _write_tree_commit(store, owner: str, name: str, files, parent, message: str):
    """Build the tree for `files`, commit it on `parent`, return the commit
    oid. `files` is {path: (mode, blob_oid)}."""
    tree_oid, new_objects = gitcore.build_tree_from_files(store.get_object, files)
    store.put_objects(new_objects)
    parents = [parent] if parent else []
    return gitcore.make_commit(store, tree_oid, parents, message)


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------

def handle_rest(store, method: str, segments, query, body, host: str, raw_path: str):
    """Route a REST request. `segments` is the path after /api/v3/ split on
    '/'. Returns (status, payload, extra_headers)."""
    try:
        return _dispatch(store, method, segments, query, body, host, raw_path)
    except RestError as err:
        return err.status, {"message": err.message}, {}


def _dispatch(store, method, segments, query, body, host, raw_path):
    if not segments:
        if method == "GET":
            return 200, {"current_user_url": "/api/v3/user",
                         "rate_limit_url": "/api/v3/rate_limit"}, \
                   {"X-OAuth-Scopes": "repo, read:org"}
        _not_found()

    head = segments[0]

    if head == "meta" and len(segments) == 1 and method == "GET":
        return 200, {"installed_version": FORGE_VERSION}, {}

    if head == "user" and len(segments) == 1:
        if method == "GET":
            return 200, _viewer(), {}
        _not_found()

    if head == "user" and len(segments) == 2 and segments[1] == "repos":
        if method == "POST":
            return _create_repo(store, body)
        if method == "GET":
            repos = [_repo_view(r) for r in store.list_repos(VIEWER_LOGIN)]
            page, link = _paginate(repos, query, host, raw_path)
            headers = {"Link": link} if link else {}
            return 200, page, headers
        _not_found()

    if head == "repos":
        return _repo_routes(store, method, segments, query, body, host, raw_path)

    _not_found()


def _create_repo(store, body):
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        _unprocessable("name is required")
    if store.get_repo(VIEWER_LOGIN, name) is not None:
        _unprocessable("name already exists on this account")
    repo = store.create_repo(
        VIEWER_LOGIN, name,
        description=body.get("description") or "",
        private=bool(body.get("private", False)),
        default_branch=body.get("default_branch") or DEFAULT_BRANCH)
    for label_name, color in DEFAULT_LABELS:
        store.add_label(VIEWER_LOGIN, name, label_name, color)
    return 201, _repo_view(repo), {}


def _repo_routes(store, method, segments, query, body, host, raw_path):
    if len(segments) < 3:
        _not_found()
    owner, name = segments[1], segments[2]
    repo = _require_repo(store, owner, name)
    rest = segments[3:]

    if not rest:
        if method == "GET":
            return 200, _repo_view(repo), {}
        if method == "PATCH":
            body = body or {}
            repo = store.update_repo(owner, name,
                                     description=body.get("description"),
                                     private=body.get("private")
                                     if "private" in body else None)
            return 200, _repo_view(repo), {}
        if method == "DELETE":
            store.delete_repo(owner, name)
            return 204, None, {}
        _not_found()

    section = rest[0]

    if section == "branches":
        return _branch_routes(store, method, owner, name, repo, rest[1:])
    if section == "commits" and method == "GET" and len(rest) == 1:
        return _list_commits(store, owner, name, repo, query)
    if section == "git" and len(rest) >= 2 and rest[1] == "refs":
        return _git_ref_routes(store, method, owner, name, repo, rest[2:], body)
    if section == "contents":
        return _contents_routes(store, method, owner, name, repo,
                                rest[1:], query, body)
    if section == "labels":
        return _label_routes(store, method, owner, name, repo, body)
    if section == "issues":
        return _issue_routes(store, method, owner, name, repo, rest[1:],
                             query, body, host, raw_path)
    if section == "pulls":
        return _pull_routes(store, method, owner, name, repo, rest[1:],
                            query, body, host, raw_path)
    _not_found()


# --------------------------------------------------------------------------
# branches and commits
# --------------------------------------------------------------------------

def _branch_routes(store, method, owner, name, repo, rest):
    if method != "GET":
        _not_found()
    refs = store.list_refs(owner, name)
    branches = {r[len("refs/heads/"):]: oid
                for r, oid in refs.items() if r.startswith("refs/heads/")}
    if not rest:
        return 200, [_branch_view(b, oid) for b, oid in sorted(branches.items())], {}
    if len(rest) == 1:
        branch = rest[0]
        if branch not in branches:
            _not_found("Branch not found")
        return 200, _branch_view(branch, branches[branch]), {}
    _not_found()


def _list_commits(store, owner, name, repo, query):
    ref = query.get("sha", [None])[0]
    if ref:
        tip = _resolve_ref(store, owner, name, ref)
        if tip is None:
            _not_found("No commit found for SHA: %s" % ref)
    else:
        tip = _branch_tip(store, owner, name, repo["default_branch"])
        if tip is None:
            return 200, [], {}
    return 200, _commit_list(store, owner, name, tip), {}


# --------------------------------------------------------------------------
# git refs
# --------------------------------------------------------------------------

def _git_ref_routes(store, method, owner, name, repo, rest, body):
    if method == "POST" and not rest:
        body = body or {}
        refname = body.get("ref") or ""
        sha = body.get("sha") or ""
        if not refname.startswith("refs/") or not is_oid(sha):
            _unprocessable("ref and sha are required")
        if not store.has_object(sha):
            _unprocessable("object does not exist")
        if store.get_ref(owner, name, refname) is not None:
            _unprocessable("Reference already exists")
        store.set_ref(owner, name, refname, sha)
        return 201, {"ref": refname,
                     "object": {"sha": sha, "type": "commit"}}, {}
    if method == "GET" and len(rest) >= 2 and rest[0] == "heads":
        branch = "/".join(rest[1:])
        refname = "refs/heads/" + branch
        oid = store.get_ref(owner, name, refname)
        if oid is None:
            _not_found("Reference does not exist")
        return 200, {"ref": refname,
                     "object": {"sha": oid, "type": "commit"}}, {}
    _not_found()


# --------------------------------------------------------------------------
# contents
# --------------------------------------------------------------------------

def _contents_routes(store, method, owner, name, repo, rest, query, body):
    if not rest:
        # GET /contents with no path = the root directory listing.
        if method == "GET":
            return _contents_get(store, owner, name, repo, "", query)
        _not_found()
    path = "/".join(rest)
    if method == "GET":
        return _contents_get(store, owner, name, repo, path, query)
    if method == "PUT":
        return _contents_put(store, owner, name, repo, path, body)
    if method == "DELETE":
        return _contents_delete(store, owner, name, repo, path, body)
    _not_found()


def _contents_get(store, owner, name, repo, path, query):
    ref = query.get("ref", [None])[0]
    if ref:
        tip = _resolve_ref(store, owner, name, ref)
        if tip is None:
            _not_found("No commit found for SHA: %s" % ref)
    else:
        tip = _branch_tip(store, owner, name, repo["default_branch"])
    if tip is None:
        _not_found("This repository is empty.")
    files = _tree_files_at(store, owner, name, tip)
    if path in files:
        mode, blob_oid = files[path]
        obj = store.get_object(blob_oid)
        data = obj[1] if obj else b""
        return 200, {"type": "file", "encoding": "base64",
                     "content": base64.b64encode(data).decode("ascii"),
                     "sha": blob_oid, "size": len(data),
                     "name": path.split("/")[-1], "path": path}, {}
    prefix = path + "/" if path else ""
    entries = []
    seen_dirs = set()
    for file_path, (mode, blob_oid) in sorted(files.items()):
        if not file_path.startswith(prefix):
            continue
        remainder = file_path[len(prefix):]
        if "/" in remainder:
            dirname = remainder.split("/")[0]
            if dirname not in seen_dirs:
                seen_dirs.add(dirname)
                entries.append({"type": "dir", "name": dirname,
                                "path": prefix + dirname, "sha": ""})
        else:
            entries.append({"type": "file", "name": remainder,
                            "path": file_path, "sha": blob_oid})
    if not entries and path:
        _not_found("Not Found")
    return 200, entries, {}


def _contents_put(store, owner, name, repo, path, body):
    body = body or {}
    message = body.get("message") or "Update %s" % path
    content_b64 = body.get("content")
    if content_b64 is None:
        _unprocessable("content is required")
    try:
        data = base64.b64decode(content_b64, validate=False)
    except Exception:
        _unprocessable("content is not valid base64")
    branch = body.get("branch") or repo["default_branch"]
    refname = "refs/heads/" + branch
    with store.lock:
        tip = store.get_ref(owner, name, refname)
        if tip is None and branch != repo["default_branch"]:
            # A new branch starts from the default branch's tip.
            tip = _branch_tip(store, owner, name, repo["default_branch"])
        files = _tree_files_at(store, owner, name, tip)
        existed = path in files
        if "sha" in body and body.get("sha") is not None:
            if not existed or files[path][1] != body["sha"]:
                _conflict("sha does not match the current blob")
        blob_oid = hash_object("blob", data)
        store.put_objects({blob_oid: ("blob", data)})
        files[path] = ("100644", blob_oid)
        new_tip = _write_tree_commit(store, owner, name, files, tip, message)
        store.set_ref(owner, name, refname, new_tip)
    return (200 if existed else 201,
            {"content": {"name": path.split("/")[-1], "path": path,
                         "sha": blob_oid, "size": len(data),
                         "encoding": "base64"},
             "commit": {"sha": new_tip}}, {})


def _contents_delete(store, owner, name, repo, path, body):
    body = body or {}
    message = body.get("message") or "Delete %s" % path
    branch = body.get("branch") or repo["default_branch"]
    refname = "refs/heads/" + branch
    with store.lock:
        tip = store.get_ref(owner, name, refname)
        if tip is None:
            _not_found("Not Found")
        files = _tree_files_at(store, owner, name, tip)
        if path not in files:
            _not_found("Not Found")
        if body.get("sha") is not None and files[path][1] != body["sha"]:
            _conflict("sha does not match the current blob")
        del files[path]
        new_tip = _write_tree_commit(store, owner, name, files, tip, message)
        store.set_ref(owner, name, refname, new_tip)
    return 200, {"commit": {"sha": new_tip}}, {}


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------

def _label_routes(store, method, owner, name, repo, body):
    if method == "GET":
        labels = store.get_labels(owner, name)
        return 200, [{"name": l["name"], "color": l["color"]} for l in labels], {}
    if method == "POST":
        body = body or {}
        label_name = (body.get("name") or "").strip()
        if not label_name:
            _unprocessable("name is required")
        label = store.add_label(owner, name, label_name, body.get("color") or "")
        return 201, {"name": label["name"], "color": label["color"]}, {}
    _not_found()


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------

def _issue_routes(store, method, owner, name, repo, rest, query, body, host, raw_path):
    if not rest:
        if method == "POST":
            return _create_issue(store, owner, name, repo, body)
        if method == "GET":
            return _list_issues(store, owner, name, repo, query, host, raw_path)
        _not_found()
    try:
        number = int(rest[0])
    except ValueError:
        _not_found()
    if len(rest) == 1:
        if method == "GET":
            issue = store.get_issue(owner, name, number)
            if issue is None:
                _not_found("Issue not found")
            return 200, _issue_view(store, owner, repo, issue), {}
        if method == "PATCH":
            body = body or {}
            state = body.get("state")
            if state is not None and state not in ("open", "closed"):
                _unprocessable("state must be open or closed")
            issue = store.update_issue(owner, name, number,
                                       title=body.get("title"),
                                       body=body.get("body"),
                                       state=state)
            if issue is None:
                _not_found("Issue not found")
            return 200, _issue_view(store, owner, repo, issue), {}
        _not_found()
    if len(rest) == 2 and rest[1] == "comments":
        if method == "POST":
            return _create_comment(store, owner, name, repo, number, body)
        if method == "GET":
            if store.get_issue(owner, name, number) is None:
                _not_found("Issue not found")
            comments = [_comment_view(c)
                        for c in store.list_comments(owner, name, number)]
            return 200, comments, {}
        _not_found()
    _not_found()


def _create_issue(store, owner, name, repo, body):
    body = body or {}
    title = (body.get("title") or "").strip()
    if not title:
        _unprocessable("title is required")
    label_names = body.get("labels") or []
    if isinstance(label_names, str):
        label_names = [label_names]
    known = {l["name"] for l in store.get_labels(owner, name)}
    for label_name in label_names:
        if label_name not in known:
            _unprocessable("Label '%s' does not exist" % label_name)
    issue = store.create_issue(owner, name, title, body.get("body") or "",
                               label_names)
    return 201, _issue_view(store, owner, repo, issue), {}


def _list_issues(store, owner, name, repo, query, host, raw_path):
    state = query.get("state", ["open"])[0]
    sort = query.get("sort", ["created"])[0]
    direction = query.get("direction", ["desc"])[0]
    issues = store.list_issues(owner, name)
    if state in ("open", "closed"):
        issues = [i for i in issues if i["state"] == state]
    reverse = direction == "desc"
    if sort == "created":
        issues = sorted(issues, key=lambda i: i["number"], reverse=reverse)
    views = [_issue_view(store, owner, repo, i) for i in issues]
    page, link = _paginate(views, query, host, raw_path)
    headers = {"Link": link} if link else {}
    return 200, page, headers


def _create_comment(store, owner, name, repo, number, body):
    body = body or {}
    comment_body = body.get("body") or ""
    if store.get_issue(owner, name, number) is None:
        _not_found("Issue not found")
    comment = store.add_comment(owner, name, number, comment_body)
    return 201, _comment_view(comment), {}


# --------------------------------------------------------------------------
# pull requests
# --------------------------------------------------------------------------

def _pull_routes(store, method, owner, name, repo, rest, query, body, host, raw_path):
    if not rest:
        if method == "POST":
            return _create_pr(store, owner, name, repo, body)
        if method == "GET":
            return _list_prs(store, owner, name, repo, query, host, raw_path)
        _not_found()
    try:
        number = int(rest[0])
    except ValueError:
        _not_found()
    if len(rest) == 1:
        if method == "GET":
            pr = store.get_pr(owner, name, number)
            if pr is None:
                _not_found("Pull request not found")
            return 200, _pr_view(store, owner, repo, pr), {}
        _not_found()
    if len(rest) == 2 and rest[1] == "files" and method == "GET":
        pr = store.get_pr(owner, name, number)
        if pr is None:
            _not_found("Pull request not found")
        return 200, _pr_files(store, owner, name, pr), {}
    if len(rest) == 2 and rest[1] == "merge" and method == "PUT":
        return _merge_pr(store, owner, name, repo, number, body)
    _not_found()


def _create_pr(store, owner, name, repo, body):
    body = body or {}
    title = (body.get("title") or "").strip()
    head = body.get("head") or ""
    base = body.get("base") or repo["default_branch"]
    if not title:
        _unprocessable("title is required")
    if not head:
        _unprocessable("head is required")
    head_sha = _branch_tip(store, owner, name, head)
    base_sha = _branch_tip(store, owner, name, base)
    if head_sha is None:
        _unprocessable("head branch '%s' does not exist" % head)
    if base_sha is None:
        _unprocessable("base branch '%s' does not exist" % base)
    if head_sha == base_sha:
        _unprocessable("No commits between %s and %s" % (head, base))
    for existing in store.list_prs(owner, name):
        if (existing["state"] == "open" and existing["head_ref"] == head
                and existing["base_ref"] == base):
            _unprocessable("A pull request already exists for %s" % head)
    pr = store.create_pr(owner, name, title, body.get("body") or "",
                         head, head_sha, base, base_sha)
    return 201, _pr_view(store, owner, repo, pr), {}


def _list_prs(store, owner, name, repo, query, host, raw_path):
    state = query.get("state", ["open"])[0]
    prs = store.list_prs(owner, name)
    if state == "open":
        prs = [p for p in prs if p["state"] == "open"]
    elif state == "closed":
        prs = [p for p in prs if p["state"] == "closed"]
    views = [_pr_view(store, owner, repo, p) for p in prs]
    page, link = _paginate(views, query, host, raw_path)
    headers = {"Link": link} if link else {}
    return 200, page, headers


def _pr_files(store, owner, name, pr):
    base_tip = _branch_tip(store, owner, name, pr["base_ref"]) or pr["base_sha"]
    head_tip = _branch_tip(store, owner, name, pr["head_ref"]) or pr["head_sha"]
    merge_base = gitcore.find_merge_base(store.get_object, base_tip, head_tip)
    if merge_base is None:
        # No common history: diff the head against an empty tree.
        old_tree, new_tree = "", gitcore.commit_tree(store.get_object, head_tip)
    else:
        old_tree = gitcore.commit_tree(store.get_object, merge_base)
        new_tree = gitcore.commit_tree(store.get_object, head_tip)
    files = []
    for entry in gitcore.diff_files(store.get_object, old_tree or "", new_tree or ""):
        files.append({"filename": entry["path"], "status": entry["status"],
                      "additions": entry["additions"],
                      "deletions": entry["deletions"],
                      "changes": entry["additions"] + entry["deletions"]})
    return files


def _merge_pr(store, owner, name, repo, number, body):
    body = body or {}
    pr = store.get_pr(owner, name, number)
    if pr is None:
        _not_found("Pull request not found")
    if pr["state"] != "open":
        raise RestError(405, "Pull request is already closed")
    merge_method = body.get("merge_method") or "squash"
    if merge_method != "squash":
        _unprocessable("only squash merges are supported")
    with store.lock:
        base_tip = _branch_tip(store, owner, name, pr["base_ref"]) or pr["base_sha"]
        head_tip = _branch_tip(store, owner, name, pr["head_ref"]) or pr["head_sha"]
        merge_base = gitcore.find_merge_base(store.get_object, base_tip, head_tip)
        if merge_base is None:
            raise RestError(405, "No common ancestor for this pull request")
        base_tree = gitcore.commit_tree(store.get_object, base_tip)
        head_tree = gitcore.commit_tree(store.get_object, head_tip)
        merge_base_tree = gitcore.commit_tree(store.get_object, merge_base)
        merged, conflicts = gitcore.merge_trees(
            store.get_object, merge_base_tree or "", base_tree or "",
            head_tree or "")
        if conflicts:
            raise RestError(405, "Merge conflict")
        merge_sha = _write_tree_commit(
            store, owner, name, merged, base_tip,
            pr["title"] or ("Merge pull request #%d" % number))
        store.set_ref(owner, name, "refs/heads/" + pr["base_ref"], merge_sha)
        store.update_pr(owner, name, number, state="closed", merged=True,
                        merge_commit_sha=merge_sha, merged_at=FIXED_ISO,
                        base_sha=merge_sha, head_sha=head_tip)
    return 200, {"sha": merge_sha, "merged": True,
                 "message": pr["title"] or "Merged"}, {}
