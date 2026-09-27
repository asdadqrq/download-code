#!/usr/bin/env python3
"""Check whether requested codes exist in the logged-in 115.com file search."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

try:
    import websocket
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: websocket-client\n"
        "Install it with: /opt/anaconda3/bin/python -m pip install websocket-client"
    ) from exc


START_URL = "https://115.com/storage/allfiles?cid=0&mode=wangpan"
DEFAULT_PROFILE = Path(__file__).with_name(".chrome-115-profile")
DEFAULT_OUTPUT = Path(__file__).with_name("115_exists_check.csv")
CODE_PREFIX = r"[A-Z0-9]*[A-Z][A-Z0-9]*"
CODE_RE = re.compile(rf"^({CODE_PREFIX})[-_ ]*0*(\d+)$", re.IGNORECASE)
TOKEN_RE = re.compile(
    rf"(?<![A-Z0-9])({CODE_PREFIX})[-_ ]*0*(\d+)(?!\d)", re.IGNORECASE
)
UNCENSORED_SUFFIX_RE = re.compile(
    # Allow resolution labels before the marker and Chinese annotations after it.
    r"^[\s._-]*(?:(?:4K|8K|2160P|1080P|720P)[\s._-]+)*"
    r"(?:UNCENSORED|UC|U)(?![A-Z0-9])", re.IGNORECASE
)


def parse_codes(values: list[str]) -> list[str]:
    codes: list[str] = []
    invalid: list[str] = []
    for value in values:
        for item in re.split(r"[,\s]+", value.strip()):
            if not item:
                continue
            code = item.upper().strip()
            if not CODE_RE.fullmatch(code):
                if item not in invalid:
                    invalid.append(item)
                continue
            if code not in codes:
                codes.append(code)
    if invalid:
        print(f"Warning: skipped invalid input: {', '.join(invalid)}", file=sys.stderr)
    if not codes:
        raise SystemExit("No valid codes supplied.")
    return codes


def canonical_code(value: str) -> tuple[str, int] | None:
    match = CODE_RE.fullmatch(value.strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def filename_matches_code(filename: str, code: str) -> bool:
    wanted = canonical_code(code)
    if wanted is None:
        return False
    return any(
        (match.group(1).upper(), int(match.group(2))) == wanted
        for match in TOKEN_RE.finditer(filename)
    )


def filename_is_uncensored(filename: str, code: str) -> bool:
    wanted = canonical_code(code)
    if wanted is None:
        return False
    for match in TOKEN_RE.finditer(filename):
        found = match.group(1).upper(), int(match.group(2))
        if found == wanted and UNCENSORED_SUFFIX_RE.match(filename[match.end() :]):
            return True
    return False


class CdpClient:
    def __init__(self, ws_url: str, origin: str):
        self.ws = websocket.create_connection(ws_url, timeout=10, origin=origin)
        self.next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
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


def find_115_target(port: int, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = read_json(f"http://127.0.0.1:{port}/json/list")
            pages = [item for item in targets if item.get("type") == "page"]
            for target in pages:
                if "115.com" in target.get("url", ""):
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
            START_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = wait_for_debug_port(profile_dir)
    target = find_115_target(port)
    client = CdpClient(target["webSocketDebuggerUrl"], f"http://127.0.0.1:{port}")
    client.call("Runtime.enable")
    return process, client


def select_115_tab(apps):
    candidates = []
    for app in apps:
        try:
            for window in app.windows():
                active_id = window.activeTab().id()
                for tab in window.tabs():
                    parsed = urlparse(tab.URL() or "")
                    if parsed.hostname not in {"115.com", "www.115.com"}:
                        continue
                    if parsed.path not in {"/", "/storage/allfiles"}:
                        continue
                    candidates.append((tab.id() != active_id, parsed.path != "/storage/allfiles", app, tab))
        except (AttributeError, RuntimeError):
            continue
    if not candidates:
        raise RuntimeError("Open your 115 file page in Chrome, then rerun the script.")
    candidates.sort(key=lambda item: item[:2])
    return candidates[0][2:]


class ExistingChromeClient:
    def __init__(self):
        try:
            import objc
            from AppKit import NSWorkspace
        except ImportError as exc:
            raise RuntimeError("Your Python needs PyObjC to read existing Chrome tabs.") from exc

        bundle = {}
        objc.loadBundle(
            "ScriptingBridge", bundle,
            bundle_path="/System/Library/Frameworks/ScriptingBridge.framework",
        )
        applications = [
            bundle["SBApplication"].applicationWithProcessIdentifier_(process.processIdentifier())
            for process in NSWorkspace.sharedWorkspace().runningApplications()
            if process.bundleIdentifier() == "com.google.Chrome"
        ]
        self.app, self.tab = select_115_tab(applications)

    def evaluate(self, expression: str):
        response = self.tab.executeJavascript_(f"JSON.stringify({expression})")
        if not response:
            raise RuntimeError(
                "Chrome could not read the 115 page. Enable View > Developer > "
                "Allow JavaScript from Apple Events."
            )
        return json.loads(response)

    def close(self):
        pass  # Keep the user's Chrome tab open.


READY_SCRIPT = r"""
(() => Boolean(
  location.hostname.endsWith('115.com') &&
  [...document.querySelectorAll('input')].some(el =>
    (el.placeholder || '').includes('搜索')
  )
))()
"""


def wait_until_ready(client: CdpClient, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.evaluate(READY_SCRIPT):
            return True
        time.sleep(0.5)
    return False


def submit_search(client: CdpClient, code: str) -> None:
    encoded = json.dumps(code)
    script = rf"""
