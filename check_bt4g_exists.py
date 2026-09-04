#!/usr/bin/env python3
"""Batch-check whether BT4G search result pages contain any results."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlsplit, parse_qs
from urllib.request import urlopen

try:
    import websocket
except ImportError:
    websocket = None


BASE_URL = "https://bt4gprx.com"
DEFAULT_PROFILE = Path(__file__).with_name(".chrome-bt4g-profile")
DEFAULT_OUTPUT = Path(__file__).with_name("bt4g_exists_check.csv")
QUERY_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$", re.IGNORECASE)


class VerificationRequired(RuntimeError):
    pass


def parse_queries(values: list[str]) -> list[str]:
    queries: list[str] = []
    for value in values:
        for item in re.split(r"[,\s]+", value.strip()):
            if not item:
                continue
            query = item.upper()
            if not QUERY_RE.fullmatch(query):
                raise SystemExit(f"Invalid search code: {item}")
            if query not in queries:
                queries.append(query)
    if not queries:
        raise SystemExit("No search codes supplied.")
    return queries


class CdpClient:
    def __init__(self, ws_url: str, origin: str):
        if websocket is None:
            raise RuntimeError(
                "Dedicated mode requires websocket-client. Install it with: "
                "/opt/anaconda3/bin/python -m pip install websocket-client"
            )
        self.ws = websocket.create_connection(ws_url, timeout=15, origin=origin)
        self.next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self.ws.send(
            json.dumps({"id": request_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools error: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise RuntimeError(value.get("description", "JavaScript evaluation failed"))
        return value.get("value")

    def close(self) -> None:
        self.ws.close()


def chrome_path() -> Path:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("Google Chrome was not found in /Applications.")


def read_json(url: str):
    with urlopen(url, timeout=3) as response:
        return json.load(response)


def wait_for_debug_port(profile_dir: Path, timeout: float = 20) -> int:
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return int(port_file.read_text(encoding="utf-8").splitlines()[0])
        except (FileNotFoundError, IndexError, ValueError):
            time.sleep(0.2)
    raise RuntimeError("Chrome did not start its debugging connection.")


def find_bt4g_target(port: int, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = read_json(f"http://127.0.0.1:{port}/json/list")
            pages = [target for target in targets if target.get("type") == "page"]
            for target in pages:
                if "bt4gprx.com" in target.get("url", ""):
                    return target
            if pages:
                return pages[0]
        except (URLError, TimeoutError):
            pass
        time.sleep(0.3)
    raise RuntimeError("Could not find the Chrome page.")


def open_browser(profile_dir: Path) -> tuple[subprocess.Popen, CdpClient]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(chrome_path()),
            f"--user-data-dir={profile_dir}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            BASE_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = wait_for_debug_port(profile_dir)
    target = find_bt4g_target(port)
    client = CdpClient(target["webSocketDebuggerUrl"], f"http://127.0.0.1:{port}")
    client.call("Runtime.enable")
    client.call("Page.enable")
    return process, client


def applescript_string(value: str) -> str:
    return json.dumps(value)


def run_applescript(lines: list[str]) -> str:
    command = ["osascript"]
    for line in lines:
        command.extend(["-e", line])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip()
        if "Executing JavaScript through AppleScript is turned off" in error:
            raise RuntimeError(
                "In Chrome, enable View > Developer > "
                "Allow JavaScript from Apple Events, then rerun the script."
            )
        raise RuntimeError(error or "Chrome AppleScript command failed")
    return result.stdout.strip()


def open_existing_chrome() -> int:
    output = run_applescript(
        [
            'tell application "Google Chrome"',
            "activate",
            "set searchWindow to make new window",
            f"set URL of active tab of searchWindow to {applescript_string(BASE_URL)}",
            "return id of searchWindow",
            "end tell",
        ]
    )
    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(f"Chrome returned an invalid window id: {output}") from exc


def close_existing_chrome(window_id: int) -> None:
    run_applescript(
        [
            'tell application "Google Chrome"',
            f"if exists window id {window_id} then close window id {window_id}",
            "end tell",
        ]
    )


def navigate_existing_chrome(window_id: int, url: str) -> None:
    run_applescript(
        [
            'tell application "Google Chrome"',
            f"set URL of active tab of window id {window_id} to {applescript_string(url)}",
            "end tell",
        ]
    )


def evaluate_existing_chrome(window_id: int, script: str):
    output = run_applescript(
        [
            'tell application "Google Chrome"',
            f"return execute active tab of window id {window_id} "
            f"javascript {applescript_string(script)}",
            "end tell",
        ]
    )
    return json.loads(output)


class CurrentChromeClient:
    """Keep using the selected tab even if the user later switches tabs."""

    def __init__(self):
        output = run_applescript([
            'tell application "Google Chrome"',
            'if (count of windows) = 0 then error "Open BT4G in Chrome first."',
            'set w to front window',
            'set t to active tab of w',
            'return (id of w as text) & linefeed & (id of t as text) & linefeed & (URL of t)',
            'end tell',
        ])
        window, tab, url = output.split("\n", 2)
        if urlsplit(url).hostname != urlsplit(BASE_URL).hostname:
            raise RuntimeError(
                "Select the verified bt4gprx.com tab in Chrome, then rerun. "
                "No new tab or window will be opened."
            )
        self.window_id = int(window)
        self.tab_id = int(tab)

    def _run(self, command: str) -> str:
        return run_applescript([
            'tell application "Google Chrome"',
            f'set targetTab to tab id {self.tab_id} of window id {self.window_id}',
            command,
            'end tell',
        ])

    def evaluate(self, expression: str):
        script = f"JSON.stringify({expression})"
        return json.loads(self._run(
            f"return execute targetTab javascript {applescript_string(script)}"
        ))

    def call(self, method: str, params: dict):
        if method != "Page.navigate":
            raise ValueError(f"Unsupported operation: {method}")
        self._run(f"set URL of targetTab to {applescript_string(params['url'])}")

    def close(self):
        pass  # The user's tab must remain open.


def attach_running_browser(query: str, debug_port: int | None = None) -> CdpClient:
    """Discover only local debugging endpoints created by these project scripts."""
    ports = set()
    if debug_port is not None:
        ports.add(debug_port)
    else:
        for profile in (DEFAULT_PROFILE, Path(__file__).with_name('.chrome-115-profile')):
            try:
                port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
                if 0 < port < 65536:
                    ports.add(port)
            except (OSError, ValueError, IndexError):
                continue
    candidates = []
    for port in sorted(ports):
        try:
            targets = read_json(f'http://127.0.0.1:{port}/json/list')
        except (URLError, TimeoutError, OSError, ValueError):
            continue
        for target in targets:
            parsed = urlsplit(target.get('url', ''))
            if (target.get('type') == 'page'
                    and parsed.hostname == urlsplit(BASE_URL).hostname
                    and target.get('webSocketDebuggerUrl')):
                exact = parse_qs(parsed.query).get('q') == [query]
                challenge = 'just a moment' in target.get('title', '').lower()
                candidates.append((not exact, challenge, port, target))
    if not candidates:
        raise RuntimeError(
            'No open BT4G tab found in the running script-created Chrome instances. '
            'Open BT4G in normal Chrome, or specify --debug-port for your existing debugging session.'
        )
    candidates.sort(key=lambda item: item[:3])
    _, _, port, target = candidates[0]
    ws_url = target['webSocketDebuggerUrl']
    if urlsplit(ws_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('Refusing a non-local Chrome debugging endpoint')
    client = CdpClient(ws_url, f'http://127.0.0.1:{port}')
    print(f"Attached to an already-open BT4G tab on local port {port}: {target['url']}")
    return client


RESULT_STATE_SCRIPT = r"""
((expectedUrl) => {
  const text = document.body ? document.body.innerText : '';
  const found = text.match(/Found\s+([\d,]+)\s+items?\s+for/i);
  const empty = /did not match any documents/i.test(text);
  const blocked = /just a moment|verify you are human|attention required|captcha/i.test(
    `${document.title}\n${text}`
  );
  return {
    correctPage: location.href === expectedUrl,
    ready: document.readyState === 'complete' || document.readyState === 'interactive',
    settled: Boolean(found || empty || blocked),
    count: found ? Number(found[1].replaceAll(',', '')) : 0,
    blocked
  };
})(EXPECTED_URL)
"""


def wait_for_result_cdp(client: CdpClient, expected_url: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    previous = None
    stable_reads = 0
    blocked_since = None
    script = RESULT_STATE_SCRIPT.replace("EXPECTED_URL", json.dumps(expected_url))
    while time.time() < deadline:
        state = client.evaluate(script)
        signature = (state.get("settled"), state.get("count"), state.get("blocked"))
        if state.get("correctPage") and state.get("ready") and state.get("settled"):
            if state.get("blocked"):
                blocked_since = blocked_since or time.time()
                if time.time() - blocked_since >= 8:
                    return state
                time.sleep(0.4)
                continue
            blocked_since = None
            stable_reads = stable_reads + 1 if signature == previous else 1
            if stable_reads >= 2:
                return state
        previous = signature
        time.sleep(0.4)
    raise RuntimeError("Search result page did not finish loading")


def wait_for_result_existing(
    window_id: int, expected_url: str, timeout: float = 20
) -> dict:
    deadline = time.time() + timeout
    previous = None
    stable_reads = 0
    blocked_since = None
    script = RESULT_STATE_SCRIPT.replace("EXPECTED_URL", json.dumps(expected_url))
    while time.time() < deadline:
        state = evaluate_existing_chrome(window_id, f"JSON.stringify({script})")
        signature = (state.get("settled"), state.get("count"), state.get("blocked"))
        if state.get("correctPage") and state.get("ready") and state.get("settled"):
            if state.get("blocked"):
                blocked_since = blocked_since or time.time()
                if time.time() - blocked_since >= 8:
                    return state
                time.sleep(0.4)
                continue
            blocked_since = None
            stable_reads = stable_reads + 1 if signature == previous else 1
            if stable_reads >= 2:
                return state
        previous = signature
        time.sleep(0.4)
    raise RuntimeError("Search result page did not finish loading")


def result_row(query: str, search_url: str, state: dict) -> dict:
    if state.get("blocked"):
        raise VerificationRequired("The site displayed a browser verification page")
    result_count = int(state.get("count", 0))
    return {
        "code": query,
        "exists": "yes" if result_count > 0 else "no",
        "result_count": result_count,
        "search_url": search_url,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": "",
    }


def check_query_cdp(client: CdpClient, query: str, resume: bool = False) -> dict:
    search_url = f"{BASE_URL}/search?q={quote(query)}"
    if not resume and client.evaluate("location.href") != search_url:
        client.call("Page.navigate", {"url": search_url})
    return result_row(query, search_url, wait_for_result_cdp(client, search_url))


def check_query_existing(window_id: int, query: str, resume: bool = False) -> dict:
    search_url = f"{BASE_URL}/search?q={quote(query)}"
    if not resume:
        current_url = evaluate_existing_chrome(window_id, "JSON.stringify(location.href)")
        if current_url != search_url:
            navigate_existing_chrome(window_id, search_url)
    return result_row(
        query, search_url, wait_for_result_existing(window_id, search_url)
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["code", "exists", "result_count", "search_url", "checked_at", "error"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch-check whether BT4G search pages contain results."
    )
    parser.add_argument("codes", nargs="+", help="Codes separated by commas or spaces")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--browser",
        choices=["current", "existing", "dedicated"],
        default="current",
        help="Use the verified foreground Chrome tab (default), a new normal window, or a dedicated profile",
    )
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--debug-port", type=int, help="Attach to an already-running local Chrome debugging port")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between searches")
    parser.add_argument(
        "--no-verification-prompt",
        action="store_true",
        help="Return unknown instead of waiting for manual browser verification",
    )
    parser.add_argument(
        "--keep-browser", action="store_true", help="Leave the script-launched Chrome open"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.delay < 0.5:
        raise SystemExit("--delay must be at least 0.5 seconds.")
    if args.debug_port is not None and not 0 < args.debug_port < 65536:
        raise SystemExit("--debug-port must be between 1 and 65535")
    if args.debug_port is not None and args.browser != 'current':
        raise SystemExit("--debug-port requires --browser current")
    queries = parse_queries(args.codes)
    process = None
    client = None
    existing_window_id = None
    rows: list[dict] = []
    try:
        if args.browser == "current":
            print("Using the foreground BT4G tab in Chrome; no new window is opened.")
            try:
                if args.debug_port is not None:
                    client = attach_running_browser(queries[0], args.debug_port)
                else:
                    try:
                        client = CurrentChromeClient()
                    except RuntimeError as selection_error:
                        print(f"Normal Chrome tab selection failed: {selection_error}")
                        client = attach_running_browser(queries[0])
                client.evaluate("({ok:true})")
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
        elif args.browser == "existing":
            print("Opening a BT4G search window in your normal Chrome session...")
            existing_window_id = open_existing_chrome()
            try:
                evaluate_existing_chrome(existing_window_id, "JSON.stringify({ok:true})")
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
        else:
            print("Opening a separate Chrome profile for BT4G searches...")
            process, client = open_browser(args.profile_dir.expanduser().resolve())

        def run_check(current_query: str, resume: bool = False) -> dict:
            if args.browser == "existing":
                return check_query_existing(existing_window_id, current_query, resume=resume)
            return check_query_cdp(client, current_query, resume=resume)

        for index, query in enumerate(queries, start=1):
            if index > 1:
                time.sleep(args.delay)
            print(f"[{index}/{len(queries)}] Checking {query}...")
            try:
                row = run_check(query)
            except VerificationRequired as exc:
                if args.no_verification_prompt:
                    row = {
                        "code": query,
                        "exists": "unknown",
                        "result_count": "",
                        "search_url": f"{BASE_URL}/search?q={quote(query)}",
                        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "error": str(exc),
                    }
                else:
                    print("BT4G requires verification in the tab being checked.")
                    input("Complete it in that same tab, then press Enter to read it without reloading... ")
                    try:
                        row = run_check(query, resume=True)
                    except Exception as retry_exc:
                        row = {
                            "code": query,
                            "exists": "unknown",
                            "result_count": "",
                            "search_url": f"{BASE_URL}/search?q={quote(query)}",
                            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "error": str(retry_exc),
                        }
            except Exception as exc:  # Keep successful rows when one query fails.
                row = {
                    "code": query,
                    "exists": "unknown",
                    "result_count": "",
                    "search_url": f"{BASE_URL}/search?q={quote(query)}",
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "error": str(exc),
                }
            rows.append(row)

        output = args.output.expanduser().resolve()
        write_csv(output, rows)
        yes_count = sum(row["exists"] == "yes" for row in rows)
        print(f"Wrote {len(rows)} rows ({yes_count} found) to {output}")
    finally:
        if client is not None:
            client.close()
        if existing_window_id is not None and not args.keep_browser:
            close_existing_chrome(existing_window_id)
        if process is not None and not args.keep_browser:
            process.terminate()


if __name__ == "__main__":
    main()
