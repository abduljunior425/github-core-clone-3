"""GraphQL v4 (the /api/graphql surface).

A small, dependency-free GraphQL implementation: a tokenizer and recursive
descent parser for the query language, a typed executor over the schema
below, and resolvers that read and write the same store as REST. Global
ids are opaque base64url strings (never the plain database id or number);
cursors are opaque base64url strings over the item number. Unknown fields
answer HTTP 200 with an `errors` array naming the field, exactly as the
suite expects; a missing repository answers `data.repository = null` with
no errors.
"""

import base64
import re

from . import gitcore
from .consts import FIXED_ISO, VIEWER_ID, VIEWER_LOGIN, VIEWER_NAME
from .store import ForgeStore

# --------------------------------------------------------------------------
# schema (drives introspection and field validation)
# --------------------------------------------------------------------------

_TYPES = {
    "Query": {"kind": "OBJECT", "fields": {
        "viewer": "User", "repository": "Repository", "node": "Node"}},
    "Mutation": {"kind": "OBJECT", "fields": {
        "createIssue": "CreateIssuePayload",
        "closeIssue": "CloseIssuePayload",
        "createPullRequest": "CreatePullRequestPayload",
        "mergePullRequest": "MergePullRequestPayload",
        "addComment": "AddCommentPayload"}},
    "User": {"kind": "OBJECT", "fields": {
        "login": "String", "id": "ID", "name": "String", "email": "String"}},
    "Repository": {"kind": "OBJECT", "fields": {
        "id": "ID", "databaseId": "Int", "name": "String",
        "nameWithOwner": "String", "owner": "User",
        "defaultBranchRef": "Ref", "viewerPermission": "String",
        "hasIssuesEnabled": "Boolean", "issue": "Issue",
        "issues": "IssueConnection", "pullRequests": "PullRequestConnection",
        "pullRequest": "PullRequest"}},
    "Issue": {"kind": "OBJECT", "fields": {
        "id": "ID", "number": "Int", "title": "String", "state": "String",
        "body": "String", "author": "User", "url": "String"}},
    "PullRequest": {"kind": "OBJECT", "fields": {
        "id": "ID", "number": "Int", "title": "String", "state": "String",
        "merged": "Boolean", "mergeable": "String",
        "headRefName": "String", "baseRefName": "String",
        "files": "PullRequestFileConnection", "body": "String"}},
    "IssueConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[Issue]", "edges": "[IssueEdge]",
        "pageInfo": "PageInfo", "totalCount": "Int"}},
    "IssueEdge": {"kind": "OBJECT", "fields": {
        "node": "Issue", "cursor": "String"}},
    "PullRequestConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[PullRequest]", "edges": "[PullRequestEdge]",
        "pageInfo": "PageInfo", "totalCount": "Int"}},
    "PullRequestEdge": {"kind": "OBJECT", "fields": {
        "node": "PullRequest", "cursor": "String"}},
    "PullRequestFileConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[PullRequestFile]", "pageInfo": "PageInfo"}},
    "PullRequestFile": {"kind": "OBJECT", "fields": {"path": "String"}},
    "PageInfo": {"kind": "OBJECT", "fields": {
        "hasNextPage": "Boolean", "hasPreviousPage": "Boolean",
        "endCursor": "String", "startCursor": "String"}},
    "Ref": {"kind": "OBJECT", "fields": {
        "name": "String", "target": "String"}},
    "Node": {"kind": "INTERFACE", "fields": {"id": "ID"}},
    "SearchType": {"kind": "ENUM", "values": [
        "ISSUE", "PULL_REQUEST", "REPOSITORY", "USER", "DISCUSSION",
        "COMMIT"]},
    "CreateIssuePayload": {"kind": "OBJECT", "fields": {"issue": "Issue"}},
    "CloseIssuePayload": {"kind": "OBJECT", "fields": {"issue": "Issue"}},
    "CreatePullRequestPayload": {"kind": "OBJECT",
                                 "fields": {"pullRequest": "PullRequest"}},
    "MergePullRequestPayload": {"kind": "OBJECT",
                                "fields": {"pullRequest": "PullRequest"}},
    "AddCommentPayload": {"kind": "OBJECT",
                          "fields": {"commentEdge": "IssueCommentEdge"}},
    "IssueCommentEdge": {"kind": "OBJECT",
                         "fields": {"node": "IssueComment"}},
    "IssueComment": {"kind": "OBJECT", "fields": {
        "id": "ID", "body": "String"}},
    "String": {"kind": "SCALAR"},
    "Int": {"kind": "SCALAR"},
    "Boolean": {"kind": "SCALAR"},
    "ID": {"kind": "SCALAR"},
}

