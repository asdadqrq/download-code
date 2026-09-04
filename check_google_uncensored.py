#!/usr/bin/env python3
import argparse
import base64
import csv
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
from urllib.request import Request, urlopen


GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&num={num}&hl=en&gbv=1"
GOOGLE_BROWSER_URL = "https://www.google.com/search?q={query}&num={num}&hl=en"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1?key={key}&cx={cx}&q={query}&num={num}"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/?q={query}"
BING_SEARCH_URL = "https://www.bing.com/search?q={query}"
DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def split_codes(values):
    codes = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                codes.append(part.upper())
    return codes


def normalize_code(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def code_patterns(code):
    normalized = normalize_code(code)
    escaped = re.escape(code.upper()).replace("\\-", "[-\\s_]?")
    return [
        re.compile(r"(?<![A-Z0-9])" + escaped + r"(?![A-Z0-9])", re.IGNORECASE),
        re.compile(r"(?<![A-Z0-9])" + re.escape(normalized) + r"(?![A-Z0-9])", re.IGNORECASE),
    ]


UNCENSORED_RE = re.compile(
    r"\b(un[-\s_]?censored|de[-\s_]?censored|uncensor(?:ed)?|decensor(?:ed)?|uncen)\b",
    re.IGNORECASE,
)
GENERIC_RESULT_RE = re.compile(
    r"\b(results?\s+for|search\s+results?|porn\s+search|jav\s+porn\s+videos|recent/any-category|free\s+download|streaming\s+collection|japanese\s+adult\s+uncensored\s+and\s+censored)\b",
    re.IGNORECASE,
)
GENERIC_UNCENSORED_RE = re.compile(
    r"\b(uncensored\s+and\s+censored|censored\s+and\s+uncensored|uncensored\s+or\s+censored|censored\s+or\s+uncensored|uncensored\s+.*?\s+censored|censored\s+.*?\s+uncensored)\b",
    re.IGNORECASE,
)
BOILERPLATE_UNCENSORED_RE = re.compile(
    r"\b(hd\s+uncensored\s+jav|brand\s+new\s+free\s+jav\s+website|free\s+jav\s+website)\b",
    re.IGNORECASE,
)
ANY_CODE_RE = re.compile(r"\b[A-Z]{2,6}[-\s_]?\d{2,6}(?:[-\s_]?[A-Z])?\b", re.IGNORECASE)


def clean_text(value):
    value = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", "", value or "")
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url, timeout=20):
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_json_url(url, timeout=2):
    return json.loads(fetch_url(url, timeout=timeout))


def cdp_call(ws, method, params=None, call_id=None):
    call_id = call_id or int(time.time() * 1000)
    ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == call_id:
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools error in {method}: {message['error']}")
            return message.get("result", {})


def wait_for_debugger(port, timeout=15):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return read_json_url(f"http://127.0.0.1:{port}/json/version")
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Chrome remote debugger did not start: {last_error}")


def normalize_google_link(href):
    if not href:
        return ""
    href = html.unescape(href)
    parsed = urlparse(href)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        return parse_qs(parsed.query).get("q", [""])[0]
    if href.startswith("/url?"):
        return parse_qs(urlparse(href).query).get("q", [""])[0]
    return href


def is_search_result_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not parsed.scheme.startswith("http"):
        return False
    if "google." in host or host in {"webcache.googleusercontent.com"}:
        return False
    return True


