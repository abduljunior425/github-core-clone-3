"""Shared constants for the forge.

Everything a reply may carry that looks like an identity, a version or a
timestamp is a fixed constant here: the forge never reads the wall clock and
never generates randomness, so two runs that see the same requests in the same
order store byte-identical state.
"""

# The login the trial's bearer token names (wire.ts buildScenario(): the
# scenario viewer, id 1000). Repositories belong to this login; any other
# owner in a URL is a 404, never a 403.
VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"

# The forge's own version string (GET /api/v3/meta).
FORGE_VERSION = "1.0.0"

# The fixed epoch every server-made commit and timestamp derives from
# (2025-01-01T00:00:00Z). A per-process counter is added so two commits made
# in one run still get distinct ids, while a replay of the same requests
# reproduces the same ids.
FIXED_EPOCH_SEC = 1735689600
FIXED_ISO = "2025-01-01T00:00:00Z"

# The default branch every repository is born with.
DEFAULT_BRANCH = "main"

# The server's identity on the MCP endpoint.
MCP_SERVER_NAME = "github-core-clone-3"
MCP_SERVER_VERSION = "1.0.0"

# The largest pkt-line payload git allows (git's LARGE_PACKET_MAX minus header).
PKT_LINE_MAX = 65520
