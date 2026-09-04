#!/usr/bin/env python3
"""Extract all paginated Tangxin titles and check their 0mag result counts."""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: beautifulsoup4\n"
        "Install it with: /opt/anaconda3/bin/python -m pip install beautifulsoup4"
    ) from exc


DEFAULT_SOURCE = (
    "https://tangxinvlog.app/zh-tw/a/"
    "%E5%B0%8F%E6%AC%A3%E5%A5%88/"
)
DEFAULT_OUTPUT = Path(__file__).with_name("tangxin_0mag_search.csv")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
RESULT_COUNT_RE = re.compile(r"([\d,]+)\s+results?\b", re.IGNORECASE)


def ascii_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, quote(parts.path, safe="/%"), parts.query, parts.fragment)
    )


def fetch_html(url: str, retries: int = 3) -> str:
    url = ascii_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def extract_catalog(start_url: str) -> list[dict]:
    rows: list[dict] = []
    seen_pages: set[str] = set()
    seen_videos: set[str] = set()
    page_url: str | None = start_url
    page_number = 1

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        soup = BeautifulSoup(fetch_html(page_url), "html.parser")
        links = soup.select("main article h3 a[href]")
        if not links:
            raise RuntimeError(f"No video titles found on {page_url}")

        for link in links:
            title = " ".join(link.get_text(" ", strip=True).split())
            source_url = urljoin(page_url, link["href"])
            if not title or source_url in seen_videos:
                continue
            seen_videos.add(source_url)
            rows.append(
                {
                    "page": page_number,
                    "title": title,
                    "source_url": source_url,
                }
            )

        next_link = next(
            (
                link
                for link in soup.select('nav[aria-label="pagination"] a[href]')
                if "下一頁" in link.get_text(" ", strip=True)
                or "next" in link.get_text(" ", strip=True).lower()
            ),
            None,
        )
        next_url = urljoin(page_url, next_link["href"]) if next_link else None
        if next_url in seen_pages:
            next_url = None
        page_url = next_url
        page_number += 1

    return rows


def search_0mag(title: str) -> dict:
    search_url = f"https://0mag.net/search?q={quote(title)}"
    soup = BeautifulSoup(fetch_html(search_url), "html.parser")
    count = None
    for paragraph in soup.select("p"):
        match = RESULT_COUNT_RE.search(paragraph.get_text(" ", strip=True))
        if match:
            count = int(match.group(1).replace(",", ""))
            break
    if count is None:
        raise RuntimeError("0mag result-count marker was not found")
    return {
        "exists": "yes" if count > 0 else "no",
        "result_count": count,
        "search_url": search_url,
    }


def output_name_for(source_url: str) -> Path:
    parts = [part for part in urlparse(source_url).path.split("/") if part]
    slug = parts[-1] if parts else "catalog"
    safe_slug = re.sub(r"[^\w.-]+", "_", slug, flags=re.UNICODE).strip("_")
    return Path(__file__).with_name(f"tangxin_{safe_slug or 'catalog'}_0mag.csv")


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "page",
        "title",
        "exists",
        "result_count",
        "source_url",
        "search_url",
        "checked_at",
        "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract every Tangxin actress-page title and check 0mag results."
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_SOURCE)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--delay", type=float, default=0.5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.delay < 0.2:
        raise SystemExit("--delay must be at least 0.2 seconds")

    print(f"Extracting all pages from {args.url}")
    catalog = extract_catalog(args.url)
    print(f"Found {len(catalog)} unique titles")

    rows: list[dict] = []
    for index, item in enumerate(catalog, start=1):
        if index > 1:
            time.sleep(args.delay)
        print(f"[{index}/{len(catalog)}] Searching: {item['title']}")
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            result = search_0mag(item["title"])
            row = {**item, **result, "checked_at": checked_at, "error": ""}
        except Exception as exc:
            row = {
                **item,
                "exists": "unknown",
                "result_count": "",
                "search_url": f"https://0mag.net/search?q={quote(item['title'])}",
                "checked_at": checked_at,
                "error": str(exc),
            }
        rows.append(row)

    output = (args.output or output_name_for(args.url)).expanduser().resolve()
    write_csv(output, rows)
    yes_count = sum(row["exists"] == "yes" for row in rows)
    unknown_count = sum(row["exists"] == "unknown" for row in rows)
    print(
        f"Wrote {len(rows)} rows ({yes_count} found, {unknown_count} unknown) "
        f"to {output}"
    )


if __name__ == "__main__":
    main()
