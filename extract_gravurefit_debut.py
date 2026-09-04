#!/usr/bin/env python3
import argparse
import csv
import html
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MISSAV_SEARCH_BASE = "https://missav.ws/en/search/"
DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class FetchBlockedError(RuntimeError):
    pass


class BrowserRenderError(RuntimeError):
    pass


def strip_tags(value):
    value = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", "", value)
    value = re.sub(r"(?is)<br\s*/?>", "\n", value)
    value = re.sub(r"(?is)</(?:p|div|tr|li|h[1-6])>", "\n", value)
    value = re.sub(r"(?is)<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def fetch_html(url):
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")
    except HTTPError as exc:
        if exc.code in {403, 429, 503}:
            raise FetchBlockedError(f"Direct fetch was blocked by the site: HTTP {exc.code}") from exc
        raise
    except URLError as exc:
        raise FetchBlockedError(f"Direct fetch failed: {exc.reason}") from exc


def read_json_url(url, timeout=2):
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_debugger(port, timeout=15):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return read_json_url(f"http://127.0.0.1:{port}/json/version")
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise BrowserRenderError(f"Chrome remote debugger did not start: {last_error}")


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cdp_call(ws, method, params=None, call_id=None):
    call_id = call_id or int(time.time() * 1000)
    ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == call_id:
            if "error" in message:
                raise BrowserRenderError(f"Chrome DevTools error in {method}: {message['error']}")
            return message.get("result", {})


def evaluate_js(ws, expression, call_id):
    result = cdp_call(
        ws,
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
        call_id,
    )
    return result.get("result", {}).get("value", "")


def render_html_with_chrome(url, wait_seconds=15, chrome_path=None, port=None):
    try:
        import websocket
    except ImportError as exc:
        raise BrowserRenderError(
            "The websocket-client package is required for browser rendering. "
            "Install it with: /opt/anaconda3/bin/python -m pip install websocket-client"
        ) from exc

    chrome_path = chrome_path or DEFAULT_CHROME_PATH
    if not Path(chrome_path).exists():
        discovered = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        if discovered:
            chrome_path = discovered
        else:
            raise BrowserRenderError(f"Chrome was not found at {chrome_path}")

    user_data_dir = tempfile.mkdtemp(prefix="gravurefit-chrome-")
    port = port or find_free_port()
    process = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_debugger(port)
        tabs = read_json_url(f"http://127.0.0.1:{port}/json/list")
        target = next((tab for tab in tabs if tab.get("url") == url), tabs[0] if tabs else None)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise BrowserRenderError("Could not find the Chrome tab to read.")

        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=5)
        try:
            cdp_call(ws, "Runtime.enable", call_id=1)
            time.sleep(max(0, wait_seconds))
            page_html = evaluate_js(ws, "document.documentElement.outerHTML", call_id=2)
        finally:
            ws.close()
        return page_html
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)


