#!/usr/bin/env python3
"""Extract every paginated Tangxin actress title into a Simplified Chinese CSV."""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: beautifulsoup4\n"
        "Install it with: /opt/anaconda3/bin/python -m pip install beautifulsoup4"
    ) from exc


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


def build_converter():
    try:
        from opencc import OpenCC

        converter = OpenCC("t2s")
        return converter.convert
    except ImportError:
        pass

    try:
        from Foundation import NSString

        def convert_with_macos(text: str) -> str:
            value = NSString.stringWithString_(text).stringByApplyingTransform_reverse_(
                "Traditional-Simplified", False
            )
            return str(value or text)

        return convert_with_macos
    except ImportError as exc:
        raise SystemExit(
            "Simplified Chinese conversion is unavailable. Install OpenCC with:\n"
            "/opt/anaconda3/bin/python -m pip install opencc-python-reimplemented"
        ) from exc


to_simplified = build_converter()


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


def extract_all_titles(start_url: str) -> list[str]:
    titles: list[str] = []
    seen_titles: set[str] = set()
    seen_pages: set[str] = set()
    page_url: str | None = start_url

    while page_url and page_url not in seen_pages:
        print(f"Reading {page_url}")
        seen_pages.add(page_url)
        soup = BeautifulSoup(fetch_html(page_url), "html.parser")
        title_links = soup.select("main article h3 a[href]")
        if not title_links:
            raise RuntimeError(f"No titles found on {page_url}")

        for link in title_links:
            source_title = " ".join(link.get_text(" ", strip=True).split())
            title = " ".join(to_simplified(source_title).split())
            if title and title not in seen_titles:
                seen_titles.add(title)
                titles.append(title)

        next_link = next(
            (
                link
                for link in soup.select('nav[aria-label="pagination"] a[href]')
                if "下一頁" in link.get_text(" ", strip=True)
                or "下一页" in link.get_text(" ", strip=True)
                or "next" in link.get_text(" ", strip=True).lower()
            ),
            None,
        )
        page_url = urljoin(page_url, next_link["href"]) if next_link else None

    return titles


def default_output_path(source_url: str) -> Path:
    path_parts = [part for part in urlsplit(source_url).path.split("/") if part]
    actress = unquote(path_parts[-1]) if path_parts else "actress"
    actress = to_simplified(actress).lstrip("@").strip()
    safe_name = re.sub(r"[^\w.-]+", "_", actress, flags=re.UNICODE).strip("_")
    return Path(__file__).with_name(f"tangxin_{safe_name or 'titles'}_titles.csv")


def write_csv(path: Path, titles: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["title"])
        writer.writerows([[title] for title in titles])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract all Tangxin actress-page titles as Simplified Chinese."
    )
    parser.add_argument("url", help="Tangxin actress URL")
    parser.add_argument("-o", "--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    titles = extract_all_titles(args.url)
    output = (args.output or default_output_path(args.url)).expanduser().resolve()
    write_csv(output, titles)
    print(f"Wrote {len(titles)} titles to {output}")


if __name__ == "__main__":
    main()
