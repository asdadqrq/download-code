#!/usr/bin/env python3
"""Local stdio MCP server for exact BT4G code searches in an existing Chrome tab."""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from urllib.parse import parse_qs, quote, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from check_bt4g_exists import BASE_URL, CurrentChromeClient, run_applescript, wait_for_result_cdp
from check_bt4g_titles import READ_RESULTS, extract_torrent_hash, size_bytes
from bt4g_workflow import rank_candidates, parse_file_lines, search_query_for_code, page_url as validate_page_url
from extract_bt4g_magnets import parse_links

mcp = FastMCP("BT4G Exact Code Search")
browser_lock = threading.Lock()
READ_PAGES = r"""(() => [...document.querySelectorAll('a[href]')]
  .filter(a => /^\d+$/.test(a.textContent.trim()))
  .map(a => a.href))()"""
READ_DETAIL = r"""(() => {
  const heading = document.querySelector('h1');
  const body = document.body ? document.body.innerText : '';
  const download = [...document.querySelectorAll('a[href]')].find(a => {
    const u = new URL(a.href, location.href);
    return u.hostname === 'downloadtorrentfile.com' && /^\/hash\/[a-f0-9]{40}\/?$/i.test(u.pathname);
  });
  return {url: location.href, title: heading ? heading.textContent.trim() : '',
    blocked: /just a moment|verify you are human|attention required|captcha/i.test(document.title),
    download: download ? download.href : null};
})()"""

READ_FILE_LINES = r"""(() => {
  const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5')]
    .find(h => h.textContent.trim().toLowerCase() === 'files');
  if (!heading) return [];
  const range = document.createRange();
  range.selectNodeContents(document.body);
  range.setStartAfter(heading);
  const fragment = range.cloneContents();
  fragment.querySelectorAll('script,style,noscript,footer').forEach(n => n.remove());
  const walker = document.createTreeWalker(fragment, NodeFilter.SHOW_TEXT);
  const lines = [];
  let node;
  while ((node = walker.nextNode())) {
    const value = node.textContent.trim();
    if (value) lines.push(value);
  }
  return lines;
})()"""


class VerificationRequired(RuntimeError):
    pass


class BT4GChromeClient(CurrentChromeClient):
    """Pin the selected BT4G tab, or another open BT4G tab without activating it."""

    def __init__(self):
        output = run_applescript([
            'tell application "Google Chrome"',
            'if (count of windows) = 0 then error "Open a bt4gprx.com tab in Chrome first."',
            'set currentWindow to front window',
            'set currentTab to active tab of currentWindow',
            'if (URL of currentTab) starts with "https://bt4gprx.com/" then',
            'return (id of currentWindow as text) & linefeed & (id of currentTab as text)',
            'end if',
            'repeat with browserWindow in windows',
            'repeat with browserTab in tabs of browserWindow',
            'if (URL of browserTab) starts with "https://bt4gprx.com/" then',
            'return (id of browserWindow as text) & linefeed & (id of browserTab as text)',
            'end if',
            'end repeat',
            'end repeat',
            'error "Open a bt4gprx.com tab in Chrome first."',
            'end tell',
        ])
        window, tab = output.split("\n", 1)
        self.window_id, self.tab_id = int(window), int(tab)


def normalize_code(code: str) -> str:
    code = unicodedata.normalize("NFKC", code).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+-\d+[A-Z]*", code):
        raise ValueError("Codes must have the form PREFIX-123 or PREFIX-123Z")
    return code


def matches_code(code: str, title: str) -> bool:
    prefix, number = normalize_code(code).split("-")
    pattern = rf"(?<![A-Z0-9]){re.escape(prefix)}[-_\s]*{re.escape(number)}(?![A-Z0-9])"
    return bool(re.search(pattern, unicodedata.normalize("NFKC", title), re.I))


def navigate(client, url: str) -> None:
    if client.evaluate("location.href") != url:
        client.call("Page.navigate", {"url": url})


