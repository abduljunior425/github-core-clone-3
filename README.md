# github-core-clone-3

[![make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/c/github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/abduljunior425/github-core-clone-3/rank.svg)](https://make.fun/c/github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/abduljunior425/github-core-clone-3/reward.svg)](https://make.fun/c/github-core-clone-01)

A GitHub-core clone written in **Python 3 standard library only** — no
framework, no third-party packages. It implements the surface the
[github-core-clone-01 bounty](https://make.fun/c/github-core-clone-01)
grades: repositories, issues, pull requests (with three-way squash merge),
the git smart-HTTP protocol (upload-pack v2/v0, receive-pack v0, shallow
fetch and deepen), the REST v3 API, the GraphQL v4 API, and the MCP
JSON-RPC endpoint.

## Architecture

```
main.py            entrypoint: trial mode (MAKEFUN_ENV_URL) or standalone
forge/
  consts.py        viewer identity, fixed epoch, version strings
  pktline.py       git pkt-line codec
  gitobjs.py       SHA-1 objects, trees/commits/tags, pack read/write
                   (plain objects; OFS/REF deltas resolve on read)
  store.py         SQLite (WAL) as the durable log + in-memory dicts as
                   the working set; one RLock; deterministic counters
  gitcore.py       reachability, fetch planning (haves/shallow/deepen),
                   three-way merge, diffs
  gitwire.py       smart-HTTP handlers: info/refs, upload-pack v2/v0,
                   receive-pack v0 (fast-forward checks, sideband report)
  rest.py          REST v3: repos, branches, commits, contents, labels,
                   issues, comments, pull requests, squash merge
  graphql.py       GraphQL v4: hand-written parser + typed executor,
                   introspection, mutations, opaque global ids
  mcp.py           MCP JSON-RPC: server/discover + legacy initialize,
                   tools/list, tools/call (6 tools)
  server.py        ThreadingHTTPServer, routing, auth, JSON framing
```

Design notes:

- **Deterministic.** No wall clock, no unseeded randomness anywhere in
  stored or returned data: server-made commits use a fixed epoch
  (2025-01-01T00:00:00Z) plus a per-process counter, ids are sequential,
  and request ids are counter-based. Replaying the same requests in the
  same order reproduces byte-identical state.
- **Durable.** Every mutation writes to SQLite first, then to memory;
  reads are served from the in-memory working set, so the hot paths
  (fetch planning, issue reads) never touch disk.
- **Fetch planning** never re-sends an object the client holds: the send
  set is the depth-limited want closure minus the held closure, with a
  bounded walk that stops at commits outside the send set.

## Run it

```bash
python3 main.py            # standalone on 127.0.0.1:18080
FORGE_PORT=9000 python3 main.py
FORGE_DATA_DIR=/tmp/forge-data python3 main.py
```

Requires Python 3.12+ (standard library only). In trial mode the entry
binds an ephemeral port, registers it with `$MAKEFUN_ENV_URL/register`,
and exits once `$MAKEFUN_ENV_URL/done` reports `{"done": true}`.

## Run the tests

The kit ships the conformance suite
(`github-core-clone-01-kit/sdk/run.ts`). Start a **fresh** server — the
suite is stateful, so always use a fresh data directory per run:

```bash
# terminal 1: fresh server
FORGE_DATA_DIR=$(mktemp -d) python3 main.py

# terminal 2: from the kit directory
node --experimental-strip-types sdk/run.ts \
  --url http://127.0.0.1:18080 --token test-token

# with the performance benchmark
node --experimental-strip-types sdk/run.ts \
  --url http://127.0.0.1:18080 --token test-token --perf
```

## License

MIT — see [LICENSE](LICENSE).