def is_generic_query_result(result, code):
    title = result.get("title", "")
    snippet = result.get("snippet", "")
    link = result.get("link", "")
    parsed = urlparse(link)
    path_text = unquote(parsed.path or "")
    query_text = unquote(parsed.query or "")
    page_text = " ".join([title, snippet, path_text])

    has_search_query = any(key in parse_qs(parsed.query) for key in ["q", "query", "keyword", "search"])
    code_in_path = any(pattern.search(path_text) for pattern in code_patterns(code))
    generic_tag_or_category = bool(re.search(r"/(tag|tags|category|categories|search)/", path_text, re.I))

    if has_search_query and not code_in_path:
        return True
    if generic_tag_or_category and GENERIC_RESULT_RE.search(page_text):
        return True
    if generic_tag_or_category and GENERIC_UNCENSORED_RE.search(page_text):
        return True
    if GENERIC_RESULT_RE.search(page_text) and not code_in_path:
        return True
    if GENERIC_RESULT_RE.search(page_text) and any(pattern.search(query_text) for pattern in code_patterns(code)):
        return True
    return False


def field_confirms_code_uncensored(field, patterns, code):
    if not field or GENERIC_UNCENSORED_RE.search(field) or BOILERPLATE_UNCENSORED_RE.search(field):
        return False

    gap = r"(?:[\s\[\]\(\)\{\}:_\-–—|/\\.,]*|[\s\[\]\(\)\{\}:_\-–—|/\\.,]*(?:leak(?:ed)?|mosaic|version|vr)?[\s\[\]\(\)\{\}:_\-–—|/\\.,]*)"
    keyword = r"(?:un[-\s_]?censored|de[-\s_]?censored|uncensor(?:ed)?|decensor(?:ed)?|uncen)"
    for pattern in patterns:
        code_pattern = pattern.pattern
        if re.search(code_pattern + gap + keyword, field, re.IGNORECASE):
            return True
        if re.search(keyword + gap + code_pattern, field, re.IGNORECASE):
            return True
    return False