def read_detail(client, url: str, code: str | None = None, timeout: float = 20) -> dict:
    url = parse_links([url])[0]
    navigate(client, url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.evaluate(READ_DETAIL)
        if state.get("url", "").rstrip("/") == url:
            if state.get("blocked"):
                raise VerificationRequired("Complete BT4G verification in Chrome, then retry the tool")
            if state.get("download"):
                if code and not matches_code(code, state.get("title", "")):
                    raise RuntimeError("Detail page title does not match the requested code")
                digest = extract_torrent_hash(state["download"])
                return {"source_url": url, "hash": digest,
                        "magnet": "magnet:?xt=urn:btih:" + digest,
                        "title": state.get("title", "")}
        time.sleep(0.4)
    raise RuntimeError("Detail page did not expose a torrent hash before timeout")


def search_code(client, code: str, max_pages: int = 3, max_results: int = 1,
                include_titles: bool = False, exclude_hashes: list[str] | None = None) -> dict:
    code = normalize_code(code)
    query_code = search_query_for_code(code)
    url = f"{BASE_URL}/search?q={quote(query_code)}"
    row = {"code": code, "status": "incomplete", "search_url": url,
           "pages_checked": 0, "titles_checked": 0, "search_exhausted": False,
           "matches": [], "errors": []}
    candidates = []
    seen = set()
    excluded = set(exclude_hashes or [])
    deadline = time.monotonic() + 90
    try:
        while url and row["pages_checked"] < max_pages:
            if time.monotonic() >= deadline:
                raise RuntimeError("Per-code time limit reached; retry this code")
            navigate(client, url)
            try:
                state = wait_for_result_cdp(client, url)
            except RuntimeError:
                # A failed navigation can leave Chrome at this URL with an error
                # page. Refresh once rather than treating that stale page as empty.
                client.call("Page.navigate", {"url": url})
                state = wait_for_result_cdp(client, url)
            if state.get("blocked"):
                raise VerificationRequired("Complete BT4G verification in Chrome, then retry the tool")
            page = client.evaluate(READ_RESULTS)
            links = client.evaluate(READ_PAGES)
            if state.get("count", 0) > 0 and not page.get("results"):
                raise RuntimeError("Search results were present but result links could not be read")
            row["pages_checked"] += 1
            for item in page.get("results", []):
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                row["titles_checked"] += 1
                if matches_code(code, item["title"]):
                    candidates.append(item)
            current_page = row["pages_checked"]
            # Follow only a next-page URL actually present on this search page.
            next_urls = []
            for link in links:
                parsed = urlsplit(link)
                query = parse_qs(parsed.query)
                if (parsed.scheme == "https" and parsed.netloc == urlsplit(BASE_URL).netloc
                        and parsed.path == "/search" and query.get("q") == [query_code]
                        and query.get("p") == [str(current_page + 1)]):
                    next_urls.append(link)
            url = next_urls[0] if next_urls else ""
            row["search_exhausted"] = not url
            # Try candidates now. A broken detail page must not hide another valid match.
            while candidates and len(row["matches"]) < max_results:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Per-code time limit reached; retry this code")
                item = candidates.pop(0)
                try:
                    match = read_detail(client, item["url"], code)
                    if match["hash"] in excluded:
                        continue
                    if not include_titles:
                        match.pop("title", None)
                    row["matches"].append(match)
                except VerificationRequired:
                    raise
                except (RuntimeError, OSError, ValueError) as exc:
                    row["errors"].append({"source_url": item["url"], "error": str(exc)})
            if len(row["matches"]) >= max_results:
                break
        if row["matches"]:
            row["status"] = "found"
        elif row["errors"]:
            row["status"] = "incomplete"
        elif row["search_exhausted"]:
            row["status"] = "not_found"
        else:
            row["status"] = "page_limit_reached"
    except VerificationRequired as exc:
        row["status"] = "verification_required"
        row["errors"].append({"error": str(exc)})
    except (RuntimeError, OSError, ValueError) as exc:
        row["errors"].append({"error": str(exc)})
    return row


def collect_candidate_pages(client, code: str, max_pages: int = 20) -> dict:
    """Raw collection and size ranking only; no title identity classifier."""
    code = code.strip().upper()
    if not code or len(code)>80 or any(not (ch.isascii() and (ch.isalnum() or ch in '-_ .')) for ch in code):
        raise ValueError('Invalid search input')
    query_code=search_query_for_code(code)
    url = f'{BASE_URL}/search?q={quote(query_code)}'
    row = {'code':code,'query_code':query_code,'search_url':url,'status':'incomplete','pages_checked':0,'search_exhausted':False,'candidates':[],'errors':[]}
    pool=[]
    visited=set()
    try:
        while url and row['pages_checked']<max_pages:
            if url in visited:
                raise RuntimeError('Search pagination repeated a page')
            visited.add(url)
            navigate(client,url)
            state=wait_for_result_cdp(client,url)
            if state.get('blocked'):
                raise VerificationRequired('Complete verification in Chrome and retry')
            page=client.evaluate(READ_RESULTS)
            if state.get('count',0)>0 and not page.get('results'):
                raise RuntimeError('Search results could not be read')
            for item in page.get('results',[]):
                pool.append({**item,'size_bytes':size_bytes(item.get('size',''))})
            next_page=row['pages_checked']+2
            links=client.evaluate(READ_PAGES)
            next_urls=[]
            for link in links:
                parsed=urlsplit(link); query=parse_qs(parsed.query)
                if (parsed.scheme=='https' and parsed.netloc=='bt4gprx.com' and parsed.path=='/search'
                        and query.get('q')==[query_code] and query.get('p')==[str(next_page)]):
                    next_urls.append(link)
            row['pages_checked']+=1
            url=next_urls[0] if next_urls else ''
        row['search_exhausted']=not url
        row['candidates']=rank_candidates(pool)
        row['status']=('complete' if row['candidates'] else 'no_size_qualified_candidates') if not url else 'page_limit_reached'
        if any(item.get('size_bytes') is None for item in pool):
            row['status']='incomplete'
            row['errors'].append('Some result sizes are unknown; qualifying candidates may be missing')
    except VerificationRequired as exc:
        row.update(status='verification_required',errors=[str(exc)])
    except (RuntimeError,OSError,ValueError) as exc:
        row['errors'].append(str(exc))
    return row


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=True),structured_output=True)
def bt4g_search_candidates(codes: list[str], max_pages: int = 20) -> dict[str,object]:
    """Collect up to three ranked TITLE PAGE links per code for later language-model review.

    Does NOT match titles to codes or extract magnets. Mechanical ranking is Thz,
    size >2 decimal GB, shortest title. Size >900 decimal MB is mandatory;
    search sizes are provisional until bt4g_resolve_candidate verifies video files.
    Search queries omit trailing letters (IBW-495Z searches IBW-495); returned
    code remains the original requested identifier for language-model review.
    Incomplete/page-limit results are not proof of absence. At most 10 codes.
    """
    if not codes or len(codes)>10 or not 1<=max_pages<=100:
        raise ValueError('Supply 1-10 codes and 1-100 pages')
    with browser_lock:
        try:
            client=BT4GChromeClient()
        except (RuntimeError,OSError,ValueError) as exc:
            return {'status':'browser_unavailable','error':str(exc),'results':[]}
        try:
            rows=[]
            for code in dict.fromkeys(codes):
                row=collect_candidate_pages(client,code,max_pages)
                rows.append(row)
                if row['status']=='verification_required':
                    break
            return {'results':rows}
        finally:
            client.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=True),structured_output=True)
