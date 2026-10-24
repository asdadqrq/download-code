#!/usr/bin/env python3
"""Search codes under a specified 115 directory using netdisk MCP search_115.

Requires Python 3.11+, or `pip install tomli` on older Python versions.
No browser or browser automation dependencies are used.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

CODE_PREFIX = r"[A-Z0-9]*[A-Z][A-Z0-9]*"
CODE_RE = re.compile(rf"^({CODE_PREFIX})[-_ ]*0*(\d+)$", re.I)
TOKEN_RE = re.compile(rf"(?<![A-Z0-9])({CODE_PREFIX})[-_ ]*0*(\d+)(?!\d)", re.I)
UNCENSORED_SUFFIX_RE = re.compile(
    r"^[\s._-]*(?:(?:4K|8K|2160P|1080P|720P)[\s._-]+)*"
    r"(?:UNCENSORED|UC|U)(?![A-Z0-9])", re.I,
)
FIELDS = ["code", "exists", "uncensored", "result_count", "matched_names",
          "uncensored_names", "checked_at", "error"]


def parse_codes(values):
    codes = []
    for value in values:
        for item in re.split(r"[,，\s]+", value.strip()):
            if not item:
                continue
            code = item.upper()
            if not CODE_RE.fullmatch(code):
                print(f"Warning: skipped invalid input: {item}", file=sys.stderr)
            elif code not in codes:
                codes.append(code)
    if not codes:
        raise ValueError("No valid codes supplied")
    return codes


def canonical_code(value):
    match = CODE_RE.fullmatch(value.strip())
    return (match.group(1).upper(), int(match.group(2))) if match else None


def filename_matches_code(filename, code):
    wanted = canonical_code(code)
    return wanted is not None and any(
        (m.group(1).upper(), int(m.group(2))) == wanted
        for m in TOKEN_RE.finditer(filename)
    )


def filename_is_uncensored(filename, code):
    wanted = canonical_code(code)
    return wanted is not None and any(
        (m.group(1).upper(), int(m.group(2))) == wanted
        and UNCENSORED_SUFFIX_RE.match(filename[m.end():])
        for m in TOKEN_RE.finditer(filename)
    )


def load_server_config(path):
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise RuntimeError("Use Python 3.11+ or install tomli: python3 -m pip install tomli") from None
    with path.open("rb") as handle:
        config = tomllib.load(handle).get("mcp_servers", {}).get("netdisk")
    if not config or not config.get("command"):
        raise RuntimeError("No stdio mcp_servers.netdisk entry found in the config")
    return config


class NetdiskMCP:
    """Small JSON-RPC stdio client; credentials stay in the child environment."""

    def __init__(self, config, timeout=60):
        self.timeout = timeout
        self.next_id = 0
        self.messages = queue.Queue()
        env = {**os.environ, **config.get("env", {})}
        for key in config.get("env_vars", []):
            if key in os.environ:
                env[key] = os.environ[key]
        # npx needs node on PATH even when the configured command is absolute.
        command = config["command"]
        if Path(command).is_absolute():
            env["PATH"] = str(Path(command).parent) + os.pathsep + env.get("PATH", "")
        self.process = subprocess.Popen(
            [command, *config.get("args", [])], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "check-115-exists", "version": "1.0"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def _send(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"Netdisk MCP timed out during {method}") from None
            if message is None:
                raise RuntimeError("Netdisk MCP server exited; check its configuration")
            if "method" in message and "id" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32601, "message": "Client method not supported",
                }})
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise RuntimeError(f"Netdisk MCP rejected {method}")
            return message["result"]

    def search_115(self, code, directory, offset=0, limit=100):
        result = self.request("tools/call", {
            "name": "search_115", "arguments": {
                "query": code, "path": directory, "offset": offset, "limit": limit,
            },
        })
        if result.get("isError"):
            raise RuntimeError("Netdisk MCP search_115 failed; check the 115 cookie or folder access")
        text = "\n".join(item.get("text", "") for item in result.get("content", [])
                         if item.get("type") == "text")
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("Unexpected search_115 response")
        return data

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()


def make_rows(codes, names, errors):
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for code in codes:
        matched = [name for name in names if filename_matches_code(name, code)]
        uncensored = [name for name in matched if filename_is_uncensored(name, code)]
        rows.append({
            "code": code,
            "exists": "yes" if matched else "unknown" if errors else "no",
            "uncensored": "yes" if uncensored else "unknown" if errors else "no",
            "result_count": len(matched),
            "matched_names": " | ".join(matched),
            "uncensored_names": " | ".join(uncensored),
            "checked_at": checked_at,
            "error": " | ".join(errors),
        })
    return rows


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def check_code(client, code, directory, page_size=100, max_pages=100, delay=0.2):
    names, seen_ids, errors = set(), set(), []
    offset, total = 0, None
    try:
        for page_number in range(max_pages):
            page = client.search_115(code, directory, offset, page_size)
            # Reuse the resolved directory ID on subsequent pages.
            directory = page["cid"]
            if page["offset"] != offset:
                raise RuntimeError("Search returned an unexpected page offset")
            if total is not None and page["total"] != total:
                raise RuntimeError("Search results changed during pagination; retry this code")
            total = page["total"]
            for item in page["items"]:
                key = (item["type"], item["id"])
                if key in seen_ids:
                    raise RuntimeError("Search repeated results during pagination")
                seen_ids.add(key)
                names.add(item["name"])
            if not page["has_more"]:
                break
            next_offset = page["next_offset"]
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise RuntimeError("Search pagination did not advance")
            offset = next_offset
            time.sleep(delay)
        else:
            errors.append("Search page limit reached; increase --max-pages")
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    row = make_rows([code], sorted(names), errors)[0]
    row["result_count"] = total if total is not None else ""
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", help="Codes separated by commas or spaces")
    parser.add_argument("-o", "--output", type=Path, default=Path(__file__).with_name("115_exists_mcp.csv"))
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--path", help="115 directory path or folder ID; prompts if omitted. Searches only this directory and its subfolders")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum search pages per code")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 1000 or args.max_pages < 1 or args.delay < 0 or args.timeout <= 0:
        parser.error("Invalid page size, page limit, delay, or timeout")
    try:
        directory = (args.path if args.path is not None else input(
            "Enter 115 directory path or folder ID: "
        )).strip()
        if not directory:
            raise ValueError("Directory is required; specify --path or enter a directory")
        if not directory.startswith("/") and not directory.isdigit():
            directory = "/" + directory
        codes = parse_codes(args.codes or [input("Enter IDs (comma-separated): ")])
        print(f"Searching only: {directory} (including subfolders)", flush=True)
        client = NetdiskMCP(load_server_config(args.config.expanduser()), args.timeout)
        rows = []
        output = args.output.expanduser().resolve()
        try:
            tools = client.request("tools/list", {}).get("tools", [])
            if not any(tool.get("name") == "search_115" for tool in tools):
                raise RuntimeError("The configured netdisk server has no search_115 tool; configure the local enhanced server")
            for index, code in enumerate(codes):
                if index:
                    time.sleep(args.delay)
                print(f"[{index + 1}/{len(codes)}] Searching {code}...", flush=True)
                row = check_code(client, code, directory, args.page_size, args.max_pages, args.delay)
                rows.append(row)
                write_csv(output, rows)
                print(f"{code}: exists={row['exists']}, uncensored={row['uncensored']}")
                if row["error"]:
                    print(f"  {row['error']}", file=sys.stderr)
        finally:
            client.close()
        print(f"Saved: {output}")
    except (RuntimeError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
