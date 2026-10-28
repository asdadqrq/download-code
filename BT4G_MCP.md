# BT4G MCP

The current workflow uses `bt4g_workflow.py` (also available through
`bt4g_115_batch.py`). Run `collect`, have Codex perform the whole-batch language
model review, run `apply-review`, then `transfer`. The installed `$bt4g-to-115`
skill documents the review contract and commands. This replaces the historical
automatic code matcher for new batches. No previous reports are migrated or
resubmitted automatically.

Uploads are round-based batches: resolve one approved magnet for every eligible
code, submit the full list once, then submit replacement batches only for failed
codes. A 100-code batch with distinct approved hashes sends 100 links in one
115 MCP call. The local helper's submission deadline scales up to five minutes;
the per-round completion check uses the workflow's 10-second rule.

The 115 helper is a separate `netdisk-mcp-server` checkout. To reproduce its
batch timeout change, apply `patches/netdisk-offline-batch-timeout.patch` inside
that checkout and rebuild it:

```sh
git -C netdisk-mcp-server apply ../patches/netdisk-offline-batch-timeout.patch
npm --prefix netdisk-mcp-server run build
```

New MCP tools:

- `bt4g_search_candidates`: save up to three ranked title-page links, with no
  magnet extraction or title identity classification. Prefer Thz, then >2 GB,
  then shortest title; >900 MB is mandatory.
- `bt4g_resolve_candidate`: after review, read the page's magnet/hash and video
  file size evidence. The transfer runner refuses videos totaling <=900 MB.

Restart/reload the BT4G MCP connection after updating the server source.
Legacy tools below remain available for compatibility and are not used by the
new model-reviewed workflow.

Local stdio MCP server: `bt4g_mcp_server.py`.

## Tools

- `bt4g_search_exact(codes, max_pages=3, max_results=1, include_titles=false, exclude_hashes=[])`:
  returns verified magnet URIs only for titles containing the requested code.
  Case, spaces and underscores are accepted; different prefixes, different
  numbers and longer identifiers are rejected. No implicit aliases.
  Use `exclude_hashes` to skip previously failed magnets and find another exact
  match. Up to 100 pages can be requested, subject to the per-code time limit.
- `bt4g_extract_magnets(urls)`: converts BT4G detail links into magnet URIs.

Calls navigate an existing Chrome tab. Keep a `https://bt4gprx.com/`
tab open first. The selected BT4G tab is preferred; otherwise the server pins
another open BT4G tab. Chrome must already allow JavaScript from Apple Events, as required
by the original scripts. The server never changes this setting. If BT4G shows
verification, complete it yourself and retry. No interactive stdin prompts
are used, because stdin belongs to MCP.

The default search returns the first exact match with a readable detail hash.
It does not rank by seeders or size. `not_found` means all available search
pages were checked; other failure/limit statuses cannot establish absence.
Results validate metadata only; file contents and download availability are
not verified. No files are downloaded and no transfers are started.

## Setup

Use Python 3.10+ to create `.venv-bt4g-mcp`, then install
`requirements-bt4g-mcp.txt` into it. Register with Codex:

```sh
codex mcp add bt4g -- /Users/fangdingding/PycharmProjects/PythonProject/.venv-bt4g-mcp/bin/python /Users/fangdingding/PycharmProjects/PythonProject/bt4g_mcp_server.py
```

Configure `tool_timeout_sec = 600` under `[mcp_servers.bt4g]` for batches.
Restart Codex or open a new chat if the tools do not appear in the current chat.

Example request: "Use bt4g to search exact codes ONCE-031 and IENE-015 and
return the magnets."

The server imports the three existing scripts beside it. Keep them and the
virtual environment at these paths. Run regression checks with:

```sh
.venv-bt4g-mcp/bin/python -m unittest test_bt4g_mcp_server -v
```