(() => {{
  const input = [...document.querySelectorAll('input')].find(el =>
    (el.placeholder || '').includes('搜索')
  );
  if (!input) return {{ok: false, error: 'Search box not found'}};
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, 'value'
  ).set;
  input.focus();
  setter.call(input, {encoded});
  input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText'}}));
  input.dispatchEvent(new Event('change', {{bubbles: true}}));
  for (const type of ['keydown', 'keypress', 'keyup']) {{
    input.dispatchEvent(new KeyboardEvent(type, {{
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
    }}));
  }}
  return {{ok: true}};
}})()
"""
    result = client.evaluate(script)
    if not result or not result.get("ok"):
        raise RuntimeError((result or {}).get("error", "Could not submit search"))


def read_results(client: CdpClient, code: str, timeout: float = 12) -> dict:
    encoded = json.dumps(code)
    script = rf"""
(() => {{
  const input = [...document.querySelectorAll('input')].find(el =>
    (el.placeholder || '').includes('搜索')
  );
  const frame = [...document.querySelectorAll('iframe')].find(el => {{
    try {{
      return new URL(el.src).searchParams.get('search_value') === {encoded};
    }} catch {{ return false; }}
  }});
  if (frame) {{
    const doc = frame.contentDocument;
    const text = doc?.body?.innerText || '';
    const counts = text.match(/当前共\s*(\d+)\s*个文件[，,]\s*(\d+)\s*个文件夹/);
    const empty = /未找到|没有找到|暂无相关|无搜索结果/.test(text);
    return {{
      queryReady: Boolean(input && input.value === {encoded}),
      settled: Boolean(counts || empty),
      resultCount: counts ? Number(counts[1]) + Number(counts[2]) : 0,
      names: [...(doc?.querySelectorAll('li[rel="item"][title]') || [])]
        .map(el => el.getAttribute('title')).filter(Boolean)
    }};
  }}
  const bodyText = document.body.innerText || '';
  const found = bodyText.match(/找到\s+(\d+)\s+个结果/);
  const noResult = /未找到|没有找到|暂无相关|无搜索结果/.test(bodyText);
  const names = [];
  const add = value => {{
    value = (value || '').trim();
    if (value && value.length <= 300 && !names.includes(value)) names.push(value);
  }};
  for (const el of document.querySelectorAll('[title], img[alt]')) {{
    add(el.getAttribute('title'));
    add(el.getAttribute('alt'));
  }}
  for (const el of document.querySelectorAll('body *')) {{
    if (el.children.length === 0) add(el.textContent);
  }}
  return {{
    queryReady: Boolean(input && input.value === {encoded}),
    settled: Boolean(found || noResult),
    resultCount: found ? Number(found[1]) : 0,
    names
  }};
}})()
"""
    deadline = time.time() + timeout
    previous = None
    stable_reads = 0
    while time.time() < deadline:
        data = client.evaluate(script)
        signature = (data.get("queryReady"), data.get("resultCount"), tuple(data.get("names", [])))
        if data.get("queryReady") and data.get("settled"):
            stable_reads = stable_reads + 1 if signature == previous else 1
            if stable_reads >= 2:
                return data
        previous = signature
        time.sleep(0.5)
    raise RuntimeError("Search results did not finish loading")


def check_code(client: CdpClient, code: str) -> dict:
    submit_search(client, code)
    time.sleep(0.7)
    data = read_results(client, code)
    matched_names = [
        name for name in data.get("names", []) if filename_matches_code(name, code)
    ]
    matched_names = list(dict.fromkeys(matched_names))[:20]
    uncensored_names = [
        name for name in matched_names if filename_is_uncensored(name, code)
    ]
    return {
        "code": code,
        "exists": "yes" if matched_names else "no",
        "uncensored": "yes" if uncensored_names else "no",
        "result_count": data.get("resultCount", 0),
        "matched_names": " | ".join(matched_names),
        "uncensored_names": " | ".join(uncensored_names),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": "",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "code",
        "exists",
        "uncensored",
        "result_count",
        "matched_names",
        "uncensored_names",
        "checked_at",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search your logged-in 115.com storage for one or more codes."
    )
    parser.add_argument("codes", nargs="+", help="Codes separated by commas or spaces")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--browser", choices=["existing", "dedicated"], default="existing",
        help="Use your open Chrome 115 tab (default), or the separate persistent profile",
    )
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--keep-browser", action="store_true", help="Leave the script-launched Chrome open"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    codes = parse_codes(args.codes)
    process = None
    client = None
    rows: list[dict] = []
    try:
        if args.browser == "existing":
            print("Using your open 115 page in Chrome...")
            client = ExistingChromeClient()
            if not wait_until_ready(client, 8):
                raise RuntimeError("The open 115 tab has no ready file-search field.")
        else:
            print("Opening Chrome with the persistent 115 profile...")
            process, client = open_browser(args.profile_dir.expanduser().resolve())
            if not wait_until_ready(client, 8):
                print("Log in to 115.com in the opened Chrome window.")
                input("After the file page is visible, press Enter here to continue... ")
                if not wait_until_ready(client, 120):
                    raise RuntimeError("115.com file search did not become ready")

        for index, code in enumerate(codes, start=1):
            print(f"[{index}/{len(codes)}] Checking {code}...")
            try:
                row = check_code(client, code)
            except Exception as exc:  # Keep the remaining codes useful.
                row = {
                    "code": code,
                    "exists": "unknown",
                    "uncensored": "unknown",
                    "result_count": "",
                    "matched_names": "",
                    "uncensored_names": "",
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "error": str(exc),
                }
            rows.append(row)

        output = args.output.expanduser().resolve()
        write_csv(output, rows)
        yes_count = sum(row["exists"] == "yes" for row in rows)
        uncensored_count = sum(row["uncensored"] == "yes" for row in rows)
        print(
            f"Wrote {len(rows)} rows ({yes_count} found, "
            f"{uncensored_count} uncensored) to {output}"
        )
    finally:
        if client is not None:
            client.close()
        if process is not None and not args.keep_browser:
            process.terminate()


if __name__ == "__main__":
    main()
