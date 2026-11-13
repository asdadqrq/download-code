#!/usr/bin/env python3
"""Collect LTX forum-search titles into CSV using a logged-in Chrome tab.

Columns: title, link, date (YYYY-MM-DD), code (blank for new items).
Existing rows and manually entered codes are preserved. New TIDs go above them.
Search always starts at page 1 and stops at the first TID already in the input
CSV. With no saved TIDs, it continues to the last results page. A null date range
disables date filtering; it does not disable the existing-TID stopping rule.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from check_bt4g_exists import CurrentChromeClient, run_applescript

KEYWORD = 'LTX'
ORIGIN = 'https://sehuatang.net'
HOSTS = {'sehuatang.net', 'www.sehuatang.net'}
EXCLUDED = {'求片问答悬赏区', '综合讨论区', '投诉建议区', '个人导航'}
FIELDS = ['title', 'link', 'date', 'code']


def tid_from_link(value):
    try:
        parsed = urlsplit((value or '').strip())
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in HOSTS:
            return ''
        tids = parse_qs(parsed.query).get('tid', [])
        if parsed.path.rstrip('/') == '/forum.php' and len(tids) == 1:
            tid = tids[0]
        else:
            match = re.fullmatch(r'/thread-(\d+)-\d+-\d+\.html', parsed.path)
            tid = match.group(1) if match else ''
        return str(int(tid)) if re.fullmatch(r'\d{1,20}', tid) and int(tid) > 0 else ''
    except ValueError:
        return ''


def parse_date_range(value):
    if value is None or value.strip().lower() in ('', 'null', 'none', 'all'):
        return None
    parts = value.strip().strip('()[]').split(',')
    if len(parts) != 2:
        raise ValueError('Use --date-range "(2026-11-01,2026-11-12)" or null')
    start, end = [date.fromisoformat(part.strip()) for part in parts]
    if start > end:
        raise ValueError('Date range start must not be after end')
    return start, end


def normalize_date(value):
    match = re.search(r'(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', value or '')
    if match:
        return date(*map(int, match.groups())).isoformat()
    # Some Discuz themes render recent dates relatively instead of full dates.
    for label, days in [('今天', 0), ('昨天', 1), ('前天', 2)]:
        if label in (value or ''):
            return (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=days)).isoformat()
    raise ValueError(f'Unrecognized search-result date: {value!r}')


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames or [], list(reader)
    if not fields or any(None in row for row in rows):
        raise ValueError('Existing CSV is malformed')
    return fields, rows


class ThreadCsv:
    def __init__(self, path, bounds=None):
        self.path, self.bounds = Path(path), bounds
        old_fields, self.original = read_csv(self.path)
        self.fields = old_fields + [field for field in FIELDS if field not in old_fields]
        self.new = []
        self.seen = {tid for row in self.original if (tid := tid_from_link(row.get('link', '')))}
        self.existing_tids = frozenset(self.seen)
        self.counts = Counter()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if old_fields != self.fields or not self.path.exists():
            self.flush()

    def offer(self, item):
        section = re.sub(r'\s+', '', item.get('section', ''))
        if not section:
            raise ValueError('Cannot determine the result section; refusing to include it')
        if section in EXCLUDED:
            self.counts['excluded_section'] += 1
            return False
        tid = tid_from_link(item.get('link', ''))
        if not tid:
            raise ValueError('Result has no valid forum thread ID')
        if tid in self.seen:
            self.counts['existing_tid'] += 1
            return False
        created = normalize_date(item.get('date', ''))
        if self.bounds and not self.bounds[0] <= date.fromisoformat(created) <= self.bounds[1]:
            self.counts['outside_date_range'] += 1
            return False
        title = item.get('title', '').strip()
        if not title:
            raise ValueError('Search-result title is empty')
        self.new.append({'title':title, 'link':ORIGIN+'/forum.php?mod=viewthread&tid='+tid,
                         'date':created, 'code':''})
        self.seen.add(tid)
        self.counts['added'] += 1
        return True

    def flush(self):
        # All this run's additions stay above pre-existing rows. Order the new
        # block by date/TID, without modifying old manually entered code values.
        self.new.sort(key=lambda row:(row['date'], int(tid_from_link(row['link']))), reverse=True)
        temporary = self.path.with_suffix(self.path.suffix+'.tmp')
        with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.new)
            writer.writerows(self.original)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)


class ForumChromeClient(CurrentChromeClient):
    def __init__(self):
        output = run_applescript([
            'tell application "Google Chrome"',
            'repeat with w in windows', 'repeat with t in tabs of w',
            'set u to URL of t',
            'if u starts with "https://sehuatang.net/" or u starts with "https://www.sehuatang.net/" then',
            'return (id of w as text) & linefeed & (id of t as text)',
            'end if', 'end repeat', 'end repeat',
            'error "Open a logged-in sehuatang.net LTX search tab in Chrome first."',
            'end tell',
        ])
        window, tab = output.split('\n', 1)
        self.window_id, self.tab_id = int(window), int(tab)


READ_PAGE = r"""(() => {
  const body = document.body ? document.body.innerText : '';
  const result = body.match(/相关内容\s*([\d,]+)\s*个/);
  const nodes = [...document.querySelectorAll('li.pbw')].filter(li=>li.querySelector('h3 a[href]'));
  const items = nodes.map(li => {
    const anchor = li.querySelector('h3 a[href]');
    const metadata = [...li.querySelectorAll('p')].reverse().find(p=>p.querySelector('a[href*="mod=forumdisplay"]'));
    const section = metadata?.querySelector('a[href*="mod=forumdisplay"]');
    const stamp = metadata?.querySelector('span');
    const exactStamp = stamp?.getAttribute('title') || stamp?.querySelector('[title]')?.getAttribute('title');
    return {title:anchor.textContent.trim(), link:anchor.href,
      section:section?.textContent.trim() || '', date:exactStamp || stamp?.textContent.trim() || ''};
  });
  // A late ad/resource can keep readyState=loading after the result list is
  // complete. The footer is after the list and pagination in this theme.
  const documentReady = document.readyState === 'complete' || document.readyState === 'interactive' || Boolean(document.querySelector('#ft'));
  const rowsReady = items.length > 0 && items.every(item=>item.title && item.link && item.date && item.section);
  const ready = documentReady && Boolean(rowsReady || (result && Number(result[1].replaceAll(',','')) === 0));
  const rate = documentReady && !ready && body.match(/(?:刷新过于频繁|搜索间隔|搜索过于频繁)[\s\S]{0,80}?(\d+)\s*秒/);
  return {
    url:location.href, ready, documentReady, items,
    total:result ? Number(result[1].replaceAll(',','')) : null,
    keyword:document.querySelector('input[name="srchtxt"]')?.value || new URL(location.href).searchParams.get('kw') || '',
    links:[...document.querySelectorAll('.pg a[href]')].map(a=>a.href),
    expired:documentReady && !ready && /搜索已过期|搜索结果已过期|指定的搜索不存在|搜索结果不存在/.test(body),
    login:documentReady && !ready && /请先登录|需要先登录|登录后才能|尚未登录/.test(body),
    blocked:/just a moment|verify you are human|attention required/i.test(document.title),
    rateSeconds:rate ? Number(rate[1]) : 0,
    searchForm:Boolean(document.querySelector('form input[name="srchtxt"]'))
  };
})()"""


class SearchExpired(RuntimeError):
    pass


def search_url(value, page=None):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.path != '/search.php':
        raise ValueError('Expected a https://sehuatang.net/search.php URL')
    query = parse_qs(parsed.query)
    if query.get('mod', ['forum']) != ['forum']:
        raise ValueError('Expected a forum search URL')
    for key in ('kw', 'srchtxt'):
        if query.get(key) and query[key] != [KEYWORD]:
            raise ValueError('The fixed search keyword must be LTX')
    if page is not None:
        query['page'] = [str(page)]
    return urlunsplit(('https', parsed.netloc, parsed.path, urlencode(query, doseq=True), ''))


def next_page(state):
    current = int(parse_qs(urlsplit(state['url']).query).get('page', ['1'])[0])
    current_id = parse_qs(urlsplit(state['url']).query).get('searchid')
    later = {}
    for link in state['links']:
        parsed = urlsplit(link)
        query = parse_qs(parsed.query)
        page = query.get('page', [''])[0]
        if (parsed.scheme == 'https' and parsed.hostname in HOSTS and parsed.path == '/search.php'
                and query.get('searchid') == current_id and page.isdigit() and int(page) > current):
            later[int(page)] = search_url(link)
    if current+1 in later:
        return later[current+1]
    if later:
        raise RuntimeError('Pagination has a gap; results are incomplete')
    return ''


def wait_page(client, expected=None, timeout=30):
    deadline = time.monotonic()+timeout
    last = {}
    rate_retries = 0
    while time.monotonic() < deadline:
        last = client.evaluate(READ_PAGE)
        if expected and last.get('url', '').rstrip('#') != expected.rstrip('#'):
            time.sleep(0.4)
            continue
        if last.get('blocked') or last.get('login'):
            raise RuntimeError('Log in or complete verification in the same forum tab, then rerun')
        if last.get('expired'):
            raise SearchExpired('Forum search cache has expired')
        if last.get('rateSeconds'):
            if rate_retries >= 3:
                raise RuntimeError('Forum rate limit persists after three retries; rerun later to resume')
            rate_retries += 1
            wait = last['rateSeconds'] + 1
            print(f'Forum requests a pause: waiting {wait:g}s before retry {rate_retries}/3...', flush=True)
            while wait > 0:
                chunk = min(wait,30)
                time.sleep(chunk)
                wait -= chunk
            client.call('Page.navigate', {'url':last['url']})
            deadline = time.monotonic()+timeout
            time.sleep(0.4)
            continue
        if last.get('ready'):
            if last.get('keyword', '').strip().upper() != KEYWORD:
                raise ValueError('The open search is not for LTX')
            return last
        time.sleep(0.4)
    raise RuntimeError(f'Forum results did not load; URL={last.get("url", "unknown")}')


def start_search(client, timeout):
    # Use the site's own form and existing login; no cookies are exported/stored.
    acknowledged = client.evaluate(r"""(() => {
      const field=document.querySelector('input[name="srchtxt"]');
      const form=field?.form;
      if (!form) return false;
      field.value='LTX';
      window.setTimeout(()=>form.requestSubmit(),0);
      return true;
    })()""")
    if not acknowledged:
        raise RuntimeError('Open the forum search page in Chrome, then rerun')
    return wait_page(client, timeout=timeout)


def fresh_search(client, timeout):
    # Load the empty form first, so wait_page cannot accidentally accept the
    # old cached results while the new POST navigation is still pending.
    form_url = ORIGIN+'/search.php?mod=forum'
    client.call('Page.navigate', {'url':form_url})
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        state = client.evaluate(READ_PAGE)
        if state.get('url') == form_url and state.get('documentReady'):
            if state.get('blocked') or state.get('login'):
                raise RuntimeError('Log in or complete verification in the forum tab, then rerun')
            if state.get('searchForm'):
                return start_search(client, timeout)
        time.sleep(0.4)
    raise RuntimeError('The forum search form did not load')


def collect(client, store, initial_url=None, delay=3, timeout=30, max_pages=0, start_page=1):
    if start_page != 1:
        raise ValueError('Search must start from page 1')
    if initial_url is None and start_page == 1:
        state = fresh_search(client, timeout)
    else:
        current = initial_url or client.evaluate('location.href')
        try:
            url = search_url(current, page=start_page)
        except ValueError:
            url = ORIGIN+'/search.php?mod=forum'
        client.call('Page.navigate', {'url':url})
        try:
            state = wait_page(client, expected=url, timeout=timeout)
        except SearchExpired:
            state = start_search(client, timeout)
        except RuntimeError as exc:
            latest = client.evaluate(READ_PAGE)
            if latest.get('searchForm') and not latest.get('login') and not latest.get('blocked') and latest.get('total') is None:
                state = start_search(client, timeout)
            else:
                raise exc
    seen_pages, seen_threads, pages = set(), set(), 0
    expected_total = state.get('total') or 0
    first_page = int(parse_qs(urlsplit(state['url']).query).get('page',['1'])[0])
    if first_page != 1:
        raise RuntimeError('The forum did not return page 1; refusing to skip earlier results')
    while True:
        if max_pages and pages >= max_pages:
            raise RuntimeError('Page limit reached; the CSV contains partial results')
        url = state['url']
        if url in seen_pages:
            raise RuntimeError('Forum pagination repeated a page')
        seen_pages.add(url)
        fresh = 0
        for item in state['items']:
            tid = tid_from_link(item['link'])
            if not tid:
                raise RuntimeError('Could not read a search-result thread ID')
            if tid in store.existing_tids:
                store.counts['existing_tid'] += 1
                store.flush()
                print(f'Stopped at existing TID {tid} on page {pages+1}; no later items or pages will be searched.', flush=True)
                return pages+1
            if tid in seen_threads:
                continue
            seen_threads.add(tid)
            fresh += 1
            store.offer(item)
        store.flush()
        pages += 1
        print(f'Page {first_page+pages-1}: {len(state["items"])} results; saved {store.counts["added"]} new threads; excluded {store.counts["excluded_section"]}; existing {store.counts["existing_tid"]}', flush=True)
        if state['items'] and not fresh:
            raise RuntimeError('Pagination repeated the same thread list')
        following = next_page(state)
        if not following:
            if first_page == 1 and expected_total > len(seen_threads):
                raise RuntimeError('Pagination ended before all reported threads were read')
            return pages
        time.sleep(delay)
        client.call('Page.navigate', {'url':following})
        state = wait_page(client, expected=following, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', '-o', type=Path, default=Path('sehuatang_LTX.csv'))
    parser.add_argument('--date-range', '--date_range', help='Inclusive (YYYY-MM-DD,YYYY-MM-DD); omit or null for no date filter. Always stops at the first saved TID')
    parser.add_argument('--search-url', help='Optional LTX forum search URL; otherwise reuse the open tab')
    parser.add_argument('--delay', type=float, default=3, help='Seconds between pages; default 3 respects the observed refresh limit')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-pages', type=int, default=0, help='Optional limit; 0 = continue until a saved TID or the last page')
    parser.add_argument('--start-page', type=int, choices=[1], default=1, help='Search always starts at page 1')
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.max_pages < 0 or args.start_page < 1:
        parser.error('Invalid delay, timeout, or page limit')
    try:
        bounds=parse_date_range(args.date_range)
        store=ThreadCsv(args.csv.expanduser().resolve(),bounds)
        if args.search_url:
            search_url(args.search_url)
        client=ForumChromeClient()
        try:
            pages=collect(client,store,args.search_url,args.delay,args.timeout,args.max_pages,args.start_page)
        finally:
            store.flush()
            client.close()
        print(f'Complete: {pages} pages from requested page {args.start_page}; '+', '.join(f'{k}={v}' for k,v in store.counts.items()))
        print('CSV: '+str(store.path.resolve()))
    except (ValueError,RuntimeError,OSError,EOFError) as exc:
        parser.exit(1,f'INCOMPLETE: {exc}\n')


if __name__=='__main__':
    main()
