#!/usr/bin/env python3
"""Fetch incremental LTX metadata from BT4G and Sukebei into a deduplicated CSV.

Install: python -m pip install requests beautifulsoup4
BT4G uses an already-open Chrome tab, as in check_bt4g_titles.py.
Dates are source creation/upload dates (inclusive, YYYY-MM-DD). 1G = 1 GiB.
Existing CSV: resume at each site's latest saved date, including that date.
Missing CSV: create it and collect all history. --full-rescan disables resume.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

KEYWORD = 'LTX'
MIN_BYTES = 1024 ** 3
FIELDS = ['title', 'magnet', 'create_time', 'file_size', 'file_size_bytes', 'info_hash', 'source', 'source_url']
PRINT_LOCK = threading.Lock()


def log(message):
    with PRINT_LOCK:
        print(message, flush=True)


def info_hash(value):
    value = (value or '').strip()
    if re.fullmatch(r'[a-fA-F0-9]{40}', value):
        return value.lower()
    if value.lower().startswith('magnet:'):
        for xt in parse_qs(urlsplit(value).query).get('xt', []):
            if xt.lower().startswith('urn:btih:'):
                digest = xt[9:]
                if re.fullmatch(r'[a-fA-F0-9]{40}', digest):
                    return digest.lower()
                if re.fullmatch(r'[a-zA-Z2-7]{32}', digest):
                    return base64.b32decode(digest.upper()).hex()
    return ''


def parse_size(value):
    match = re.search(r'(?<![\d.])(\d+(?:[.,]\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b', value or '', re.I)
    if not match:
        raise ValueError('Missing or unrecognized file size')
    number, unit = match.groups()
    exponent = {'B':0, 'KB':1, 'KIB':1, 'MB':2, 'MIB':2, 'GB':3, 'GIB':3, 'TB':4, 'TIB':4}[unit.upper()]
    return int(Decimal(number.replace(',', '.')) * (1024 ** exponent))


def parse_date_range(value):
    if value is None or value.strip().lower() in ('', 'null', 'none', 'all'):
        return None
    parts = value.strip().strip('()[]').split(',')
    if len(parts) != 2:
        raise ValueError('Use --create-time "(2020-10-11,2021-11-11)" or null')
    start, end = [date.fromisoformat(p.strip()) for p in parts]
    if start > end:
        raise ValueError('Date range start must not be after end')
    return start, end


def in_range(value, bounds):
    if bounds is None:
        return True
    if not value:
        return False
    return bounds[0] <= date.fromisoformat(value) <= bounds[1]


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)
    if not fields or any(None in row for row in rows):
        raise ValueError('Existing CSV has missing headers or malformed rows')
    return fields, rows


def sorted_search_url(source, page=1):
    if source == 'sukebei':
        return 'https://sukebei.nyaa.si/?' + urlencode({
            'f':0, 'c':'0_0', 'q':KEYWORD, 's':'id', 'o':'desc', 'p':page,
        })
    # BT4G's observed "By Time" link has no orderby parameter. Omitting it
    # selects descending creation time; size/seeders/relevance are other modes.
    return 'https://bt4gprx.com/search?' + urlencode({'q':KEYWORD, 'p':page})


class DateWindow:
    """Validate descending dates before using an incremental stopping point."""
    def __init__(self, cutoff=None):
        self.cutoff = cutoff
        self.previous = None

    def select(self, items):
        selected, crossed = [], False
        for item in items:
            value = date.fromisoformat(item['create_time'])
            if self.previous is not None and value > self.previous:
                raise RuntimeError('Source results are not ordered by time descending; refusing an unsafe incremental stop')
            self.previous = value
            if self.cutoff is not None and value < self.cutoff:
                crossed = True
            else:
                selected.append(item)
        return selected, crossed


class CsvStore:
    """One thread-safe hash check and durable append for every discovered item."""
    def __init__(self, path, bounds=None, incremental=True):
        self.path, self.bounds = Path(path), bounds
        self.incremental = incremental
        self.latest = {}
        self.lock = threading.Lock()
        self.counts = Counter()
        fields, rows = read_csv(self.path)
        self.fields = fields + [f for f in FIELDS if f not in fields]
        self.seen = set()
        self.urls = set()
        for row in rows:
            has_hash = False
            for column in ('info_hash', 'hash', 'torrent_hash', 'magnet', 'magnet_link', 'magnet_uri'):
                digest = info_hash(row.get(column))
                if digest:
                    self.seen.add(digest)
                    has_hash = True
            if has_hash and row.get('source_url'):
                self.urls.add(row['source_url'])
            if has_hash:
                source = (row.get('source') or '').lower()
                if source not in ('bt4g', 'sukebei'):
                    host = urlsplit(row.get('source_url') or '').hostname
                    source = {'bt4gprx.com':'bt4g', 'sukebei.nyaa.si':'sukebei'}.get(host)
                try:
                    created = date.fromisoformat(row.get('create_time') or '')
                except ValueError:
                    continue
                if source:
                    self.latest[source] = max(created, self.latest.get(source, created))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fields != self.fields or not self.path.exists():
            temporary = self.path.with_suffix(self.path.suffix + '.tmp')
            with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fields)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(self.path)
        log(f'CSV: {self.path.resolve()} | existing unique hashes: {len(self.seen)}')

    def cutoff(self, source):
        # An explicit historical range overrides automatic incremental mode.
        if self.bounds is not None:
            return self.bounds[0]
        return self.latest.get(source) if self.incremental else None

    def sort_newest_first(self):
        with self.lock:
            fields, rows = read_csv(self.path)
            def date_key(row):
                try:
                    return date.fromisoformat(row.get('create_time') or '')
                except ValueError:
                    return date.min
            rows.sort(key=date_key, reverse=True)
            temporary = self.path.with_suffix(self.path.suffix + '.tmp')
            with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(self.path)

    def known_url(self, url):
        with self.lock:
            return url in self.urls

    def add(self, item):
        digest = info_hash(item.get('magnet')) or info_hash(item.get('info_hash'))
        if not digest:
            raise ValueError('Item has no valid BitTorrent hash')
        with self.lock:
            # The check and write share a lock, so cross-site duplicates cannot race.
            if digest in self.seen:
                self.counts['duplicates'] += 1
                return False
            if int(item['file_size_bytes']) < MIN_BYTES:
                self.counts['below_1G'] += 1
                return False
            if not in_range(item.get('create_time'), self.bounds):
                self.counts['outside_date_range'] += 1
                return False
            item = {**item, 'info_hash':digest}
            if not item.get('magnet', '').lower().startswith('magnet:'):
                item['magnet'] = 'magnet:?xt=urn:btih:' + digest
            with self.path.open('a', encoding='utf-8', newline='') as handle:
                csv.DictWriter(handle, fieldnames=self.fields, extrasaction='ignore').writerow(item)
                handle.flush()
                os.fsync(handle.fileno())
            self.seen.add(digest)
            if item.get('source_url'):
                self.urls.add(item['source_url'])
            self.counts['added'] += 1
            self.counts[item['source']] += 1
            return True


def fetch(session, url, timeout, retries=2):
    for attempt in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)


def parse_sukebei(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.torrent-list')
    if table is None:
        raise RuntimeError('Sukebei result table missing; the response may be a verification/error page')
    items = []
    for tr in table.select('tbody tr'):
        cells = tr.find_all('td', recursive=False)
        link = next((a for a in tr.select('a[href]') if re.fullmatch(r'/view/\d+', a.get('href', ''))), None)
        magnet = tr.select_one('a[href^="magnet:"]')
        timestamp = tr.select_one('[data-timestamp]')
        if not link or not magnet or len(cells) < 5 or not timestamp:
            raise RuntimeError('Could not parse a Sukebei result row')
        size = cells[3].get_text(' ', strip=True)
        created = datetime.fromtimestamp(int(timestamp['data-timestamp']), timezone.utc).date().isoformat()
        items.append({'title':link.get('title') or link.get_text(' ', strip=True),
                      'magnet':magnet['href'], 'create_time':created, 'file_size':size,
                      'file_size_bytes':parse_size(size), 'source':'sukebei',
                      'source_url':urljoin(url, link['href'])})
    current = int(parse_qs(urlsplit(url).query).get('p', ['1'])[0])
    next_url = ''
    for a in soup.select('.pagination a[href]'):
        candidate = urljoin(url, a['href'])
        parsed = urlsplit(candidate)
        q = parse_qs(parsed.query)
        if (parsed.netloc == 'sukebei.nyaa.si' and q.get('q') == [KEYWORD]
                and q.get('p') == [str(current + 1)]):
            next_url = sorted_search_url('sukebei', current + 1)
            break
    return items, next_url


def collect_sukebei(store, args):
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    url = sorted_search_url('sukebei')
    window = DateWindow(store.cutoff('sukebei'))
    log(f'sukebei: newest first; lower date boundary: {window.cutoff or "none (full history)"}')
    signatures, pages = set(), 0
    try:
        while url:
            if args.max_pages and pages >= args.max_pages:
                raise RuntimeError('Sukebei page limit reached; collection is incomplete')
            items, next_url = parse_sukebei(fetch(session, url, args.timeout), url)
            signature = tuple(info_hash(item['magnet']) for item in items)
            if signature and signature in signatures:
                raise RuntimeError('Sukebei pagination repeated a page')
            signatures.add(signature)
            selected, crossed = window.select(items)
            for item in selected:
                if KEYWORD.casefold() in item['title'].casefold():
                    store.add(item)
            pages += 1
            log(f'sukebei: page {pages}, {len(items)} items read')
            if crossed:
                log(f'sukebei: reached dates older than {window.cutoff}; incremental search complete')
                break
            url = next_url
            if url:
                time.sleep(args.delay)
    finally:
        session.close()
    return pages


BT4G_METADATA = r"""(() => {
  const anchors = [...document.querySelectorAll('a[href]')];
  const results = anchors.filter(a => {
    const u = new URL(a.href, location.href);
    return u.origin === location.origin && /^\/(magnet|hash)\//.test(u.pathname);
  }).map(a => {
    let text = '';
    for (let node = a.parentElement; node && node !== document.body; node = node.parentElement) {
      const urls = new Set([...node.querySelectorAll('a[href]')].filter(x =>
        /^\/(magnet|hash)\//.test(new URL(x.href,location.href).pathname)).map(x=>x.href));
      if (urls.size > 1) break;
      text = node.textContent;
      if (/Creation\s+Time/i.test(text) && /Total\s+Size/i.test(text)) break;
    }
    const created = text.match(/Creation\s+Time\s*:\s*(\d{4}-\d{2}-\d{2})/i);
    const size = text.match(/Total\s+Size\s*:\s*(\d+(?:[.,]\d+)?\s*(?:TiB|GiB|MiB|KiB|TB|GB|MB|KB|B))\b/i);
    return {title:a.textContent.trim(), source_url:a.href,
      create_time:created ? created[1] : '', file_size:size ? size[1] : ''};
  });
  return {items:results, links:anchors.map(a=>a.href)};
})()"""


class PinnedBT4GClient:
    """Reuse the existing helper without changing the user's selected tab."""
    def __new__(cls):
        from check_bt4g_exists import CurrentChromeClient, run_applescript
        output = run_applescript([
            'tell application "Google Chrome"',
            'repeat with w in windows', 'repeat with t in tabs of w',
            'if (URL of t) starts with "https://bt4gprx.com/" then',
            'return (id of w as text) & linefeed & (id of t as text)',
            'end if', 'end repeat', 'end repeat',
            'error "Open a verified https://bt4gprx.com/ tab in Chrome first."',
            'end tell',
        ])
        window, tab = output.split('\n', 1)
        client = object.__new__(CurrentChromeClient)
        client.window_id, client.tab_id = int(window), int(tab)
        return client


BT4G_DETAIL = r"""(() => {
  const links = [...document.querySelectorAll('a[href]')];
  return {url:location.href, title:document.title,
    blocked:/just a moment|verify you are human|attention required/i.test(document.title),
    magnet:links.find(a=>a.href.startsWith('magnet:'))?.href || '',
    download:links.find(a=>{
      const u=new URL(a.href,location.href);
      return u.hostname==='downloadtorrentfile.com' && /^\/hash\/[a-f0-9]{40}\/?$/i.test(u.pathname);
    })?.href || ''};
})()"""


def read_bt4g_magnet(client, url, timeout):
    from check_bt4g_exists import VerificationRequired
    from check_bt4g_titles import extract_torrent_hash
    if client.evaluate('location.href') != url:
        client.call('Page.navigate', {'url':url})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.evaluate(BT4G_DETAIL)
        if state.get('url', '').rstrip('/') == url.rstrip('/'):
            if state.get('blocked'):
                raise VerificationRequired('Complete BT4G verification in Chrome and rerun to resume')
            if info_hash(state.get('magnet')):
                return state['magnet']
            if state.get('download'):
                return 'magnet:?xt=urn:btih:' + extract_torrent_hash(state['download'])
        time.sleep(0.4)
    raise RuntimeError(f'No usable magnet/download link on {url}')


def collect_bt4g(store, args):
    from check_bt4g_exists import VerificationRequired, wait_for_result_cdp
    from check_bt4g_titles import next_result_page
    client = PinnedBT4GClient()
    # RSS is only a hash lookup for the latest page, never the pagination source.
    rss_hashes = {}
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    try:
        xml = fetch(session, 'https://bt4gprx.com/search?q=LTX&page=rss', args.timeout, retries=0)
        for element in ET.fromstring(xml).findall('./channel/item'):
            rss_hashes[element.findtext('guid', '').strip()] = element.findtext('link', '').strip()
    except (requests.RequestException, ET.ParseError):
        pass
    finally:
        session.close()
    pages, seen_pages, seen_items = 0, set(), set()
    detail_errors = []
    url = sorted_search_url('bt4g')
    window = DateWindow(store.cutoff('bt4g'))
    log(f'bt4g: newest first; lower date boundary: {window.cutoff or "none (full history)"}')
    try:
        while url:
            if args.max_pages and pages >= args.max_pages:
                raise RuntimeError('BT4G page limit reached; collection is incomplete')
            if url in seen_pages:
                raise RuntimeError('BT4G pagination repeated a page')
            seen_pages.add(url)
            if client.evaluate('location.href') != url:
                client.call('Page.navigate', {'url':url})
            state = wait_for_result_cdp(client, url, timeout=args.timeout)
            if state.get('blocked'):
                raise RuntimeError('Complete BT4G verification in its Chrome tab, then rerun to resume')
            page = client.evaluate(BT4G_METADATA)
            if state.get('count', 0) and not page['items']:
                raise RuntimeError('BT4G result cards could not be read')
            next_url = next_result_page(page['links'], url, KEYWORD)
            if next_url:
                next_url = sorted_search_url('bt4g', int(parse_qs(urlsplit(next_url).query)['p'][0]))
            selected, crossed = window.select(page['items'])
            selected_urls = {item['source_url'] for item in selected}
            fresh_items = []
            new_on_page = 0
            for item in page['items']:
                if item['source_url'] in seen_items:
                    continue
                seen_items.add(item['source_url'])
                new_on_page += 1
                if item['source_url'] in selected_urls:
                    fresh_items.append(item)
            for item in fresh_items:
                if KEYWORD.casefold() not in item['title'].casefold() or store.known_url(item['source_url']):
                    continue
                item['file_size_bytes'] = parse_size(item['file_size'])
                if item['file_size_bytes'] < MIN_BYTES or not in_range(item['create_time'], store.bounds):
                    continue
                item['source'] = 'bt4g'
                item['magnet'] = rss_hashes.get(item['source_url'], '')
                if not info_hash(item['magnet']):
                    try:
                        item['magnet'] = read_bt4g_magnet(client, item['source_url'], args.timeout)
                    except VerificationRequired:
                        raise
                    except (RuntimeError, OSError, ValueError) as exc:
                        detail_errors.append(str(exc))
                        log(f'bt4g: detail skipped: {exc}')
                        continue
                store.add(item)
            if page['items'] and not new_on_page:
                raise RuntimeError('BT4G pagination repeated the same results')
            pages += 1
            log(f'bt4g: page {pages}, {len(page["items"])} items read, {store.counts["added"]} total appended')
            if crossed:
                log(f'bt4g: reached dates older than {window.cutoff}; incremental search complete')
                break
            url = next_url
            if url:
                time.sleep(args.delay)
            elif state.get('count', 0) > len(seen_items):
                raise RuntimeError('BT4G pagination ended before all reported results were read')
    finally:
        client.close()
    if detail_errors:
        raise RuntimeError(f'{pages} pages read, but {len(detail_errors)} details failed: ' + '; '.join(detail_errors))
    return pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', '-o', '--output', dest='csv_path', type=Path, default=Path('LTX_results.csv'),
                        help='Existing CSV to compare/append, or filename for a new CSV')
    parser.add_argument('--create-time', help='Inclusive range (YYYY-MM-DD,YYYY-MM-DD), overriding CSV date watermarks; null means no explicit date filter')
    parser.add_argument('--sources', nargs='+', choices=['bt4g','sukebei'], default=['bt4g','sukebei'])
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-pages', type=int, default=0, help='Optional page limit per source; 0 = no limit (incremental cutoff still applies)')
    parser.add_argument('--full-rescan', action='store_true', help='Ignore CSV date watermarks and scan all history again')
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.max_pages < 0:
        parser.error('Invalid delay, timeout, or page limit')
    try:
        bounds = parse_date_range(args.create_time)
        store = CsvStore(args.csv_path.expanduser().resolve(), bounds, incremental=not args.full_rescan)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    errors = []
    workers = {'bt4g':collect_bt4g, 'sukebei':collect_sukebei}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(workers[source], store, args):source for source in set(args.sources)}
        for future in as_completed(futures):
            source = futures[future]
            try:
                log(f'{source}: complete ({future.result()} pages)')
            except Exception as exc:
                errors.append(f'{source}: {exc}')
                log('INCOMPLETE ' + errors[-1])
    store.sort_newest_first()
    log('Results: ' + ', '.join(f'{key}={value}' for key,value in store.counts.items()))
    log(f'CSV saved: {store.path}')
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