_SCALARS = {"String", "Int", "Boolean", "ID"}


def _gid(kind: str, *parts) -> str:
    raw = ":".join([kind] + [str(p) for p in parts])
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _ungid(gid):
    if not isinstance(gid, str):
        return None
    try:
        padded = gid + "=" * (-len(gid) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    return raw.split(":")


def _cursor(number: int) -> str:
    return base64.urlsafe_b64encode(("c:%d" % number).encode()).decode("ascii")


def _uncursor(cursor):
    parts = _ungid(cursor)
    if not parts or len(parts) != 2 or parts[0] != "c":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# tokenizer and parser
# --------------------------------------------------------------------------

class GQLSyntaxError(ValueError):
    pass


_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\r\n,]+)
  | (?P<comment>\#[^\n]*)
  | (?P<ellipsis>\.\.\.)
  | (?P<punct>[!$()\[\]{}\=:|&@])
  | (?P<float>-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?)
  | (?P<int>-?(?:0|[1-9][0-9]*))
  | (?P<string>"(?:[^"\\\n]|\\.)*")
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
""", re.VERBOSE)


def _tokenize(source: str):
    tokens = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if not match:
            raise GQLSyntaxError("unexpected character %r at %d"
                                 % (source[pos], pos))
        pos = match.end()
        kind = match.lastgroup
        if kind in ("ws", "comment"):
            continue
        tokens.append((kind, match.group()))
    tokens.append(("eof", ""))
    return tokens


class _Parser:
    def __init__(self, source: str):
        self.tokens = _tokenize(source)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, text: str):
        kind, value = self.next()
        if value != text:
            raise GQLSyntaxError("expected %r, got %r" % (text, value))

    def expect_name(self):
        kind, value = self.next()
        if kind != "name":
            raise GQLSyntaxError("expected a name, got %r" % value)
        return value

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            if self.peek() == ("name", "fragment"):
                frag = self.parse_fragment()
                fragments[frag["name"]] = frag
            else:
                operations.append(self.parse_operation())
        return {"operations": operations, "fragments": fragments}

    def parse_operation(self):
        kind, value = self.peek()
        if value == "{":
            return {"type": "query", "name": None, "variables": {},
                    "selection": self.parse_selection_set()}
        op_type = self.expect_name()
        if op_type not in ("query", "mutation", "subscription"):
            raise GQLSyntaxError("unknown operation %r" % op_type)
        name = None
        if self.peek()[0] == "name":
            name = self.next()[1]
        variables = {}
        if self.peek()[1] == "(":
            variables = self.parse_variable_definitions()
        self._skip_directives()
        return {"type": op_type, "name": name, "variables": variables,
                "selection": self.parse_selection_set()}

    def parse_variable_definitions(self):
        self.expect("(")
        variables = {}
        while self.peek()[1] != ")":
            self.expect("$")
            name = self.expect_name()
            self.expect(":")
            var_type = self._parse_type()
            default = None
            if self.peek()[1] == "=":
                self.next()
                default = self.parse_value()
            variables[name] = {"type": var_type, "default": default}
        self.expect(")")
        return variables

    def _parse_type(self):
        if self.peek()[1] == "[":
            self.next()
            inner = self._parse_type()
            self.expect("]")
            result = "[%s]" % inner
        else:
            result = self.expect_name()
        if self.peek()[1] == "!":
            self.next()
            result += "!"
        return result

    def _skip_directives(self):
        while self.peek()[1] == "@":
            self.next()
            self.expect_name()
            if self.peek()[1] == "(":
                self.parse_arguments()

    def parse_fragment(self):
        self.expect_name()  # fragment
        name = self.expect_name()
        on = self.expect_name()
        if on != "on":
            raise GQLSyntaxError("expected 'on' in fragment")
        type_name = self.expect_name()
        self._skip_directives()
        return {"name": name, "type": type_name,
                "selection": self.parse_selection_set()}

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek()[1] != "}":
            selections.append(self.parse_selection())
        self.expect("}")
        return selections

    def parse_selection(self):
        if self.peek()[1] == "...":
            self.next()
            if self.peek()[0] == "name" and self.peek()[1] == "on":
                self.next()
                type_name = self.expect_name()
                self._skip_directives()
                return {"kind": "inline", "type": type_name,
                        "selection": self.parse_selection_set()}
            if self.peek()[0] == "name":
                name = self.next()[1]
                self._skip_directives()
                return {"kind": "spread", "name": name}
            self._skip_directives()
            return {"kind": "inline", "type": None,
                    "selection": self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.peek()[1] == ":":
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek()[1] == "(":
            args = self.parse_arguments()
        self._skip_directives()
        selection = None
        if self.peek()[1] == "{":
            selection = self.parse_selection_set()
        return {"kind": "field", "alias": alias or name, "name": name,
                "args": args, "selection": selection}

    def parse_arguments(self):
        self.expect("(")
        args = {}
        while self.peek()[1] != ")":
            name = self.expect_name()
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == "punct" and value == "$":
            return {"kind": "variable", "name": self.expect_name()}
        if kind == "int":
            return {"kind": "int", "value": int(value)}
        if kind == "float":
            return {"kind": "float", "value": float(value)}
        if kind == "string":
            return {"kind": "string", "value": _unescape(value[1:-1])}
        if kind == "name":
            if value == "true":
                return {"kind": "boolean", "value": True}
            if value == "false":
                return {"kind": "boolean", "value": False}
            if value == "null":
                return {"kind": "null"}
            return {"kind": "enum", "value": value}
        if kind == "punct" and value == "[":
            items = []
            while self.peek()[1] != "]":
                items.append(self.parse_value())
            self.expect("]")
            return {"kind": "list", "items": items}
        if kind == "punct" and value == "{":
            fields = {}
            while self.peek()[1] != "}":
                name = self.expect_name()
                self.expect(":")
                fields[name] = self.parse_value()
            self.expect("}")
            return {"kind": "object", "fields": fields}
        raise GQLSyntaxError("unexpected value token %r" % value)


def _unescape(text: str) -> str:
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"',
                       "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
            if nxt == "u" and index + 5 < len(text):
                out.append(chr(int(text[index + 2:index + 6], 16)))
                index += 6
                continue
            out.append(mapping.get(nxt, nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _resolve_value(node, variables):
    kind = node["kind"]
    if kind == "variable":
        if node["name"] in variables:
            return variables[node["name"]]
        return None
    if kind in ("int", "float", "string", "boolean"):
        return node["value"]
    if kind == "null":
        return None
    if kind == "enum":
        return node["value"]
    if kind == "list":
        return [_resolve_value(item, variables) for item in node["items"]]
    if kind == "object":
        return {name: _resolve_value(value, variables)
                for name, value in node["fields"].items()}
    return None


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

class _Executor:
    def __init__(self, store: ForgeStore, document, variables):
        self.store = store
        self.document = document
        self.variables = variables or {}
        self.errors = []

    def execute(self, operation):
        variables = dict(self.variables)
        for name, definition in operation["variables"].items():
            if name not in variables and definition["default"] is not None:
                variables[name] = _resolve_value(definition["default"], variables)
        self.variables = variables
        root_type = "Query" if operation["type"] == "query" else "Mutation"
        data = self._execute_selection(
            operation["selection"], root_type, {})
        result = {"data": data}
        if self.errors:
            result["errors"] = self.errors
        return result

    def _execute_selection(self, selections, parent_type, parent_value):
        result = {}
        for selection in selections:
            if selection["kind"] == "spread":
                fragment = self.document["fragments"].get(selection["name"])
                if fragment is None:
                    self.errors.append(
                        {"message": "Unknown fragment %r" % selection["name"]})
                    continue
                runtime = self._runtime_type(parent_type, parent_value)
                if runtime is not None and runtime != fragment["type"]:
                    continue
                result.update(self._execute_selection(
                    fragment["selection"], parent_type, parent_value))
                continue
            if selection["kind"] == "inline":
                runtime = self._runtime_type(parent_type, parent_value)
                if selection["type"] is not None and runtime is not None \
                        and runtime != selection["type"]:
                    continue
                result.update(self._execute_selection(
                    selection["selection"], parent_type, parent_value))
                continue
            # field
            name = selection["name"]
            alias = selection["alias"]
            if name == "__typename":
                result[alias] = self._runtime_type(parent_type, parent_value)
                continue
            if name == "__type":
                args = self._resolve_args(selection["args"])
                result[alias] = self._execute_value(
                    "__Type", self._introspect_type(args.get("name")),
                    selection["selection"])
                continue
            if name == "__schema":
                result[alias] = self._execute_value(
                    "__Schema", {"queryType": {"name": "Query"}},
                    selection["selection"])
                continue
            field_type = _TYPES.get(parent_type, {}).get("fields", {}).get(name)
            if field_type is None:
                self.errors.append(
                    {"message": "Cannot query field '%s' on type '%s'."
                                % (name, parent_type)})
                result[alias] = None
                continue
            args = self._resolve_args(selection["args"])
            value = _resolve_field(self.store, parent_type, parent_value,
                                   name, args, self)
            if value is None:
                result[alias] = None
                continue
            result[alias] = self._execute_value(
                field_type, value, selection["selection"])
        return result

    def _runtime_type(self, declared_type, value):
        if isinstance(value, dict) and "_type" in value:
            return value["_type"]
        return declared_type

    def _resolve_args(self, args):
        return {name: _resolve_value(node, self.variables)
                for name, node in args.items()}

    def _execute_value(self, field_type, value, selection):
        if field_type.startswith("[") and field_type.endswith("]"):
            item_type = field_type[1:-1]
            return [self._execute_value(item_type, item, selection)
                    for item in (value or [])]
        if field_type in _SCALARS:
            return value
        if field_type == "__Type":
            return self._introspect_value(value, selection)
        if field_type == "__Schema":
            return self._introspect_schema(value, selection)
        if selection is None:
            return value
        if value is None:
            return None
        runtime = self._runtime_type(field_type, value)
        return self._execute_selection(selection, runtime, value)

    # -- introspection ------------------------------------------------------
    def _introspect_type(self, name):
        info = _TYPES.get(name)
        if info is None:
            return None
        return {"name": name, "kind": info["kind"],
                "fields": [{"name": f} for f in info.get("fields", {})],
                "enumValues": [{"name": v} for v in info.get("values", [])]}

    def _introspect_value(self, value, selection):
        if value is None or selection is None:
            return value
        out = {}
        for selection_item in selection:
            if selection_item["kind"] != "field":
                continue
            name = selection_item["name"]
            alias = selection_item["alias"]
            if name == "fields":
                fields = value.get("fields") or []
                out[alias] = [self._introspect_value(f, selection_item["selection"])
                              for f in fields]
            elif name == "enumValues":
                values = value.get("enumValues") or []
                out[alias] = [self._introspect_value(v, selection_item["selection"])
                              for v in values]
            elif name in value:
                out[alias] = value[name]
            else:
                out[alias] = None
        return out

    def _introspect_schema(self, value, selection):
        if selection is None:
            return value
        out = {}
        for selection_item in selection:
            if selection_item["kind"] != "field":
                continue
            name = selection_item["name"]
            if name == "queryType":
                out[selection_item["alias"]] = {"name": "Query"}
            elif name == "types":
                out[selection_item["alias"]] = [
                    self._introspect_value(self._introspect_type(t), None)
                    for t in sorted(_TYPES)]
            else:
                out[selection_item["alias"]] = None
        return out


# --------------------------------------------------------------------------
# field resolvers
# --------------------------------------------------------------------------

def _repo_dict(store, owner: str, name: str):
    repo = store.get_repo(owner, name)
    if repo is None:
        return None
    return {"_type": "Repository", "_owner": owner, "_name": name,
            "_repo": repo, "id": _gid("R", owner, name),
            "databaseId": repo["id"], "name": name,
            "nameWithOwner": repo["full_name"],
            "owner": {"_type": "User", "login": owner, "id": str(VIEWER_ID),
                      "name": VIEWER_NAME, "email": ""},
            "defaultBranchRef": {"_type": "Ref",
                                 "name": repo["default_branch"]},
            "viewerPermission": "ADMIN", "hasIssuesEnabled": True}


def _issue_dict(store, owner: str, name: str, issue):
    return {"_type": "Issue", "_owner": owner, "_name": name,
            "_number": issue["number"],
            "id": _gid("I", owner, name, issue["number"]),
            "number": issue["number"], "title": issue["title"],
            "state": issue["state"].upper(), "body": issue["body"],
            "author": {"_type": "User", "login": VIEWER_LOGIN,
                       "id": str(VIEWER_ID)},
            "url": "https://example.invalid/%s/%s/issues/%d"
                   % (owner, name, issue["number"])}


def _pr_dict(store, owner: str, name: str, pr):
    state = "OPEN" if pr["state"] == "open" else (
        "MERGED" if pr["merged"] else "CLOSED")
    return {"_type": "PullRequest", "_owner": owner, "_name": name,
            "_number": pr["number"], "_pr": pr,
            "id": _gid("P", owner, name, pr["number"]),
            "number": pr["number"], "title": pr["title"],
            "state": state, "merged": pr["merged"],
            "mergeable": _mergeable(store, owner, name, pr),
            "headRefName": pr["head_ref"], "baseRefName": pr["base_ref"],
            "body": pr["body"]}


def _mergeable(store, owner: str, name: str, pr) -> str:
    if pr["state"] != "open":
        return "UNKNOWN"
    base_tip = store.get_ref(owner, name, "refs/heads/" + pr["base_ref"]) \
        or pr["base_sha"]
    head_tip = store.get_ref(owner, name, "refs/heads/" + pr["head_ref"]) \
        or pr["head_sha"]
    merge_base = gitcore.find_merge_base(store.get_object, base_tip, head_tip)
    if merge_base is None:
        return "UNKNOWN"
    base_tree = gitcore.commit_tree(store.get_object, base_tip) or ""
    head_tree = gitcore.commit_tree(store.get_object, head_tip) or ""
    base_tree_at = gitcore.commit_tree(store.get_object, merge_base) or ""
    _merged, conflicts = gitcore.merge_trees(
        store.get_object, base_tree_at, base_tree, head_tree)
    return "CONFLICTING" if conflicts else "MERGEABLE"


def _issue_connection(store, owner: str, name: str, args):
    issues = store.list_issues(owner, name)
    states = args.get("states")
    if states:
        wanted = {s.upper() for s in states}
        issues = [i for i in issues if i["state"].upper() in wanted]
    order_by = args.get("orderBy") or {}
    direction = (order_by.get("direction") or "ASC").upper()
    issues = sorted(issues, key=lambda i: i["number"],
                    reverse=(direction == "DESC"))
    after_number = _uncursor(args.get("after")) if args.get("after") else None
    if after_number is not None:
        if direction == "DESC":
            issues = [i for i in issues if i["number"] < after_number]
        else:
            issues = [i for i in issues if i["number"] > after_number]
    total = len(issues)
    first = args.get("first")
    page = issues[:first] if isinstance(first, int) else issues
    has_next = len(issues) > len(page)
    nodes = [_issue_dict(store, owner, name, i) for i in page]
    page_info = {"_type": "PageInfo", "hasNextPage": has_next,
                 "hasPreviousPage": after_number is not None,
                 "endCursor": _cursor(page[-1]["number"]) if page else None,
                 "startCursor": _cursor(page[0]["number"]) if page else None}
    return {"_type": "IssueConnection", "nodes": nodes,
            "edges": [{"_type": "IssueEdge", "node": n,
                       "cursor": _cursor(n["number"])} for n in nodes],
            "pageInfo": page_info, "totalCount": total}


def _pr_connection(store, owner: str, name: str, args):
    prs = store.list_prs(owner, name)
    after_number = _uncursor(args.get("after")) if args.get("after") else None
    if after_number is not None:
        prs = [p for p in prs if p["number"] > after_number]
    total = len(prs)
    first = args.get("first")
    page = prs[:first] if isinstance(first, int) else prs
    has_next = len(prs) > len(page)
    nodes = [_pr_dict(store, owner, name, p) for p in page]
    page_info = {"_type": "PageInfo", "hasNextPage": has_next,
                 "hasPreviousPage": after_number is not None,
                 "endCursor": _cursor(page[-1]["number"]) if page else None,
                 "startCursor": _cursor(page[0]["number"]) if page else None}
    return {"_type": "PullRequestConnection", "nodes": nodes,
            "edges": [{"_type": "PullRequestEdge", "node": n,
                       "cursor": _cursor(n["number"])} for n in nodes],
            "pageInfo": page_info, "totalCount": total}


def _pr_files_connection(store, owner: str, name: str, pr, args):
    from .rest import _pr_files
    files = _pr_files(store, owner, name, pr)
    first = args.get("first")
    page = files[:first] if isinstance(first, int) else files
    nodes = [{"_type": "PullRequestFile", "path": f["filename"]}
             for f in page]
    page_info = {"_type": "PageInfo", "hasNextPage": len(files) > len(page),
                 "hasPreviousPage": False, "endCursor": None,
                 "startCursor": None}
    return {"_type": "PullRequestFileConnection", "nodes": nodes,
            "pageInfo": page_info}


def _resolve_field(store, parent_type, parent, name, args, executor):
    # -- Query --------------------------------------------------------------
    if parent_type == "Query":
        if name == "viewer":
            return {"_type": "User", "login": VIEWER_LOGIN,
                    "id": str(VIEWER_ID), "name": VIEWER_NAME,
                    "email": ""}
        if name == "repository":
            owner, repo_name = args.get("owner"), args.get("name")
            if owner != VIEWER_LOGIN:
                return None
            return _repo_dict(store, owner, repo_name)
        if name == "node":
            return _resolve_node(store, args.get("id"))
        return None
    # -- Mutation -----------------------------------------------------------
    if parent_type == "Mutation":
        return _resolve_mutation(store, name, args, executor)
    # -- Repository ---------------------------------------------------------
    if parent_type == "Repository":
        owner, repo_name = parent["_owner"], parent["_name"]
        if name == "issue":
            issue = store.get_issue(owner, repo_name, args.get("number"))
            return _issue_dict(store, owner, repo_name, issue) if issue else None
        if name == "issues":
            return _issue_connection(store, owner, repo_name, args)
        if name == "pullRequest":
            pr = store.get_pr(owner, repo_name, args.get("number"))
            return _pr_dict(store, owner, repo_name, pr) if pr else None
        if name == "pullRequests":
            return _pr_connection(store, owner, repo_name, args)
        return parent.get(name)
    # -- PullRequest --------------------------------------------------------
    if parent_type == "PullRequest":
        if name == "files":
            return _pr_files_connection(store, parent["_owner"],
                                        parent["_name"], parent["_pr"], args)
        return parent.get(name)
    # -- everything else is a plain dict lookup ----------------------------
    if isinstance(parent, dict):
        return parent.get(name)
    return None


def _resolve_node(store, gid):
    parts = _ungid(gid)
    if not parts:
        return None
    kind = parts[0]
    if kind == "R" and len(parts) == 3:
        return _repo_dict(store, parts[1], parts[2])
    if kind == "I" and len(parts) == 4:
        issue = store.get_issue(parts[1], parts[2], int(parts[3]))
        return _issue_dict(store, parts[1], parts[2], issue) if issue else None
    if kind == "P" and len(parts) == 4:
        pr = store.get_pr(parts[1], parts[2], int(parts[3]))
        return _pr_dict(store, parts[1], parts[2], pr) if pr else None
    if kind == "U" and len(parts) == 2:
        return {"_type": "User", "login": parts[1], "id": str(VIEWER_ID)}
    return None


def _resolve_mutation(store, name, args, executor):
    payload = args.get("input") or {}
    if name == "createIssue":
        parts = _ungid(payload.get("repositoryId"))
        if not parts or parts[0] != "R":
            executor.errors.append({"message": "repositoryId is not a repository"})
            return {"issue": None}
        owner, repo_name = parts[1], parts[2]
        if store.get_repo(owner, repo_name) is None:
            executor.errors.append({"message": "repository not found"})
            return {"issue": None}
        issue = store.create_issue(owner, repo_name, payload.get("title") or "",
                                   payload.get("body") or "")
        return {"issue": _issue_dict(store, owner, repo_name, issue)}
    if name == "closeIssue":
        parts = _ungid(payload.get("issueId"))
        if not parts or parts[0] != "I":
            executor.errors.append({"message": "issueId is not an issue"})
            return {"issue": None}
        owner, repo_name, number = parts[1], parts[2], int(parts[3])
        issue = store.update_issue(owner, repo_name, number, state="closed")
        if issue is None:
            executor.errors.append({"message": "issue not found"})
            return {"issue": None}
        return {"issue": _issue_dict(store, owner, repo_name, issue)}
    if name == "createPullRequest":
        parts = _ungid(payload.get("repositoryId"))
        if not parts or parts[0] != "R":
            executor.errors.append({"message": "repositoryId is not a repository"})
            return {"pullRequest": None}
        owner, repo_name = parts[1], parts[2]
        head = payload.get("headRefName") or ""
        base = payload.get("baseRefName") or ""
        head_sha = store.get_ref(owner, repo_name, "refs/heads/" + head)
        base_sha = store.get_ref(owner, repo_name, "refs/heads/" + base)
        if head_sha is None or base_sha is None:
            executor.errors.append({"message": "head or base branch not found"})
            return {"pullRequest": None}
        for existing in store.list_prs(owner, repo_name):
            if (existing["state"] == "open" and existing["head_ref"] == head
                    and existing["base_ref"] == base):
                return {"pullRequest": _pr_dict(store, owner, repo_name, existing)}
        pr = store.create_pr(owner, repo_name, payload.get("title") or "",
                             payload.get("body") or "", head, head_sha,
                             base, base_sha)
        return {"pullRequest": _pr_dict(store, owner, repo_name, pr)}
    if name == "mergePullRequest":
        parts = _ungid(payload.get("pullRequestId"))
        if not parts or parts[0] != "P":
            executor.errors.append({"message": "pullRequestId is not a pull request"})
            return {"pullRequest": None}
        owner, repo_name, number = parts[1], parts[2], int(parts[3])
        pr = store.get_pr(owner, repo_name, number)
        if pr is None:
            executor.errors.append({"message": "pull request not found"})
            return {"pullRequest": None}
        merged_pr, error = _merge_pr(store, owner, repo_name, pr)
        if error:
            executor.errors.append({"message": error})
            return {"pullRequest": None}
        return {"pullRequest": _pr_dict(store, owner, repo_name, merged_pr)}
    if name == "addComment":
        parts = _ungid(payload.get("subjectId"))
        if not parts or parts[0] not in ("I", "P"):
            executor.errors.append({"message": "subjectId is not an issue"})
            return {"commentEdge": None}
        owner, repo_name, number = parts[1], parts[2], int(parts[3])
        comment = store.add_comment(owner, repo_name, number,
                                    payload.get("body") or "")
        node = {"_type": "IssueComment",
                "id": _gid("C", owner, repo_name, number, comment["id"]),
                "body": comment["body"]}
        return {"commentEdge": {"_type": "IssueCommentEdge", "node": node}}
    executor.errors.append({"message": "Unknown mutation '%s'" % name})
    return None


def _merge_pr(store, owner: str, name: str, pr):
    """Squash-merge a pull request; returns (updated_pr, error_message)."""
    from .rest import _write_tree_commit
    if pr["state"] != "open":
        return None, "Pull request is already closed"
    with store.lock:
        base_tip = store.get_ref(owner, name, "refs/heads/" + pr["base_ref"]) \
            or pr["base_sha"]
        head_tip = store.get_ref(owner, name, "refs/heads/" + pr["head_ref"]) \
            or pr["head_sha"]
        merge_base = gitcore.find_merge_base(store.get_object, base_tip, head_tip)
        if merge_base is None:
            return None, "No common ancestor for this pull request"
        base_tree = gitcore.commit_tree(store.get_object, base_tip) or ""
        head_tree = gitcore.commit_tree(store.get_object, head_tip) or ""
        merge_base_tree = gitcore.commit_tree(store.get_object, merge_base) or ""
        merged, conflicts = gitcore.merge_trees(
            store.get_object, merge_base_tree, base_tree, head_tree)
        if conflicts:
            return None, "Merge conflict"
        merge_sha = _write_tree_commit(
            store, owner, name, merged, base_tip,
            pr["title"] or ("Merge pull request #%d" % pr["number"]))
        store.set_ref(owner, name, "refs/heads/" + pr["base_ref"], merge_sha)
        updated = store.update_pr(owner, name, pr["number"], state="closed",
                                  merged=True, merge_commit_sha=merge_sha,
                                  merged_at=FIXED_ISO, base_sha=merge_sha,
                                  head_sha=head_tip)
    return updated, None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def handle_graphql(store, body, operation_name=None):
    """Execute one operation document (dict) or a batch (list of dicts).
    Returns (status, payload)."""
    if isinstance(body, list):
        return 200, [handle_graphql(store, item)[1] for item in body]
    if not isinstance(body, dict) or "query" not in body:
        return 400, {"errors": [{"message": "query is required"}]}
    query = body.get("query") or ""
    variables = body.get("variables") or {}
    if isinstance(variables, str):
        import json
        try:
            variables = json.loads(variables)
        except ValueError:
            variables = {}
    try:
        document = _Parser(query).parse_document()
    except GQLSyntaxError as err:
        return 200, {"errors": [{"message": str(err)}]}
    if not document["operations"]:
        return 200, {"errors": [{"message": "no operation in document"}]}
    operation = document["operations"][0]
    name = operation_name or body.get("operationName")
    if name:
        for candidate in document["operations"]:
            if candidate["name"] == name:
                operation = candidate
                break
    executor = _Executor(store, document, variables)
    return 200, executor.execute(operation)
