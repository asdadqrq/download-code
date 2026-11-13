#!/usr/bin/env python3
"""Search all requested exact codes before submitting to 115 via its MCP server."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import tomllib

from check_115_exists_mcp import NetdiskMCP, load_server_config
from cloud_download_115_csv import tool_json

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'bt4g_115_retry_report.json'
CODES = 'ODFM-022,HERX-015,RGD-260,HUNT-408,ARM-091,AD-213,AMAD-025,ONCE-007,ARM-055,MIRD-075,HAVD-730,DKSW-241,SILK-005,HUNT-321,RDD-006,SDMT-014,KIRD-142,KIRD-141,MXGS-231,KIFD-013,17ID-022,PYM-002,WIF-008,DVH-538,BUR-262,NHDT-824,IPSD-039,DANDY-139,DVH-532,AKB-011,SSPD-065,AKI-012,EZD-207,17ID-005,GSX-029,SSPD-060,CHU-027,WED-055'.split(',')
FOLDER = '3548255879277053465'


def save(report):
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    tmp = REPORT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    tmp.replace(REPORT)
    lines = []
    for row in report['rows']:
        match = row.get('selected') or {}
        lines.extend([row['code']+' '+row['status'], match.get('magnet', ''), ''])
    REPORT.with_name('bt4g_115_magnets.txt').write_text('\n'.join(lines))


def emit(row, message):
    print(f"{row['code']}: {message}", flush=True)


def search(bt, row, report):
    excluded = list(dict.fromkeys(a['hash'] for a in row['attempts']))
    emit(row, f"searching exact code; excluding {len(excluded)} tried hashes")
    result = tool_json(bt, 'bt4g_search_exact', {
        'codes': [row['code']], 'max_pages': 100, 'max_results': 1,
        'exclude_hashes': excluded, 'include_titles': True,
    })
    if not result.get('results'):
        row['status'] = 'search_incomplete'
        row['error'] = result.get('error', 'No search response')
    else:
        item = result['results'][0]
        row['search_history'].append(item)
        if item.get('matches'):
            row['selected'] = item['matches'][0]
            row['status'] = 'ready'
            row.pop('downloading_since', None)
            row.pop('task', None)
            row.pop('retry_reason', None)
            row.pop('error', None)
            emit(row, 'verified '+row['selected']['magnet'])
        elif item['status'] == 'not_found':
            row['selected'] = None
            row['status'] = 'exhausted' if excluded else 'no_exact_match'
            emit(row, row['status'])
        else:
            row['status'] = 'search_incomplete'
            row['error'] = item.get('errors', item['status'])
            emit(row, 'search inconclusive: '+item['status'])
    save(report)


def task_pages(net, wanted):
    found = {}
    page = 1
    while True:
        result = tool_json(net, 'offline_tasks_115', {'page': page})
        for task in result.get('tasks', []):
            digest = task.get('info_hash', '').lower()
            if digest in wanted:
                found.setdefault(digest, task)
        if wanted <= found.keys() or page >= int(result.get('page_count', 1)):
            return found
        page += 1
        if page > 1000:
            raise RuntimeError('115 task pagination exceeded 1000 pages')


def reconcile(net, report, downloading_timeout=None):
    active = [r for r in report['rows'] if r['status'] != 'completed'
              and (r.get('selected') or r.get('attempts'))]
    if not active:
        return
    wanted = {a['hash'] for r in active for a in r.get('attempts', [])}
    wanted.update(r['selected']['hash'] for r in active if r.get('selected'))
    tasks = task_pages(net, wanted)
    for row in active:
        # A previously timed-out candidate can finish later. Preserve that success.
        candidates = ([row['selected']] if row.get('selected') else []) + row.get('attempts', [])
        completed = next((a for a in candidates if tasks.get(a['hash'], {}).get('status') == 2
                          and str(tasks[a['hash']].get('folder_id')) == FOLDER), None)
        if completed:
            row['selected'] = {k: completed[k] for k in ('hash','magnet','source_url','title') if k in completed}
            row['task'] = tasks[completed['hash']]
            row['status'] = 'completed'
            emit(row, '115 completed (verified task destination)')
            continue
        if not row.get('selected') or row['status'] in ('exhausted','retry_limit_reached','search_incomplete','failed'):
            continue
        task = tasks.get(row['selected']['hash'])
        if not task:
            if row['status'] not in ('ready',):
                row['status'] = 'submission_unconfirmed'
            continue
        row['task'] = task
        if task['status'] == -1:
            row['status'] = 'failed'
        elif str(task.get('folder_id')) != FOLDER:
            row['status'] = 'different_folder'
        elif task['status'] == 2:
            row['status'] = 'completed'
        elif task['status'] == 1:
            row['status'] = 'downloading'
            row.setdefault('downloading_since', time.time())
            if downloading_timeout is not None and time.time() - row['downloading_since'] >= downloading_timeout:
                row['status'] = 'failed'
                row['retry_reason'] = f'Still downloading after {downloading_timeout:g} seconds (user retry rule)'
        else:
            row['status'] = 'queued'
        if row['status'] == 'failed' and not any(a['hash'] == row['selected']['hash'] for a in row['attempts']):
            row['attempts'].append({**row['selected'], 'submission': 'already_failed_in_115', 'task': task})
        if row['status'] == 'failed':
            for attempt in row['attempts']:
                if attempt['hash'] == row['selected']['hash']:
                    attempt['outcome'] = row.get('retry_reason', '115 task failed')
                    attempt['task'] = task
        emit(row, '115 '+row['status']+f" ({task.get('percent_done',0)}%)")
    save(report)


def submit(net, row, report):
    selected = row['selected']
    receipt = {**selected, 'submission': 'attempting'}
    row['attempts'].append(receipt)
    row['status'] = 'submission_unconfirmed'
    save(report)  # Receipt before mutation makes restarts safe.
    try:
        res = net.request('tools/call', {'name': 'offline_download', 'arguments': {
            'magnet_links': [selected['magnet']], 'target_path': FOLDER,
        }})
        output = '\n'.join(c.get('text', '') for c in res.get('content', []) if c.get('type') == 'text')
        receipt['output'] = output
        receipt['submission'] = 'helper_error' if res.get('isError') else 'accepted'
        row['status'] = 'submission_unconfirmed' if res.get('isError') else 'submitted'
        emit(row, receipt['submission'])
    except Exception as exc:
        receipt['submission'] = 'unconfirmed'
        receipt['error'] = str(exc)
        emit(row, 'submission unconfirmed; will reconcile before any retry')
    save(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--search-only', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--downloading-timeout', type=float)
    parser.add_argument('--max-retries', type=int, default=3)
    args = parser.parse_args()
    if args.max_retries < 0:
        parser.error('--max-retries must not be negative')
    config_path = Path.home()/'.codex/config.toml'
    config = tomllib.loads(config_path.read_text())['mcp_servers']
    if REPORT.exists():
        report = json.loads(REPORT.read_text())
        assert report['folder_id'] == FOLDER
        assert [r['code'] for r in report['rows']] == CODES
    else:
        report = {'folder_id': FOLDER, 'rows': [
            {'code': c, 'status': 'pending_search', 'search_history': [], 'attempts': []} for c in CODES]}
    bt = NetdiskMCP(config['bt4g'], timeout=600)
    report['max_retries_per_code'] = args.max_retries
    report['downloading_timeout_seconds'] = args.downloading_timeout
    try:
        # Complete the initial search for every code before the first 115 submission.
        if not args.check_only:
            for row in report['rows']:
                if row['status'] in ('pending_search', 'search_incomplete'):
                    search(bt, row, report)
            report['initial_search_finished'] = True
            save(report)
        if args.search_only:
            print('INITIAL_SEARCH_FINISHED',dict(Counter(r['status'] for r in report['rows'])),flush=True)
            return
        net = NetdiskMCP(load_server_config(config_path), timeout=120)
        try:
            reconcile(net, report, args.downloading_timeout)
            if args.downloading_timeout is not None and not args.check_only:
                if any(r['status'] in ('downloading','queued','submitted') for r in report['rows']):
                    time.sleep(args.downloading_timeout)
                    reconcile(net, report, args.downloading_timeout)
            if not args.check_only:
                for round_no in range(args.rounds):
                    for row in report['rows']:
                        if row['status'] == 'failed':
                            tried = len({a['hash'] for a in row['attempts']})
                            if tried >= args.max_retries + 1:
                                row['status'] = 'retry_limit_reached'
                                emit(row, f'stopped: {args.max_retries} retries reached')
                                save(report)
                                continue
                            search(bt, row, report)
                        if row['status'] == 'ready':
                            submit(net, row, report)
                    reconcile(net, report, args.downloading_timeout)
                    if args.downloading_timeout is not None:
                        if any(r['status'] in ('downloading','queued','submitted') for r in report['rows']):
                            time.sleep(args.downloading_timeout)
                            reconcile(net, report, args.downloading_timeout)
                    print('ROUND',round_no+1,dict(Counter(r['status'] for r in report['rows'])),flush=True)
                    pending_states = ('failed','downloading','queued','submitted') if args.downloading_timeout is not None else ('failed',)
                    if not any(r['status'] in pending_states for r in report['rows']):
                        break
        finally:
            net.close()
    finally:
        bt.close()
    save(report)
    print('SUMMARY',dict(Counter(r['status'] for r in report['rows'])),flush=True)


if __name__ == '__main__':
    main()
