# Execution and review contract

Use Python 3.12 in `/Users/fangdingding/PycharmProjects/PythonProject/.venv-bt4g-mcp/bin/python`.
The example paths below are placeholders for the current user's inputs; do not reuse a historical code list, destination, or batch directory.

## 1. Collect all title pages

```sh
.venv-bt4g-mcp/bin/python bt4g_workflow.py collect --codes-file codes.txt --output-dir outputs/new-batch --max-pages 20
```

`codes.txt` accepts commas and whitespace.
Search queries strip trailing letter suffixes (`IBW-495Z` -> `IBW-495`) while
preserving the original requested code. Candidate rows record both `code` and
`query_code`. Results with the original suffix remain valid for model review.
The output contains `candidates.json`, `candidate_pages.md`, `review_prompt.md`, and `review_template.json`. Candidates
contain title, size evidence, page URL, and candidate ID; no magnets/hashes are
extracted in this phase. Size ranking is lexicographic: Thz first, then >2 GB,
then shortest title. The >900 MB minimum is strict; unknown sizes cannot qualify.

If collection is incomplete, resolve the browser/verification issue or increase
`--max-pages` and rerun collection before applying review. Complete codes resume
from saved search outcomes. A reviewed snapshot is immutable.

## 2. Perform actual language-model review

Read the entire candidate set and prompt. As Codex, reason semantically about
each title; do not generate a classifier or script that supplies decisions.
Author `review_decisions.json` based on the template:

```json
{
  "candidate_digest": "copy the exact template digest",
  "method": "language_model",
  "reviewer": "actual reviewing model/agent",
  "decisions": [
    {
      "code": "REQUESTED-CODE",
      "candidate_id": "copy the saved candidate ID",
      "decision": "accept",
      "reason": "Specific contextual evidence that this represents the requested video",
      "rank": 1
    }
  ]
}
```

Cover every candidate with `accept` or `reject`; rejected entries still need
reasons and can have `rank: null`. Rank accepted candidates uniquely per code.
The digest binds the decision to the original titles, page links, and sizes.
The CLI validates this record; an attestation field alone does not perform
language-model review. The Codex agent must genuinely do that reasoning.

Known rejection examples: “Love 191 Photos.zip” for LOVE-191, words/numbers
from unrelated identifiers, generic multi-code ZIP bait, and clearly timed
clips when the requested release is the full video. Release part indicators
can be legitimate; do not reject genuine multipart videos merely for having
several parts. Preferences do not override identity or the hard size minimum.

```sh
.venv-bt4g-mcp/bin/python bt4g_workflow.py apply-review --output-dir outputs/new-batch --review-file outputs/new-batch/review_decisions.json
```

This produces `review.json`. Changing any reviewed candidate invalidates the
review. Never carry approval from an old batch or fabricate approval to make
the transfer command pass.

## 3. Resolve and transfer the approved candidates

Only when the user has requested uploads and supplied a destination:

```sh
.venv-bt4g-mcp/bin/python bt4g_workflow.py transfer --output-dir outputs/new-batch --folder-id USER_FOLDER_ID
```

The runner resolves one approved page per code, verifies the unchanged title,
and sums video-file sizes (excluding ZIPs, photos and other non-video files).
Unknown video sizes and totals <=900,000,000 bytes are rejected. It then submits
one full magnet batch through 115 MCP after resolving all eligible codes. For
100 eligible codes with distinct hashes, the first call contains 100 magnets.
Status 2 in the requested folder is success; failure or non-completion after
10 seconds advances that code to its next approved page. All replacements for
failed codes form the next batch; completed codes are excluded. Receipts include
`upload_batches` with the exact code/hash membership and responses per round.
At most three selected pages are considered. No new search/fourth page is added.

An unavailable detail page can be skipped. Browser verification, account/quota
problems and ambiguous submissions remain blockers, not synthetic success.
Receipts are saved before submission. On restart, reconcile their info hashes;
never blindly repeat a write whose response was lost. Existing files/tasks in
other folders are left untouched and do not satisfy this destination.

## 4. Report

- `failed_codes.txt`: comma-separated codes that did not complete, including
  ignored and blocked cases. Detailed outcome/reason data stays in receipts.
- `final_results_v2.md`: reviewed successful titles, title pages, magnets and
  per-code outcome; no unsupported claim of content authenticity.
- `successful_magnets_v2.txt`: completed results only.
- `receipts_v2.json`: immutable review binding and resumable attempt history.

In the final chat response, paste the combined skipped/failed codes from
`failed_codes.txt` directly as one comma-separated line, preserving input order
and excluding completed codes. Label it `Skipped/failed codes:` and use a
single-line code block. Do not replace this list with a download link or counts.
If every code completed, explicitly write `Skipped/failed codes: None`.

## Implementation and verification

Workspace files: `bt4g_workflow.py` (stages/review/report), `bt4g_transfer.py`
(round-based 115 batch submission and per-code reconciliation), `bt4g_mcp_server.py`
(browser candidate collection/detail metadata), `bt4g_115_batch.py` (v2 CLI
alias), and `test_bt4g_workflow.py` (offline protocol/state tests).

```sh
.venv-bt4g-mcp/bin/python -m unittest test_bt4g_workflow test_bt4g_mcp_server -v
```

The updated server exposes `bt4g_search_candidates` and
`bt4g_resolve_candidate`. Reload/restart the BT4G MCP connection after source
changes so a previously loaded server does not keep the legacy tool set.