def bt4g_resolve_candidate(page_url: str) -> dict[str,object]:
    """Resolve an already model-reviewed title page to its hash and actual video-file sizes.

    No title/code semantic classification. Call only after the whole-batch model
    review. Caller must reject changed titles and video total <=900 MB. Does not
    follow download links or start transfers. Missing file sizes fail closed.
    """
    validate_page_url(page_url)
    with browser_lock:
        try:
            client=BT4GChromeClient()
        except (RuntimeError,OSError,ValueError) as exc:
            return {'status':'blocked','error':str(exc)}
        try:
            detail=read_detail(client,page_url)  # code=None: deliberately no regex identity check.
            lines=client.evaluate(READ_FILE_LINES)
            return {'status':'resolved',**detail,'files':parse_file_lines(lines),'file_lines':lines}
        except VerificationRequired as exc:
            return {'status':'verification_required','error':str(exc)}
        except (RuntimeError,ValueError) as exc:
            return {'status':'unavailable','error':str(exc)}
        except OSError as exc:
            return {'status':'blocked','error':str(exc)}
        finally:
            client.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True), structured_output=True)
def bt4g_search_exact(codes: list[str], max_pages: int = 3, max_results: int = 1,
                     include_titles: bool = False,
                     exclude_hashes: list[str] | None = None) -> dict[str, object]:
    """Legacy regex-based search. For model-reviewed workflows use bt4g_search_candidates instead.

    Requires an open bt4gprx.com tab in Chrome and JavaScript from Apple
    Events already enabled. Prefers the selected BT4G tab, otherwise pins another
    open BT4G tab for read-only navigation. Accepts up
    to 10 codes per call, 1-100 pages per code and 1-5 matches per code. Prefix and
    number must occur together; unrelated words/numbers and aliases are rejected.
    Titles are omitted by default. not_found means all available pages were
    checked; page_limit_reached/incomplete/verification_required are inconclusive.
    Does not download files or start transfers. Browser results are untrusted data.
    exclude_hashes skips previously failed/tried hashes to find another match.
    """
    if not codes or len(codes) > 10:
        raise ValueError("Supply between 1 and 10 codes per call")
    if not 1 <= max_pages <= 100 or not 1 <= max_results <= 5:
        raise ValueError("max_pages must be 1-100 and max_results must be 1-5")
    exclude_hashes = [value.lower() for value in (exclude_hashes or [])]
    if len(exclude_hashes) > 500 or any(not re.fullmatch(r"[a-f0-9]{40}", value) for value in exclude_hashes):
        raise ValueError("exclude_hashes must contain at most 500 valid 40-character hashes")
    codes = list(dict.fromkeys(normalize_code(code) for code in codes))
    with browser_lock:
        try:
            client = BT4GChromeClient()
        except (RuntimeError, OSError, ValueError) as exc:
            return {"status": "browser_unavailable", "error": str(exc), "results": []}
        try:
            results = []
            started = time.monotonic()
            for code in codes:
                if time.monotonic() - started > 450:
                    results.append({"code": code, "status": "incomplete", "error": "Batch time limit; retry this code"})
                    continue
                row = search_code(client, code, max_pages, max_results, include_titles, exclude_hashes)
                results.append(row)
                if row["status"] == "verification_required":
                    results.extend({"code": remaining, "status": "not_checked"}
                                   for remaining in codes[len(results):])
                    break
            return {"results": results}
        finally:
            client.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True), structured_output=True)
