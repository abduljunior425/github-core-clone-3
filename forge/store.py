"""The forge's durable store.

SQLite is the write-through log; plain dicts are the working set. Every
read in the request path is served from memory, every mutation writes the
row to SQLite first and then to memory, under one re-entrant lock, so the
in-memory snapshot is always consistent with the database. The database
lives in FORGE_DATA_DIR (or a fresh temp dir); nothing in it depends on the
wall clock — timestamps come from a fixed epoch plus a per-process counter.
"""

import json
import os
import sqlite3
import threading

from .consts import FIXED_EPOCH_SEC, FIXED_ISO

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos(
    id INTEGER PRIMARY KEY, owner TEXT, name TEXT, description TEXT,
    private INTEGER, default_branch TEXT, created_at TEXT,
    UNIQUE(owner, name));
CREATE TABLE IF NOT EXISTS refs(
    owner TEXT, repo TEXT, name TEXT, oid TEXT,
    PRIMARY KEY(owner, repo, name));
CREATE TABLE IF NOT EXISTS objects(
    oid TEXT PRIMARY KEY, type TEXT, data BLOB);
CREATE TABLE IF NOT EXISTS issues(
    owner TEXT, repo TEXT, number INTEGER, title TEXT, body TEXT,
    state TEXT, labels TEXT, created_at TEXT,
    PRIMARY KEY(owner, repo, number));
CREATE TABLE IF NOT EXISTS prs(
    owner TEXT, repo TEXT, number INTEGER, title TEXT, body TEXT,
    state TEXT, merged INTEGER, head_ref TEXT, head_sha TEXT,
    base_ref TEXT, base_sha TEXT, merge_sha TEXT, merged_at TEXT,
    created_at TEXT,
    PRIMARY KEY(owner, repo, number));