class ChromeGoogleSearcher:
    method = "google_chrome_isolated"
    def __init__(self, chrome_path=None, wait_seconds=4):
        self.chrome_path = chrome_path or DEFAULT_CHROME_PATH
        self.wait_seconds = wait_seconds
        self.process = None
        self.user_data_dir = None
        self.ws = None

    def __enter__(self):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "Install websocket-client first: /opt/anaconda3/bin/python -m pip install websocket-client"
            ) from exc

        if not Path(self.chrome_path).exists():
            discovered = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
            if discovered:
                self.chrome_path = discovered
            else:
                raise RuntimeError(f"Chrome was not found at {self.chrome_path}")

        port = find_free_port()
        self.user_data_dir = tempfile.mkdtemp(prefix="google-search-chrome-")
        self.process = subprocess.Popen(
            [
                self.chrome_path,
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for_debugger(port)
        tabs = read_json_url(f"http://127.0.0.1:{port}/json/list")
        target = tabs[0]
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10)
        cdp_call(self.ws, "Page.enable", call_id=1)
        cdp_call(self.ws, "Runtime.enable", call_id=2)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.ws:
            self.ws.close()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.user_data_dir:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def search(self, query, limit=10):
        url = GOOGLE_BROWSER_URL.format(query=quote_plus(query), num=limit)
        cdp_call(self.ws, "Page.navigate", {"url": url}, call_id=10)
        time.sleep(max(0, self.wait_seconds))
        expression = f"""
(() => {{
  const limit = {int(limit)};
  const rows = [];
  const seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a'))) {{
    const title = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    const link = a.href || '';
    if (!title || !link || seen.has(link)) continue;
    seen.add(link);
    const block = a.closest('div');
    const snippet = block ? (block.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    rows.push({{ title, snippet, link }});
  }}
  return rows.slice(0, limit * 8);
}})()
"""
        result = cdp_call(
            self.ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            call_id=11,
        )
        raw_results = result.get("result", {}).get("value", [])
        results = []
        seen = set()
        for item in raw_results:
            link = normalize_google_link(item.get("link", ""))
            if not is_search_result_url(link) or link in seen:
                continue
            seen.add(link)
            results.append(
                {
                    "title": clean_text(item.get("title", "")),
                    "snippet": clean_text(item.get("snippet", "")),
                    "link": link,
                }
            )
            if len(results) >= limit:
                break
        return results


def google_result_script(query, limit):
    return r"""(() => {
      const expected = %QUERY%;
      const current = new URL(location.href);
      if (current.pathname !== '/search' || current.searchParams.get('q') !== expected)
        return {ready:false, results:[]};
      const root = document.querySelector('#rso') || document.querySelector('#search');
      const rows = [];
      const seen = new Set();
      for (const heading of root ? root.querySelectorAll('h3') : []) {
        const anchor = heading.closest('a');
        if (!anchor || !anchor.href || seen.has(anchor.href)) continue;
        seen.add(anchor.href);
        const block = heading.closest('.MjjYud') || heading.parentElement;
        rows.push({title:heading.innerText.trim(), link:anchor.href,
          snippet:(block.innerText || '').replace(/\s+/g,' ').trim()});
      }
      const text = document.body ? document.body.innerText : '';
      const blocked = !rows.length && /unusual traffic|not a robot|verify you are human/i.test(text);
      const empty = /did not match any documents|no results found/i.test(text);
      return {ready:rows.length > 0 || empty, blocked, results:rows.slice(0,%LIMIT%)};
    })()""".replace('%QUERY%', json.dumps(query)).replace('%LIMIT%', str(int(limit)))


class ExistingChromeGoogleSearcher:
    """Search in the user's normal macOS Chrome profile without launching a new one."""
    method = "google_chrome"

    def __init__(self, wait_seconds=4):
        self.timeout = max(15, wait_seconds + 10)
        self.window_id = None
        self.tab_id = None

    @staticmethod
    def run(lines):
        result = subprocess.run(
            ['osascript', '-'], input='\n'.join(lines), text=True,
            capture_output=True, timeout=30,
        )
        if result.returncode:
            message = result.stderr.strip()
            if 'Executing JavaScript through AppleScript is turned off' in message:
                raise RuntimeError('Enable Chrome > View > Developer > Allow JavaScript from Apple Events.')
            raise RuntimeError(message or 'Could not read normal Chrome')
        return result.stdout.strip()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Leave the search tab available for inspection, including any CAPTCHA.
        return False

    def evaluate(self, script):
        encoded = json.dumps('JSON.stringify(' + script + ')', ensure_ascii=False)
        return json.loads(self.run([
            'tell application "Google Chrome"',
            f'set t to tab id {self.tab_id} of window id {self.window_id}',
            f'return execute t javascript {encoded}',
            'end tell',
        ]))

    def search(self, query, limit=10):
        url = GOOGLE_BROWSER_URL.format(query=quote_plus(query), num=limit)
        if self.tab_id is None:
            # Reuse an exact query tab when possible; never overwrite unrelated tabs.
            stem = 'https://www.google.com/search?q=' + quote_plus(query)
            result = self.run([
                'tell application "Google Chrome"',
                'repeat with w in windows',
                'repeat with t in tabs of w',
                f'if URL of t is {json.dumps(stem)} or URL of t starts with {json.dumps(stem + "&")} then',
                'return (id of w as text) & linefeed & (id of t as text)',
                'end if', 'end repeat', 'end repeat',
                'if (count of windows) = 0 then make new window',
                'set w to front window',
                f'set t to make new tab at end of tabs of w with properties {{URL:{json.dumps(url)}}}',
                'return (id of w as text) & linefeed & (id of t as text)',
                'end tell',
            ])
            self.window_id, self.tab_id = map(int, result.splitlines())
        else:
            self.run(['tell application "Google Chrome"',
                      f'set URL of tab id {self.tab_id} of window id {self.window_id} to {json.dumps(url)}',
                      'end tell'])
        deadline = time.monotonic() + self.timeout
        script = google_result_script(query, limit)
        while time.monotonic() < deadline:
            state = self.evaluate(script)
            if state.get('blocked'):
                raise RuntimeError('Google requires verification in the Chrome search tab. Complete it and rerun.')
            if state.get('ready'):
                return [dict(item, link=normalize_google_link(item['link']))
                        for item in state['results'] if is_search_result_url(normalize_google_link(item['link']))]
            time.sleep(0.5)
        raise RuntimeError('Google result headings did not load in normal Chrome')


class SafariGoogleSearcher:
    method = "google_safari"
    def __init__(self, wait_seconds=4):
        self.wait_seconds = wait_seconds

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def search(self, query, limit=10):
        script = """
on run argv
    set searchUrl to item 1 of argv
    set waitSeconds to item 2 of argv as integer
    tell application "Safari"
        activate
        open location searchUrl
        delay waitSeconds
        set jsonText to do JavaScript "
(() => {
  const limit = %LIMIT%;
  const rows = [];
  const seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a'))) {
    const title = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    const link = a.href || '';
    if (!title || !link || seen.has(link)) continue;
    seen.add(link);
    const block = a.closest('div');
    const snippet = block ? (block.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    rows.push({ title, snippet, link });
  }
  return JSON.stringify(rows.slice(0, limit * 8));
})()
" in front document
    end tell
    return jsonText
end run
"""
        url = GOOGLE_SEARCH_URL.format(query=quote_plus(query), num=limit)
        script = script.replace("%LIMIT%", str(int(limit)))
        result = subprocess.run(
            ["osascript", "-", url, str(max(0, int(self.wait_seconds)))],
            input=script,
            text=True,
            capture_output=True,
            timeout=max(30, int(self.wait_seconds) + 20),
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "Safari could not return Google results. "
                "Enable Safari > Develop > Allow JavaScript from Apple Events. "
                f"Details: {message}"
            )
        raw_results = json.loads(result.stdout.strip() or "[]")
        results = []
        seen = set()
        for item in raw_results:
            link = normalize_google_link(item.get("link", ""))
            if not is_search_result_url(link) or link in seen:
                continue
            seen.add(link)
            results.append(
                {
                    "title": clean_text(item.get("title", "")),
                    "snippet": clean_text(item.get("snippet", "")),
                    "link": link,
                }
            )
            if len(results) >= limit:
                break
        return results


def google_html_search(query, limit=10):
    html_text = fetch_url(GOOGLE_SEARCH_URL.format(query=quote_plus(query), num=limit))
    results = []

    # Current Google result blocks usually contain /url?q= links followed by title text.
    for match in re.finditer(r'<a href="/url\?q=([^"&]+)[^"]*"[^>]*>(.*?)</a>', html_text, re.I | re.S):
        url = unquote(match.group(1))
        if "google." in urlparse(url).netloc:
            continue
        text = clean_text(match.group(2))
        if not text:
            continue
        results.append({"title": text, "snippet": "", "link": url})
        if len(results) >= limit:
            break

    if results:
        return results

    # Fallback for Google pages that encode result URLs in href="/search?...".
    for href in re.findall(r'href="([^"]+)"', html_text):
        href = html.unescape(href)
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        url = params.get("q", [""])[0] if parsed.path == "/url" else ""
        if not url:
            continue
        if "google." in urlparse(url).netloc:
            continue
        results.append({"title": "", "snippet": "", "link": url})
        if len(results) >= limit:
            break
    return results


def extract_redirect_url(url):
    url = html.unescape(url or "")
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.hostname in {'www.bing.com', 'bing.com'} and parsed.path == '/ck/a':
        destination = parse_qs(parsed.query).get('u', [''])[0]
        if destination.startswith('a1'):
            try:
                encoded = destination[2:]
                return base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                return url
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        return parse_qs(parsed.query).get("uddg", [""])[0]
    if "google." in parsed.netloc and parsed.path == "/url":
        return parse_qs(parsed.query).get("q", [""])[0]
    return url


def duckduckgo_lite_search(query, limit=10):
    page = fetch_url(DDG_LITE_URL.format(query=quote_plus(query)))
    results = []
    link_re = re.compile(r"""(?is)<a\b([^>]*)>(.*?)</a>""")
    snippet_re = re.compile(r"""(?is)<td\b[^>]*class=['"]result-snippet['"][^>]*>(.*?)</td>""")
    links = []
    for match in link_re.finditer(page):
        attrs = match.group(1)
        if not re.search(r"""class=['"][^'"]*\bresult-link\b""", attrs, re.I):
            continue
        href_match = re.search(r"""href=['"]([^'"]+)['"]""", attrs, re.I)
        if not href_match:
            continue
        links.append((href_match.group(1), match.group(2)))
    snippets = list(snippet_re.finditer(page))
    for index, (raw_link, raw_title) in enumerate(links):
        link = extract_redirect_url(raw_link)
        if not is_search_result_url(link):
            continue
        results.append(
            {
                "title": clean_text(raw_title),
                "snippet": clean_text(snippets[index].group(1)) if index < len(snippets) else "",
                "link": link,
            }
        )
        if len(results) >= limit:
            break
    return results


def bing_search(query, limit=10):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    page = fetch_url(BING_SEARCH_URL.format(query=quote_plus(query)))
    soup = BeautifulSoup(page, "html.parser")
    results = []
    for item in soup.select("li.b_algo"):
        link_tag = item.select_one("h2 a[href]")
        if not link_tag:
            continue
        link = extract_redirect_url(link_tag.get("href", ""))
        if not is_search_result_url(link):
            continue
        snippet_tag = item.select_one(".b_caption p")
        results.append(
            {
                "title": clean_text(link_tag.get_text(" ", strip=True)),
                "snippet": clean_text(snippet_tag.get_text(" ", strip=True) if snippet_tag else ""),
                "link": link,
            }
        )
        if len(results) >= limit:
            break
    return results


def google_cse_search(query, limit=10):
    key = os.environ.get("GOOGLE_API_KEY")
    cx = os.environ.get("GOOGLE_CSE_ID")
    if not key or not cx:
        return None
    limit = min(max(limit, 1), 10)
    response = fetch_url(GOOGLE_CSE_URL.format(key=key, cx=cx, query=quote_plus(query), num=limit))
    data = json.loads(response)
    return [
        {
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "link": item.get("link", ""),
        }
        for item in data.get("items", [])
    ]


def relevant_results(code, results):
    patterns = code_patterns(code)
    return [result for result in results
            if not is_generic_query_result(result, code)
            and any(pattern.search(unquote(result.get(field, '')))
                    for field in ('title', 'snippet', 'link') for pattern in patterns)]


def search_google(query, limit=10):
    errors = []
    code = query.rsplit(' uncensored', 1)[0]
    try:
        cse_results = google_cse_search(query, limit=limit)
        if cse_results is not None:
            relevant = relevant_results(code, cse_results)
            if relevant:
                return relevant, "google_custom_search"
    except Exception as exc:
        errors.append(f"google_custom_search: {exc}")

    for method_name, method in [
        ("google_html", google_html_search),
        ("bing", bing_search),
        ("duckduckgo_lite", duckduckgo_lite_search),
    ]:
        try:
            results = method(query, limit=limit)
        except Exception as exc:
            errors.append(f"{method_name}: {exc}")
            continue
        relevant = relevant_results(code, results)
        if relevant:
            return relevant, method_name

    if errors:
        raise RuntimeError("; ".join(errors))
    return [], "none"


def find_match(code, results):
    if not results:
        return "", {}
    patterns = code_patterns(code)
    for index, result in enumerate(results, start=1):
        if is_generic_query_result(result, code):
            continue
        for field in [result.get("title", ""), result.get("snippet", ""), result.get("link", "")]:
            if field_confirms_code_uncensored(field, patterns, code):
                return index, result
    return "", {}


def check_codes(codes, limit=10, delay=2.0, browser_searcher=None):
    rows = []
    for i, code in enumerate(codes, start=1):
        query = f"{code} uncensored"
        try:
            if browser_searcher:
                results = browser_searcher.search(query, limit=limit)
                search_method = getattr(browser_searcher, 'method', 'google_chrome')
            else:
                results, search_method = search_google(query, limit=limit)
            match_rank, match = find_match(code, results)
            if not relevant_results(code, results):
                matched = "unknown"
                error = "Search returned no relevant result blocks for the exact code."
            else:
                matched = "yes" if match else "no"
                error = ""
            rows.append(
                {
                    "code": code,
                    "query": query,
                    "matched": matched,
                    "match_rank": match_rank,
                    "match_title": match.get("title", ""),
                    "match_url": match.get("link", ""),
                    "match_snippet": match.get("snippet", ""),
                    "result_count_checked": len(results),
                    "search_method": search_method,
                    "google_url": GOOGLE_SEARCH_URL.format(query=quote_plus(query), num=limit),
                    "error": error,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "code": code,
                    "query": query,
                    "matched": "error",
                    "match_rank": "",
                    "match_title": "",
                    "match_url": "",
                    "match_snippet": "",
                    "result_count_checked": 0,
                    "search_method": "",
                    "google_url": GOOGLE_SEARCH_URL.format(query=quote_plus(query), num=limit),
                    "error": str(exc),
                }
            )
        print(f"{i}/{len(codes)} {code}: {rows[-1]['matched']}")
        if i < len(codes) and delay > 0:
            time.sleep(delay)
    return rows


def main():
    parser = argparse.ArgumentParser(
        description='Search Google for "<code> uncensored" and mark whether results contain the code.'
    )
    parser.add_argument("codes", nargs="*", help="Codes. Comma-separated input is accepted.")
    parser.add_argument("--codes-file", help="Text file containing codes, one per line or comma-separated")
    parser.add_argument("-o", "--output", default="google_uncensored_check.csv", help="Output CSV path")
    parser.add_argument("--limit", type=int, default=10, help="Google results to check per code")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds to wait between searches")
    parser.add_argument(
        "--browser",
        choices=["auto", "none", "chrome", "safari"],
        default="auto",
        help="auto/chrome use normal Chrome on macOS; none uses HTTP search providers",
    )
    parser.add_argument("--browser-wait", type=float, default=4.0, help="Seconds to wait after each Chrome search")
    parser.add_argument("--chrome-path", help="Path to Chrome or Chromium executable")
    args = parser.parse_args()

    codes = split_codes(args.codes)
    if args.codes_file:
        codes.extend(split_codes(Path(args.codes_file).read_text(encoding="utf-8").splitlines()))
    if not codes:
        parser.error("provide at least one code")

    if args.browser == "none":
        rows = check_codes(codes, limit=args.limit, delay=args.delay)
    elif args.browser == "safari":
        with SafariGoogleSearcher(wait_seconds=args.browser_wait) as searcher:
            rows = check_codes(codes, limit=args.limit, delay=args.delay, browser_searcher=searcher)
    elif sys.platform == 'darwin' and not args.chrome_path:
        with ExistingChromeGoogleSearcher(wait_seconds=args.browser_wait) as searcher:
            rows = check_codes(codes, limit=args.limit, delay=args.delay, browser_searcher=searcher)
    else:
        try:
            with ChromeGoogleSearcher(chrome_path=args.chrome_path, wait_seconds=args.browser_wait) as searcher:
                rows = check_codes(codes, limit=args.limit, delay=args.delay, browser_searcher=searcher)
        except Exception as exc:
            if args.browser == "chrome":
                raise
            print(f"Chrome browser search failed: {exc}")
            print("Trying Safari browser search...")
            with SafariGoogleSearcher(wait_seconds=args.browser_wait) as searcher:
                rows = check_codes(codes, limit=args.limit, delay=args.delay, browser_searcher=searcher)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