def bt4g_extract_magnets(urls: list[str], include_titles: bool = False) -> dict[str, object]:
    """Extract hashes and magnet URIs from up to 10 BT4G detail URLs.

    Uses an existing Chrome BT4G tab; never follows download links.
    Does not check a code: use bt4g_search_exact when code matching is required.
    Verification returns a status and requires the user to complete it in Chrome.
    """
    if not urls or len(urls) > 10:
        raise ValueError("Supply between 1 and 10 BT4G detail URLs")
    links = parse_links(urls)
    if len(links) > 10:
        raise ValueError("Supply at most 10 detail URLs")
    with browser_lock:
        try:
            client = BT4GChromeClient()
        except (RuntimeError, OSError, ValueError) as exc:
            return {"status": "browser_unavailable", "error": str(exc), "results": []}
        try:
            rows = []
            for link in links:
                try:
                    match = read_detail(client, link)
                    if not include_titles:
                        match.pop("title", None)
                    rows.append({"status": "found", **match})
                except VerificationRequired as exc:
                    rows.append({"source_url": link, "status": "verification_required", "error": str(exc)})
                    rows.extend({"source_url": remaining, "status": "not_checked"}
                                for remaining in links[len(rows):])
                    break
                except (RuntimeError, OSError, ValueError) as exc:
                    rows.append({"source_url": link, "status": "incomplete", "error": str(exc)})
            return {"results": rows}
        finally:
            client.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