CREATE TABLE IF NOT EXISTS comments(
    id INTEGER PRIMARY KEY, owner TEXT, repo TEXT, issue_number INTEGER,
    body TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS labels(
    owner TEXT, repo TEXT, name TEXT, color TEXT,
    PRIMARY KEY(owner, repo, name));
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
"""


class ForgeStore:
    """A SQLite-backed, memory-cached store for one forge instance."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "forge.db")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        # Working set, loaded from the database at startup.
        self.repos = {}      # (owner, name) -> repo dict
        self.refs = {}       # (owner, name) -> {refname: oid}
        self.objects = {}    # oid -> (type, data)
        self.issues = {}     # (owner, name) -> {number: issue dict}
        self.prs = {}        # (owner, name) -> {number: pr dict}
        self.comments = {}   # (owner, name, issue_number) -> [comment dict]
        self.labels = {}     # (owner, name) -> {label name: {"name", "color"}}
        self.counters = {}   # counter name -> int
        self._ts_counter = 0
        self._req_counter = 0
        self._load()

    # ------------------------------------------------------------------
    # startup load
    # ------------------------------------------------------------------

    def _load(self):
        for row in self.db.execute(
                "SELECT id, owner, name, description, private, default_branch, created_at FROM repos"):
            repo = {"id": row[0], "owner": row[1], "name": row[2],
                    "description": row[3] or "", "private": bool(row[4]),
                    "default_branch": row[5], "created_at": row[6]}
            repo["full_name"] = "%s/%s" % (row[1], row[2])
            self.repos[(row[1], row[2])] = repo
        for owner, name, refname, oid in self.db.execute(
                "SELECT owner, repo, name, oid FROM refs"):
            self.refs.setdefault((owner, name), {})[refname] = oid
        for oid, obj_type, data in self.db.execute("SELECT oid, type, data FROM objects"):
            self.objects[oid] = (obj_type, bytes(data))
        for owner, name, number, title, body, state, labels, created_at in self.db.execute(
                "SELECT owner, repo, number, title, body, state, labels, created_at FROM issues"):
            issue = {"number": number, "title": title, "body": body or "",
                     "state": state, "labels": json.loads(labels or "[]"),
                     "created_at": created_at}
            self.issues.setdefault((owner, name), {})[number] = issue
        for row in self.db.execute(
                "SELECT owner, repo, number, title, body, state, merged, head_ref, head_sha,"
                " base_ref, base_sha, merge_sha, merged_at, created_at FROM prs"):
            (owner, name, number, title, body, state, merged, head_ref, head_sha,
             base_ref, base_sha, merge_sha, merged_at, created_at) = row
            pr = {"number": number, "title": title, "body": body or "",
                  "state": state, "merged": bool(merged), "head_ref": head_ref,
                  "head_sha": head_sha, "base_ref": base_ref, "base_sha": base_sha,
                  "merge_commit_sha": merge_sha, "merged_at": merged_at,
                  "created_at": created_at}
            self.prs.setdefault((owner, name), {})[number] = pr
        for cid, owner, name, issue_number, body, created_at in self.db.execute(
                "SELECT id, owner, repo, issue_number, body, created_at FROM comments ORDER BY id"):
            comment = {"id": cid, "body": body, "created_at": created_at}
            self.comments.setdefault((owner, name, issue_number), []).append(comment)
        for owner, name, label_name, color in self.db.execute(
                "SELECT owner, repo, name, color FROM labels"):
            self.labels.setdefault((owner, name), {})[label_name] = {
                "name": label_name, "color": color}
        for name, value in self.db.execute("SELECT name, value FROM counters"):
            self.counters[name] = value

    # ------------------------------------------------------------------
    # counters and timestamps (deterministic)
    # ------------------------------------------------------------------

    def _next_counter(self, name: str) -> int:
        value = self.counters.get(name, 0) + 1
        self.counters[name] = value
        self.db.execute(
            "INSERT INTO counters(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
        return value

    def next_repo_id(self) -> int:
        with self.lock:
            value = self._next_counter("repo_id")
            self.db.commit()
            return value

    def next_number(self, owner: str, name: str) -> int:
        """The next issue/PR number for a repository (one shared counter)."""
        with self.lock:
            value = self._next_counter("number:%s/%s" % (owner, name))
            self.db.commit()
            return value

    def next_comment_id(self) -> int:
        with self.lock:
            value = self._next_counter("comment_id")
            self.db.commit()
            return value

    def next_timestamp(self) -> int:
        """A commit timestamp: the fixed epoch plus a per-process counter."""
        with self.lock:
            ts = FIXED_EPOCH_SEC + self._ts_counter
            self._ts_counter += 1
            return ts

    def next_request_id(self) -> str:
        with self.lock:
            self._req_counter += 1
            return "req-%d" % self._req_counter

    # ------------------------------------------------------------------
    # repositories
    # ------------------------------------------------------------------

    def get_repo(self, owner: str, name: str):
        return self.repos.get((owner, name))

    def list_repos(self, owner: str):
        return [repo for (o, _n), repo in sorted(self.repos.items())
                if o == owner]

    def create_repo(self, owner: str, name: str, description: str = "",
                    private: bool = False, default_branch: str = "main"):
        with self.lock:
            if (owner, name) in self.repos:
                return None
            repo = {"id": self.next_repo_id(), "owner": owner, "name": name,
                    "description": description or "", "private": bool(private),
                    "default_branch": default_branch, "created_at": FIXED_ISO,
                    "full_name": "%s/%s" % (owner, name)}
            self.db.execute(
                "INSERT INTO repos(id, owner, name, description, private, default_branch, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (repo["id"], owner, name, repo["description"], int(private),
                 default_branch, FIXED_ISO))
            self.db.commit()
            self.repos[(owner, name)] = repo
            self.refs.setdefault((owner, name), {})
            self.issues.setdefault((owner, name), {})
            self.prs.setdefault((owner, name), {})
            self.labels.setdefault((owner, name), {})
            return repo

    def update_repo(self, owner: str, name: str, description=None, private=None,
                    default_branch=None):
        with self.lock:
            repo = self.repos.get((owner, name))
            if repo is None:
                return None
            if description is not None:
                repo["description"] = description
            if private is not None:
                repo["private"] = bool(private)
            if default_branch is not None:
                repo["default_branch"] = default_branch
            self.db.execute(
                "UPDATE repos SET description=?, private=?, default_branch=? WHERE owner=? AND name=?",
                (repo["description"], int(repo["private"]), repo["default_branch"],
                 owner, name))
            self.db.commit()
            return repo

    def delete_repo(self, owner: str, name: str) -> bool:
        with self.lock:
            if (owner, name) not in self.repos:
                return False
            for table, col in (("refs", None), ("issues", None), ("prs", None),
                               ("labels", None)):
                self.db.execute(
                    "DELETE FROM %s WHERE owner=? AND repo=?" % table, (owner, name))
            self.db.execute(
                "DELETE FROM comments WHERE owner=? AND repo=?", (owner, name))
            self.db.execute("DELETE FROM repos WHERE owner=? AND name=?", (owner, name))
            self.db.commit()
            self.repos.pop((owner, name), None)
            self.refs.pop((owner, name), None)
            self.issues.pop((owner, name), None)
            self.prs.pop((owner, name), None)
            self.labels.pop((owner, name), None)
            for key in [k for k in self.comments if k[0] == owner and k[1] == name]:
                del self.comments[key]
            return True

    # ------------------------------------------------------------------
    # refs
    # ------------------------------------------------------------------

    def get_ref(self, owner: str, name: str, refname: str):
        return self.refs.get((owner, name), {}).get(refname)

    def list_refs(self, owner: str, name: str):
        return dict(self.refs.get((owner, name), {}))

    def set_ref(self, owner: str, name: str, refname: str, oid: str):
        with self.lock:
            self.db.execute(
                "INSERT INTO refs(owner, repo, name, oid) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(owner, repo, name) DO UPDATE SET oid=excluded.oid",
                (owner, name, refname, oid))
            self.db.commit()
            self.refs.setdefault((owner, name), {})[refname] = oid

    def delete_ref(self, owner: str, name: str, refname: str) -> bool:
        with self.lock:
            refs = self.refs.get((owner, name), {})
            if refname not in refs:
                return False
            self.db.execute("DELETE FROM refs WHERE owner=? AND repo=? AND name=?",
                            (owner, name, refname))
            self.db.commit()
            del refs[refname]
            return True

    # ------------------------------------------------------------------
    # git objects
    # ------------------------------------------------------------------

    def get_object(self, oid: str):
        return self.objects.get(oid)

    def has_object(self, oid: str) -> bool:
        return oid in self.objects

    def put_objects(self, objects):
        """Store {oid: (type, data)}; returns the oids actually added."""
        with self.lock:
            added = []
            rows = []
            for oid, (obj_type, data) in objects.items():
                if oid not in self.objects:
                    added.append(oid)
                    rows.append((oid, obj_type, data))
            if rows:
                self.db.executemany(
                    "INSERT OR IGNORE INTO objects(oid, type, data) VALUES(?, ?, ?)", rows)
                self.db.commit()
            for oid, (obj_type, data) in objects.items():
                self.objects.setdefault(oid, (obj_type, data))
            return added

    # ------------------------------------------------------------------
    # labels
    # ------------------------------------------------------------------

    def add_label(self, owner: str, name: str, label_name: str, color: str):
        with self.lock:
            label = {"name": label_name, "color": color or ""}
            self.db.execute(
                "INSERT INTO labels(owner, repo, name, color) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(owner, repo, name) DO UPDATE SET color=excluded.color",
                (owner, name, label_name, color or ""))
            self.db.commit()
            self.labels.setdefault((owner, name), {})[label_name] = label
            return label

    def get_labels(self, owner: str, name: str):
        return list(self.labels.get((owner, name), {}).values())

    # ------------------------------------------------------------------
    # issues
    # ------------------------------------------------------------------

    def create_issue(self, owner: str, name: str, title: str, body: str = "",
                     label_names=None):
        with self.lock:
            number = self.next_number(owner, name)
            issue = {"number": number, "title": title, "body": body or "",
                     "state": "open", "labels": list(label_names or []),
                     "created_at": FIXED_ISO}
            self.db.execute(
                "INSERT INTO issues(owner, repo, number, title, body, state, labels, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, name, number, title, body or "", "open",
                 json.dumps(issue["labels"]), FIXED_ISO))
            self.db.commit()
            self.issues.setdefault((owner, name), {})[number] = issue
            return issue

    def get_issue(self, owner: str, name: str, number: int):
        return self.issues.get((owner, name), {}).get(number)

    def list_issues(self, owner: str, name: str):
        return [self.issues[(owner, name)][n]
                for n in sorted(self.issues.get((owner, name), {}))]

    def update_issue(self, owner: str, name: str, number: int, title=None,
                     body=None, state=None, label_names=None):
        with self.lock:
            issue = self.get_issue(owner, name, number)
            if issue is None:
                return None
            if title is not None:
                issue["title"] = title
            if body is not None:
                issue["body"] = body
            if state is not None:
                issue["state"] = state
            if label_names is not None:
                issue["labels"] = list(label_names)
            self.db.execute(
                "UPDATE issues SET title=?, body=?, state=?, labels=? "
                "WHERE owner=? AND repo=? AND number=?",
                (issue["title"], issue["body"], issue["state"],
                 json.dumps(issue["labels"]), owner, name, number))
            self.db.commit()
            return issue

    def add_comment(self, owner: str, name: str, number: int, body: str):
        with self.lock:
            cid = self.next_comment_id()
            comment = {"id": cid, "body": body, "created_at": FIXED_ISO}
            self.db.execute(
                "INSERT INTO comments(id, owner, repo, issue_number, body, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (cid, owner, name, number, body, FIXED_ISO))
            self.db.commit()
            self.comments.setdefault((owner, name, number), []).append(comment)
            return comment

    def list_comments(self, owner: str, name: str, number: int):
        return list(self.comments.get((owner, name, number), []))

    # ------------------------------------------------------------------
    # pull requests
    # ------------------------------------------------------------------

    def create_pr(self, owner: str, name: str, title: str, body: str,
                  head_ref: str, head_sha: str, base_ref: str, base_sha: str):
        with self.lock:
            number = self.next_number(owner, name)
            pr = {"number": number, "title": title, "body": body or "",
                  "state": "open", "merged": False, "head_ref": head_ref,
                  "head_sha": head_sha, "base_ref": base_ref,
                  "base_sha": base_sha, "merge_commit_sha": None,
                  "merged_at": None, "created_at": FIXED_ISO}
            self.db.execute(
                "INSERT INTO prs(owner, repo, number, title, body, state, merged,"
                " head_ref, head_sha, base_ref, base_sha, merge_sha, merged_at, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, name, number, title, body or "", "open", 0, head_ref,
                 head_sha, base_ref, base_sha, None, None, FIXED_ISO))
            self.db.commit()
            self.prs.setdefault((owner, name), {})[number] = pr
            return pr

    def get_pr(self, owner: str, name: str, number: int):
        return self.prs.get((owner, name), {}).get(number)

    def list_prs(self, owner: str, name: str):
        return [self.prs[(owner, name)][n]
                for n in sorted(self.prs.get((owner, name), {}))]

    def update_pr(self, owner: str, name: str, number: int, **fields):
        with self.lock:
            pr = self.get_pr(owner, name, number)
            if pr is None:
                return None
            for key, value in fields.items():
                if key in pr:
                    pr[key] = value
            self.db.execute(
                "UPDATE prs SET title=?, body=?, state=?, merged=?, head_sha=?,"
                " base_sha=?, merge_sha=?, merged_at=? WHERE owner=? AND repo=? AND number=?",
                (pr["title"], pr["body"], pr["state"], int(pr["merged"]),
                 pr["head_sha"], pr["base_sha"], pr["merge_commit_sha"],
                 pr["merged_at"], owner, name, number))
            self.db.commit()
            return pr

    def close(self):
        self.db.close()