def render_html_with_safari(url, wait_seconds=15):
    script = """
on run argv
    set targetUrl to item 1 of argv
    set waitSeconds to item 2 of argv as integer
    tell application "Safari"
        activate
        open location targetUrl
        delay waitSeconds
        set pageSource to do JavaScript "document.documentElement.outerHTML" in front document
    end tell
    return pageSource
end run
"""
    result = subprocess.run(
        ["osascript", "-", url, str(max(0, wait_seconds))],
        input=script,
        text=True,
        capture_output=True,
        timeout=max(30, wait_seconds + 15),
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise BrowserRenderError(
            "Safari could not return rendered HTML. "
            "In Safari, enable Develop > Allow JavaScript from Apple Events, then retry. "
            f"Details: {message}"
        )
    return result.stdout


def render_html_with_browser(url, browser_name="auto", wait_seconds=15, chrome_path=None):
    errors = []
    if browser_name in {"auto", "chrome"}:
        try:
            return render_html_with_chrome(url, wait_seconds=wait_seconds, chrome_path=chrome_path)
        except BrowserRenderError as exc:
            errors.append(f"Chrome: {exc}")
            if browser_name == "chrome":
                raise

    if browser_name in {"auto", "safari"}:
        try:
            return render_html_with_safari(url, wait_seconds=wait_seconds)
        except BrowserRenderError as exc:
            errors.append(f"Safari: {exc}")
            if browser_name == "safari":
                raise

    raise BrowserRenderError("; ".join(errors) or f"Unknown browser: {browser_name}")


def default_output_path(url):
    match = re.search(r"/women/debut-(\d{6})/?", url)
    if match:
        return f"gravurefit_debut_{match.group(1)}.csv"
    slug = re.sub(r"^https?://", "", url).strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_").lower()
    return f"{slug or 'gravurefit_debut'}.csv"


def code_from(value):
    match = re.search(r"\b([a-z0-9]+-[a-z0-9]+)\b", value, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def extract_rows(page_html):
    h2_pattern = re.compile(r"(?is)<h2\b[^>]*>(.*?)</h2>")
    headings = list(h2_pattern.finditer(page_html))
    rows = []

    for index, match in enumerate(headings):
        section_name = strip_tags(match.group(1))
        if not section_name or "デビュー" in section_name:
            continue

        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(page_html)
        section_html = page_html[start:end]
        section_text = strip_tags(section_html)
        if "女優名" not in section_text and "デビュー作" not in section_text:
            continue

        actress_name = section_name
        name_match = re.search(r"女優名\s*([^\n\t（(]+)", section_text)
        if name_match:
            actress_name = name_match.group(1).strip()

        watch_match = re.search(
            r'(?is)<a\b[^>]*href=["\']([^"\']*/watch/([^/"\']+)/?[^"\']*)["\'][^>]*>(.*?)</a>',
            section_html,
        )
        if watch_match:
            source = watch_match.group(1)
            debut_title = strip_tags(watch_match.group(3))
            code = code_from(source) or code_from(debut_title)
        else:
            code = code_from(section_text)

        rows.append(
            {
                "actress_name": actress_name,
                "code": code,
                "missav_url": MISSAV_SEARCH_BASE + code.lower() if code else "",
            }
        )

    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Extract actress names and debut codes from a GravureFit debut page."
    )
    parser.add_argument("page_url", nargs="?", help="GravureFit page URL")
    parser.add_argument("--url", help="GravureFit page URL")
    parser.add_argument("--html", help="Read from a saved HTML file instead of fetching the URL")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser automatically if direct fetch is blocked",
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "chrome", "safari"],
        default="auto",
        help="Browser to use when direct fetch is blocked",
    )
    parser.add_argument(
        "--browser-wait",
        type=int,
        default=15,
        help="Seconds to wait after the browser opens before reading rendered HTML",
    )
    parser.add_argument("--chrome-path", help="Path to Chrome or Chromium executable")
    parser.add_argument("-o", "--output", help="Output CSV path")
    args = parser.parse_args()
    page_url = args.page_url or args.url
    if not page_url:
        parser.error("provide a GravureFit page URL, for example https://www.gravurefit.com/women/debut-202402/")
    output_path = args.output or default_output_path(page_url)

    if args.html:
        page_html = Path(args.html).read_text(encoding="utf-8", errors="replace")
        rows = extract_rows(page_html)
    else:
        try:
            page_html = fetch_html(page_url)
            if "Cloudflare" in page_html and "Attention Required" in page_html:
                raise FetchBlockedError("Cloudflare returned a block page.")
            rows = extract_rows(page_html)
        except FetchBlockedError as exc:
            if args.no_browser:
                sys.exit(
                    f"{exc}\n"
                    "Save the loaded page HTML from your browser and rerun:\n"
                    f"  python {Path(__file__).name} {page_url} --html page.html"
                )
            print(f"{exc} Opening {args.browser} browser to read the rendered page...")
            try:
                page_html = render_html_with_browser(
                    page_url,
                    browser_name=args.browser,
                    wait_seconds=args.browser_wait,
                    chrome_path=args.chrome_path,
                )
                rows = extract_rows(page_html)
            except BrowserRenderError as render_exc:
                sys.exit(
                    f"{render_exc}\n"
                    "Save the loaded page HTML from your browser and rerun:\n"
                    f"  python {Path(__file__).name} {page_url} --html page.html"
                )

    if not rows:
        sys.exit("No rows extracted. Check that the page HTML contains the loaded article content.")

    with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["actress_name", "code", "missav_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
