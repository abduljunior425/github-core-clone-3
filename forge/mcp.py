"""MCP (the /mcp endpoint): JSON-RPC 2.0 over Streamable HTTP, plain JSON
answers (no SSE).

The modern handshake is `server/discover` (protocol revision 2026-07-28);
the legacy `initialize` handshake is supported too. The tool list is a
module-level constant so two `tools/list` calls answer byte-for-byte
identical JSON. Tool arguments use the canonical property names the suite
passes (owner, repo, title, head, base, pull_number) and `required` lists
only what the suite supplies. Every tool reads and writes the same store
as REST and GraphQL.
"""

import json

from .consts import MCP_SERVER_NAME, MCP_SERVER_VERSION, VIEWER_LOGIN
from .graphql import _merge_pr
from .rest import DEFAULT_LABELS, _issue_view, _pr_view, _repo_view

_SUPPORTED_VERSIONS = ["2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05"]
_LEGACY_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _server_info():
    return {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}


def _result(request_id, payload):
    return 200, {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return 200, {"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository owned by the viewer.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "name": {"type": "string",
                      "description": "The repository name."},
             "description": {"type": "string",
                             "description": "A short description."},
             "private": {"type": "boolean",
                         "description": "Whether the repository is private."}},
         "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "owner": {"type": "string",
                       "description": "The repository owner login."},
             "repo": {"type": "string",
                      "description": "The repository name."}},
         "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "owner": {"type": "string",
                       "description": "The repository owner login."},
             "repo": {"type": "string",
                      "description": "The repository name."},
             "title": {"type": "string",
                       "description": "The issue title."},
             "body": {"type": "string",
                      "description": "The issue body."}},
         "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "owner": {"type": "string",
                       "description": "The repository owner login."},
             "repo": {"type": "string",
                      "description": "The repository name."},
             "state": {"type": "string",
                       "description": "Filter by state: open, closed or all."}},
         "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Open a pull request from a head branch onto a base branch.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "owner": {"type": "string",
                       "description": "The repository owner login."},
             "repo": {"type": "string",
                      "description": "The repository name."},
             "title": {"type": "string",
                       "description": "The pull request title."},
             "head": {"type": "string",
                      "description": "The head branch name."},
             "base": {"type": "string",
                      "description": "The base branch name."},
             "body": {"type": "string",
                      "description": "The pull request body."}},
         "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Squash-merge a pull request.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "owner": {"type": "string",
                       "description": "The repository owner login."},
             "repo": {"type": "string",
                      "description": "The repository name."},
             "pull_number": {"type": "integer",
                             "description": "The pull request number."}},
         "required": ["owner", "repo", "pull_number"]}},
]

_TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _tool_result(request_id, payload, structured=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    result = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        result["structuredContent"] = structured
    return _result(request_id, result)


def _tool_error(request_id, message):
    return _result(request_id, {
        "content": [{"type": "text", "text": message}],
        "isError": True})


def _call_tool(store, request_id, name, arguments):
    if name not in _TOOL_NAMES:
        return _error(request_id, -32601, "Unknown tool '%s'" % name)
    arguments = arguments or {}
    tool = next(t for t in TOOLS if t["name"] == name)
    for required in tool["inputSchema"].get("required", []):
        if required not in arguments or arguments[required] is None:
            return _error(request_id, -32602,
                          "Missing required argument '%s'" % required)
    handler = _TOOL_HANDLERS[name]
    try:
        return handler(store, request_id, arguments)
    except Exception as err:  # a tool failure is a result, never a 500
        return _tool_error(request_id, "Tool '%s' failed: %s" % (name, err))


def _create_repo(store, request_id, args):
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error(request_id, "name is required")
    if store.get_repo(VIEWER_LOGIN, name) is not None:
        return _tool_error(request_id, "name already exists on this account")
    repo = store.create_repo(VIEWER_LOGIN, name,
                             description=args.get("description") or "",
                             private=bool(args.get("private", False)))
    for label_name, color in DEFAULT_LABELS:
        store.add_label(VIEWER_LOGIN, name, label_name, color)
    view = _repo_view(repo)
    return _tool_result(request_id, view, view)


def _get_repo(store, request_id, args):
    owner, name = args.get("owner"), args.get("repo")
    if owner != VIEWER_LOGIN:
        return _tool_error(request_id, "Not Found")
    repo = store.get_repo(owner, name)
    if repo is None:
        return _tool_error(request_id, "Not Found")
    view = _repo_view(repo)
    return _tool_result(request_id, view, view)


def _create_issue(store, request_id, args):
    owner, name = args.get("owner"), args.get("repo")
    if owner != VIEWER_LOGIN or store.get_repo(owner, name) is None:
        return _tool_error(request_id, "Not Found")
    issue = store.create_issue(owner, name, args.get("title") or "",
                               args.get("body") or "")
    view = _issue_view(store, owner, store.get_repo(owner, name), issue)
    return _tool_result(request_id, view, view)


def _list_issues(store, request_id, args):
    owner, name = args.get("owner"), args.get("repo")
    if owner != VIEWER_LOGIN or store.get_repo(owner, name) is None:
        return _tool_error(request_id, "Not Found")
    repo = store.get_repo(owner, name)
    state = args.get("state") or "open"
    issues = store.list_issues(owner, name)
    if state in ("open", "closed"):
        issues = [i for i in issues if i["state"] == state]
    views = [_issue_view(store, owner, repo, i) for i in issues]
    return _tool_result(request_id, views, {"issues": views})


def _create_pull_request(store, request_id, args):
    owner, name = args.get("owner"), args.get("repo")
    if owner != VIEWER_LOGIN or store.get_repo(owner, name) is None:
        return _tool_error(request_id, "Not Found")
    head, base = args.get("head"), args.get("base")
    head_sha = store.get_ref(owner, name, "refs/heads/" + head)
    base_sha = store.get_ref(owner, name, "refs/heads/" + base)
    if head_sha is None or base_sha is None:
        return _tool_error(request_id, "head or base branch not found")
    for existing in store.list_prs(owner, name):
        if (existing["state"] == "open" and existing["head_ref"] == head
                and existing["base_ref"] == base):
            view = _pr_view(store, owner, store.get_repo(owner, name), existing)
            return _tool_result(request_id, view, view)
    repo = store.get_repo(owner, name)
    pr = store.create_pr(owner, name, args.get("title") or "",
                         args.get("body") or "", head, head_sha, base, base_sha)
    view = _pr_view(store, owner, repo, pr)
    return _tool_result(request_id, view, view)


def _merge_pull_request(store, request_id, args):
    owner, name = args.get("owner"), args.get("repo")
    if owner != VIEWER_LOGIN or store.get_repo(owner, name) is None:
        return _tool_error(request_id, "Not Found")
    try:
        number = int(args.get("pull_number"))
    except (TypeError, ValueError):
        return _tool_error(request_id, "pull_number must be an integer")
    pr = store.get_pr(owner, name, number)
    if pr is None:
        return _tool_error(request_id, "Pull request not found")
    merged_pr, error = _merge_pr(store, owner, name, pr)
    if error:
        return _tool_error(request_id, error)
    view = _pr_view(store, owner, store.get_repo(owner, name), merged_pr)
    return _tool_result(request_id, view, view)


_TOOL_HANDLERS = {
    "create_repo": _create_repo,
    "get_repo": _get_repo,
    "create_issue": _create_issue,
    "list_issues": _list_issues,
    "create_pull_request": _create_pull_request,
    "merge_pull_request": _merge_pull_request,
}


# --------------------------------------------------------------------------
# JSON-RPC entry point
# --------------------------------------------------------------------------

def handle_mcp(store, raw_body: bytes):
    """Handle one POST /mcp body. Returns (status, payload_dict_or_None).
    A payload of None means an empty body (202 for a notification)."""
    try:
        request = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(request, dict):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}}
    method = request.get("method")
    request_id = request.get("id", None)
    params = request.get("params") or {}
    if "id" not in request:
        # A notification (e.g. notifications/initialized): no reply body.
        return 202, None
    if not isinstance(method, str):
        return 400, {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32600, "message": "Invalid Request"}}
    if method == "server/discover":
        return _result(request_id, {
            "supportedVersions": list(_SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(),
            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()}})
    if method == "initialize":
        offered = params.get("protocolVersion")
        version = offered if offered in _SUPPORTED_VERSIONS else _LEGACY_VERSIONS[0]
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(),
            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()}})
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        # The list is a module constant: two calls are byte-identical.
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return _call_tool(store, request_id, name, arguments)
    return _error(request_id, -32601, "Method not found: %s" % method)
