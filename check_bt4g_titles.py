#!/usr/bin/env python3
"""Search BT4G in the selected Chrome tab and check first-page result titles."""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from check_bt4g_exists import (
    BASE_URL,
    CurrentChromeClient,
    VerificationRequired,
    parse_queries,
    wait_for_result_cdp,
)

# Keep the trailing marker boundary to avoid matching longer suffixes.
PATTERNS = {
    marker: re.compile(re.escape(marker) + r"(?![a-z0-9])", re.IGNORECASE)
    for marker in ("-U", "UC", "uncensored", "AI")
}

READ_RESULTS = r"""
(() => {
  const links = [...document.querySelectorAll('a[href]')];
  const results = links.filter(a => {
    const u = new URL(a.href, location.href);
    return u.origin === location.origin && /^\/(magnet|hash)\//.test(u.pathname);
  }).map(a => {
    let size = '';
    // Walk up only within this result, excluding the title from size matching.
    for (let node = a.parentElement; node && node !== document.body; node = node.parentElement) {
      const resultUrls = new Set([...node.querySelectorAll('a[href]')]
        .filter(x => /^\/(magnet|hash)\//.test(new URL(x.href, location.href).pathname))
        .map(x => x.href));
      if (resultUrls.size > 1) break;
      const copy = node.cloneNode(true);
      copy.querySelectorAll('a[href]').forEach(x => x.remove());
      const match = copy.textContent.match(/Total\s+Size\s*:\s*(\d+(?:[.,]\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b/i)
        || copy.textContent.match(/\b(\d+(?:[.,]\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b/i);
      if (match) { size = `${match[1]} ${match[2]}`; break; }
    }
    return {title: a.textContent.trim(), url: a.href, size};
  })
    .filter(a => a.title);
  return {results};
})()
"""


def match_title(title: str) -> list[str]:
    return [marker for marker, pattern in PATTERNS.items() if pattern.search(title)]


def size_bytes(size: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\s*", size, re.IGNORECASE)
    if not match:
        return None
    unit = match.group(2).upper()
    power = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4,
             "KIB": 1, "MIB": 2, "GIB": 3, "TIB": 4}[unit]
    return float(match.group(1).replace(",", ".")) * (1024 if "I" in unit else 1000) ** power


def search(client, code: str, size_rank: int = 1) -> dict:
    if size_rank < 1:
        raise ValueError("size_rank must be at least 1")
    url = f"{BASE_URL}/search?q={quote(code)}"
    result = {
        "code": code,
        "search_url": url,
        "status": "complete",
        "scope": "first_page",
        "size_rank": size_rank,
        "pages_checked": 0,
        "titles_checked": 0,
        "contains": {marker: False for marker in PATTERNS},
        "matches": [],
        "results": [],
        "error": "",
    }
    seen_results = set()
    try:
        if client.evaluate("location.href") != url:
            client.call("Page.navigate", {"url": url})
        state = wait_for_result_cdp(client, url)
        while state.get("blocked"):
            input("Complete verification in the same Chrome tab, then press Enter: ")
            state = wait_for_result_cdp(client, url)
        page = client.evaluate(READ_RESULTS)
        if state.get("count", 0) > 0 and not page["results"]:
            raise RuntimeError("Results exist but title links were not recognized; selectors may have changed")
        result["pages_checked"] = 1
        for item in page["results"]:
            if item["url"] in seen_results:
                continue
            seen_results.add(item["url"])
            result["titles_checked"] += 1
            result["results"].append({**item, "size_bytes": size_bytes(item.get("size", ""))})
            markers = match_title(item["title"])
            if markers:
                result["matches"].append({**item, "markers": markers})
                for marker in markers:
                    result["contains"][marker] = True
        if not result["matches"] and result["results"]:
            if any(item["size_bytes"] is None for item in result["results"]):
                result["error"] = "Size-ranked fallback unavailable: could not read every result's file size"
            elif size_rank > len(result["results"]):
                result["error"] = (
                    f"Size rank {size_rank} unavailable: only {len(result['results'])} first-page results"
                )
    except (RuntimeError, OSError, ValueError, VerificationRequired) as exc:
        result["status"] = "incomplete"
        result["error"] = str(exc)
    # A partial search can establish presence, but cannot establish absence.
    if result["status"] != "complete":
        result["contains"] = {k: True if v else None for k, v in result["contains"].items()}
    return result


def found_value(row: dict) -> str:
    values = row["contains"].values()
    if any(value is True for value in values):
        return "yes"
    if any(value is None for value in values) or row["status"] != "complete":
        return "unknown"
    return "no"


