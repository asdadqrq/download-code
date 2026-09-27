#!/usr/bin/env python3
import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, parse_qs
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag, Comment


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


def compact_dmm_code(value):
    value = unquote(value or "").strip().lower()
    value = re.sub(r"^h_?\d+(?=[a-z])", "", value)
    value = re.sub(r"^\d+(?=[a-z])", "", value)
    match = re.fullmatch(r"([a-z][a-z0-9]*?)(\d+)([a-z]?)", value)
    if not match:
        return ""
    prefix, number, suffix = match.groups()
    normalized_number = str(int(number)).zfill(3)
    return f"{prefix.upper()}-{normalized_number}{suffix.upper()}"


def code_from_detail_url(href):
    parsed = urlsplit(href or "")
    host = (parsed.hostname or "").lower()
    if host == "dmm.co.jp" or host.endswith(".dmm.co.jp"):
        product_id = parse_qs(parsed.query).get("id", [""])[0]
        if not product_id:
            match = re.search(r"/cid=([^/]+)/?", parsed.path, re.IGNORECASE)
            product_id = match.group(1) if match else ""
        return compact_dmm_code(product_id)
    if host == "mgstage.com" or host.endswith(".mgstage.com"):
        match = re.search(r"/product/product_detail/([^/]+)/?", parsed.path, re.IGNORECASE)
        candidate = unquote(match.group(1)) if match else ""
        return candidate.upper() if CODE_ONLY_RE.fullmatch(candidate) and re.search(r"\d", candidate) else ""
    if host == "prestige-av.com" or host.endswith(".prestige-av.com"):
        candidate = parse_qs(parsed.query).get("sku", [""])[0]
        return candidate.upper() if CODE_ONLY_RE.fullmatch(candidate) and re.search(r"\d", candidate) else ""
    if host == "adult.contents.fc2.com":
        match = re.fullmatch(r"/article/(\d+)/?", parsed.path)
        return f"FC2-PPV-{match.group(1)}" if match else ""
    return ""


def release_entries(area):
    """Yield every dated block, including entries whose code is absent."""
    for node in area.find_all(string=RELEASE_DATE_RE):
        if isinstance(node, Comment) or node.find_parent(["script", "style"]):
            continue
        for date_match in RELEASE_DATE_RE.finditer(str(node)):
            date = date_match.group()
            header_parts = [str(node)[date_match.end():]]
            last_text_node = node
            for following in node.next_elements:
                if isinstance(following, Tag) and following.name in {"br", "a"}:
                    break
                if isinstance(following, NavigableString):
                    if RELEASE_DATE_RE.search(str(following)):
                        break
                    header_parts.append(str(following))
                    last_text_node = following
            code_match = CODE_ONLY_RE.search(clean_text(" ".join(header_parts)))
            code = code_match.group().upper() if code_match else ""
            try:
                date = datetime.strptime(date, "%Y/%m/%d").strftime("%Y/%m/%d")
            except ValueError:
                continue
            yield last_text_node if code else node, date, code


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


def fallback_title_link_for_entry(node):
    """Use a text-bearing external link when its provider is not recognized."""
    for following in node.next_elements:
        if isinstance(following, NavigableString) and RELEASE_DATE_RE.search(str(following)):
            break
        if not isinstance(following, Tag) or following.name != "a":
            continue
        text = clean_text(following.get_text(" ", strip=True))
        href = following.get("href", "")
        parsed = urlsplit(href)
        if not text or CODE_ONLY_RE.fullmatch(text) or text.startswith("("):
            continue
        if parsed.scheme not in {"http", "https"} or parsed.hostname == "seesaawiki.jp":
            continue
        if "outlink" in (following.get("class") or []):
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
        detail_link = detail_link_for_entry(node) or fallback_title_link_for_entry(node)
        title = clean_text(detail_link.get_text(" ", strip=True)) if detail_link else ""
        if not code and detail_link:
            code = code_from_detail_url(detail_link.get("href", ""))
        if not title and (not code or code.startswith("FC2")):
            title = plain_title_for_entry(node)
        if not code and not title:
            continue
        if code and not detail_link:
            print(
                f"Warning: {code} ({release_date}): no recognized title link; "
                "retaining the code with blank detail_url.",
                file=sys.stderr,
            )
        detail_url = detail_link.get("href", "") if detail_link else ""
        key = (url, release_date, code, title, detail_url)
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
    known_codes = {row["code"].upper() for row in rows if row["code"]}
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
