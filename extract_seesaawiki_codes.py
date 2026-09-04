#!/usr/bin/env python3
import argparse
import csv
import re
import sys
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, parse_qs
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag, Comment


ENTRY_RE = re.compile(
    r"\b(\d{4}/\d{1,2}/\d{1,2})\s+(?:発売日変更\s*)?"
    r"([A-Z0-9]+(?:-[A-Z0-9]+)+)(?![A-Z0-9-])", re.IGNORECASE
)
CODE_ONLY_RE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)+", re.IGNORECASE)
RELEASE_DATE_RE = re.compile(r"\b\d{4}/\d{1,2}/\d{1,2}\b")


def is_detail_link(href):
    parsed = urlsplit(href or "")
    host = (parsed.hostname or "").lower()
    if host == "dmm.co.jp" or host.endswith(".dmm.co.jp"):
        return "/detail/" in parsed.path or (
            host == "video.dmm.co.jp"
            and parsed.path.rstrip("/") in {"/av/content", "/amateur/content"}
            and bool(parse_qs(parsed.query).get("id"))
        )
    if host == "mgstage.com" or host.endswith(".mgstage.com"):
        return parsed.path.startswith("/product/product_detail/")
    if host == "prestige-av.com" or host.endswith(".prestige-av.com"):
        return (
            parsed.path == "/goods/goods_detail.php"
            and bool(parse_qs(parsed.query).get("sku"))
        )
    if host == "adult.contents.fc2.com":
        return bool(re.fullmatch(r"/article/\d+/?", parsed.path))
    return False


def release_entries(area):
    # Match across inline markup, then map the code back to its last DOM text node.
    nodes = [node for node in area.find_all(string=True)
             if not isinstance(node, Comment)
             and not node.find_parent(["script", "style"])]
    starts, parts, position = [], [], 0
    for node in nodes:
        starts.append(position)
        parts.append(str(node))
        position += len(str(node)) + 1
    for match in ENTRY_RE.finditer(" ".join(parts)):
        date, code = match.groups()
        try:
            date = datetime.strptime(date, "%Y/%m/%d").strftime("%Y/%m/%d")
        except ValueError:
            continue
        node = nodes[bisect_right(starts, match.end(2) - 1) - 1]
        yield node, date, code.upper()


def plain_title_for_entry(node):
    parts = []
    for following in node.next_elements:
        if isinstance(following, Tag):
            if following.name == "br" and parts:
                break
            if following.name in {"a", "img", "h2", "h3", "h4"}:
                break
        elif isinstance(following, NavigableString):
            text = clean_text(str(following))
            if RELEASE_DATE_RE.search(text):
                break
            if text:
                parts.append(text)
    return clean_text(" ".join(parts))


def detail_link_for_entry(node):
    # Release entries are separated by dates, not necessarily by table rows.
    for following in node.next_elements:
        if isinstance(following, NavigableString) and RELEASE_DATE_RE.search(str(following)):
            break
        if isinstance(following, Tag) and following.name == "a":
            text = clean_text(following.get_text(" ", strip=True))
            if is_detail_link(following.get("href")) and text and not CODE_ONLY_RE.fullmatch(text):
                return following
    return None


def split_urls(values):
    urls = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                urls.append(part)
    return urls


def fetch_html(url):
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "euc-jp"
            return raw.decode(charset, "replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while fetching {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Fetch failed for {url}: {exc.reason}") from exc


def page_name_from_url(url):
    slug = url.rstrip("/").split("/")[-1]
    for charset in ("euc-jp", "shift_jis", "utf-8"):
        try:
            return unquote(slug, encoding=charset)
        except UnicodeDecodeError:
            continue
    return unquote(slug)


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def extract_page(url):
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    page_name = clean_text(soup.select_one("#page-header h2").get_text(" ", strip=True)) if soup.select_one("#page-header h2") else page_name_from_url(url)
    user_area = soup.select_one(".user-area") or soup
    # Superseded dates must not replace the current release date.
    for deleted in user_area.select("del, s, strike"):
        deleted.decompose()

    rows = []
    seen = set()
    for node, release_date, code in release_entries(user_area):
        detail_link = detail_link_for_entry(node)
        title = clean_text(detail_link.get_text(" ", strip=True)) if detail_link else ""
        if not title and code.startswith("FC2"):
            title = plain_title_for_entry(node)
        if not detail_link:
            print(
                f"Warning: {code} ({release_date}): no recognized title link; "
                "retaining the code with blank detail_url.",
                file=sys.stderr,
            )
        detail_url = detail_link.get("href", "") if detail_link else ""
        key = (url, release_date, code, detail_url)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "source_url": url,
                "page_name": page_name,
                "release_date": release_date,
                "code": code,
                "title": title,
                "detail_url": detail_url,
            }
        )
    # Notes can reference releases without giving a date or title of their own.
    known_codes = {row["code"].upper() for row in rows}
    for link in user_area.find_all("a", href=True):
        code = clean_text(link.get_text(" ", strip=True))
        if not CODE_ONLY_RE.fullmatch(code) or not is_detail_link(link["href"]):
            continue
        if code.upper() in known_codes:
            continue
        known_codes.add(code.upper())
        rows.append({
            "source_url": url,
            "page_name": page_name,
            "release_date": "",
            "code": code.upper(),
            "title": "",
            "detail_url": link["href"],
        })
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Extract release codes and titles from SeesaaWiki sougouwiki profile pages."
    )
    parser.add_argument("urls", nargs="*", help="One or more URLs. Comma-separated URLs are also accepted.")
    parser.add_argument("--urls-file", help="Text file containing one URL per line")
    parser.add_argument("-o", "--output", default="seesaawiki_codes.csv", help="Output CSV path")
    args = parser.parse_args()

    urls = split_urls(args.urls)
    if args.urls_file:
        urls.extend(split_urls(Path(args.urls_file).read_text(encoding="utf-8").splitlines()))
    if not urls:
        parser.error("provide at least one SeesaaWiki URL")

    all_rows = []
    for url in urls:
        rows = extract_page(url)
        print(f"{url}: extracted {len(rows)} rows")
        all_rows.extend(rows)

    if not all_rows:
        sys.exit("No rows extracted.")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_url", "page_name", "release_date", "code", "title", "detail_url"],
        )
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