def preferred_link(row: dict) -> str:
    """Prefer marker priority, otherwise the requested size rank on the first page."""
    priorities = {"U": 0, "UC": 0, "AI": 1, "UNCENSORED": 2}
    matches = row["matches"]
    if not matches:
        results = row.get("results", [])
        rank = row.get("size_rank", 1)
        if (not results or not 1 <= rank <= len(results)
                or any(item.get("size_bytes") is None for item in results)):
            return ""
        ranked = sorted(results, key=lambda item: item["size_bytes"], reverse=True)
        return ranked[rank - 1]["url"]
    best = min(matches, key=lambda item: min(
        (priorities.get(marker.lstrip("-").upper(), 3)
         for marker in item["markers"]),
        default=3,
    ))
    return best["url"]


READ_DOWNLOAD_LINK = r"""
(() => {
  const text = `${document.title}\n${document.body ? document.body.innerText : ''}`;
  const link = [...document.querySelectorAll('a[href]')].find(a => {
    const u = new URL(a.href, location.href);
    return u.hostname === 'downloadtorrentfile.com' &&
      /^\/hash\/[a-f0-9]{40}\/?$/i.test(u.pathname);
  });
  return {
    url: location.href,
    blocked: /just a moment|verify you are human|attention required|captcha/i.test(text),
    download: link ? link.href : null
  };
})()
"""


def extract_torrent_hash(download_url: str) -> str:
    match = re.fullmatch(r"/hash/([a-f0-9]{40})/?", urlsplit(download_url).path, re.IGNORECASE)
    if not match:
        raise ValueError("Torrent Download URL does not contain a 40-character hash")
    return match.group(1).lower()


def read_torrent_hash(client, detail_url: str, timeout: float = 20) -> str:
    # Only visit the BT4G detail page; do not follow the download link.
    if client.evaluate("location.href") != detail_url:
        client.call("Page.navigate", {"url": detail_url})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.evaluate(READ_DOWNLOAD_LINK)
        if state["url"] == detail_url:
            if state["blocked"]:
                input("Complete verification in the same Chrome tab, then press Enter: ")
                deadline = time.monotonic() + timeout
            elif state["download"]:
                return extract_torrent_hash(state["download"])
        time.sleep(0.4)
    raise RuntimeError("Could not read Torrent Download link from the preferred result")


def add_torrent_hash(client, row: dict) -> None:
    row["torrent_hash"] = ""
    link = preferred_link(row)
    if not link:
        return
    try:
        row["torrent_hash"] = read_torrent_hash(client, link)
    except (RuntimeError, OSError, ValueError) as exc:
        row["error"] = "; ".join(filter(None, [row["error"], f"Hash extraction: {exc}"]))


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "code", "found", "preferred_link", "torrent_hash", "status", "scope", "pages_checked",
        "titles_checked", "matching_titles", "matching_urls", "search_url", "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            link = preferred_link(row)
            exported_matches = row["matches"]
            if not exported_matches and row.get("torrent_hash") and link:
                exported_matches = [
                    item for item in row.get("results", []) if item["url"] == link
                ]
            writer.writerow({
                **{key: row[key] for key in (
                    "code", "status", "scope", "pages_checked", "titles_checked",
                    "search_url", "error",
                )},
                "found": found_value(row),
                "preferred_link": link,
                "torrent_hash": (
                    "magnet:?xt=urn:btih:" + row["torrent_hash"]
                    if row.get("torrent_hash") else ""
                ),
                "matching_titles": "\n".join(item["title"] for item in exported_matches),
                "matching_urls": "\n".join(item["url"] for item in exported_matches),
            })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", help="Comma- or space-separated IDs")
    parser.add_argument("-o", "--output", type=Path, default=Path("bt4g_title_results-1.csv"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument(
        "--size-rank", type=int, default=1,
        help="When no marker matches, select by descending size: 1=largest (default), 2=second-largest, 3=third-largest",
    )
    args = parser.parse_args()
    if args.delay < 0.5:
        parser.error("--delay must be at least 0.5")
    if args.size_rank < 1:
        parser.error("--size-rank must be at least 1")
    codes = parse_queries(args.codes or [input("Enter IDs (comma-separated): ")])
    client = CurrentChromeClient()
    rows = []
    try:
        for index, code in enumerate(codes):
            if index:
                time.sleep(args.delay)
            row = search(client, code, size_rank=args.size_rank)
            add_torrent_hash(client, row)
            rows.append(row)
            print(f"{code}: {found_value(row)}")
            if row["error"]:
                print(f"  {row['error']}")
            write_csv(args.output, rows)
    finally:
        client.close()
    print(f"Saved: {args.output.resolve()}")


if __name__ == "__main__":
    main()
