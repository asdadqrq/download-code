#!/usr/bin/env python3
"""Convert comma-separated BT4G detail links to magnet URIs in one Chrome tab."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from check_bt4g_exists import BASE_URL, CurrentChromeClient
from check_bt4g_titles import read_torrent_hash


def parse_links(values: list[str]) -> list[str]:
    links = []
    for value in values:
        for item in re.split(r"[,，\s]+", value.strip()):
            if not item:
                continue
            parsed = urlsplit(item)
            if (parsed.scheme != "https" or parsed.netloc.lower() != urlsplit(BASE_URL).netloc
                    or not re.fullmatch(r"/(?:magnet|hash)/[A-Za-z0-9_-]+/?", parsed.path)):
                raise ValueError(f"Expected a https://bt4gprx.com/magnet/... link: {item}")
            link = BASE_URL + parsed.path.rstrip("/")
            if link not in links:
                links.append(link)
    if not links:
        raise ValueError("No BT4G links supplied")
    return links


def write_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_url", "torrent_hash", "error"])
        writer.writeheader()
        writer.writerows(rows)


def extract_links(client, links: list[str], output: Path, delay: float = 1.5,
                  timeout: float = 30) -> list[dict]:
    rows = []
    for index, link in enumerate(links, 1):
        if index > 1:
            time.sleep(delay)
        row = {"source_url": link, "torrent_hash": "", "error": ""}
        try:
            digest = read_torrent_hash(client, link, timeout=timeout)
            row["torrent_hash"] = "magnet:?xt=urn:btih:" + digest
            print(f"[{index}/{len(links)}] {row['torrent_hash']}", flush=True)
        except (RuntimeError, OSError, ValueError, EOFError) as exc:
            row["error"] = str(exc)
            print(f"[{index}/{len(links)}] Failed: {link}: {exc}", flush=True)
        rows.append(row)
        # Save each result so an interrupted batch retains completed work.
        write_results(output, rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("links", nargs="*", help="BT4G URLs separated by commas or spaces")
    parser.add_argument("-o", "--output", type=Path, default=Path("bt4g_link_magnets.csv"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.delay < 0.5 or args.timeout <= 0:
        parser.error("--delay must be at least 0.5 and --timeout must be positive")
    try:
        links = parse_links(args.links or [input("Enter BT4G links (comma-separated): ")])
    except ValueError as exc:
        parser.error(str(exc))
    client = CurrentChromeClient()
    try:
        rows = extract_links(client, links, args.output.expanduser().resolve(), args.delay, args.timeout)
    finally:
        client.close()
    print(f"Saved {sum(bool(row['torrent_hash']) for row in rows)}/{len(rows)} magnets: {args.output.expanduser().resolve()}")


if __name__ == "__main__":
    main()
