#!/usr/bin/env python3
"""Submit CSV torrent_hash magnets through netdisk MCP and report 115 task status."""
from __future__ import annotations
import argparse
import base64
from collections import Counter
import csv
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from check_115_exists_mcp import NetdiskMCP, load_server_config


def normalize_magnet(value):
    value = (value or '').strip()
    if re.fullmatch(r'[a-fA-F0-9]{40}', value):
        value = 'magnet:?xt=urn:btih:' + value
    parsed = urlsplit(value)
    if parsed.scheme != 'magnet':
        raise ValueError('Missing or invalid torrent_hash')
    hashes = [v[9:] for v in parse_qs(parsed.query).get('xt', []) if v.lower().startswith('urn:btih:')]
    if len(hashes) != 1:
        raise ValueError('Expected exactly one BitTorrent hash')
    digest = hashes[0]
    if re.fullmatch(r'[A-Z2-7a-z2-7]{32}', digest):
        digest = base64.b32decode(digest.upper()).hex()
    if not re.fullmatch(r'[a-fA-F0-9]{40}', digest):
        raise ValueError('Invalid BitTorrent hash')
    return value, digest.lower()


def read_input(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'code', 'torrent_hash'}.issubset(reader.fieldnames or []):
            raise ValueError('CSV must contain code and torrent_hash columns')
        rows = []
        for index, row in enumerate(reader, 2):
            item = {'csv_row':index, 'code':row['code'].strip(), 'submission':'not_submitted'}
            try:
                item['magnet'], item['info_hash'] = normalize_magnet(row['torrent_hash'])
            except ValueError as exc:
                item.update(status='invalid_input', error=str(exc))
            rows.append(item)
    return rows


def tool_json(client, name, arguments):
    result = client.request('tools/call', {'name':name, 'arguments':arguments})
    text = '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')
    if result.get('isError'):
        raise RuntimeError(text)
    return json.loads(text)


def all_tasks(client):
    tasks = {}
    page = 1
    while True:
        data = tool_json(client, 'offline_tasks_115', {'page':page})
        for task in data['tasks']:
            tasks.setdefault(task['info_hash'], task)
        if page >= data['page_count']:
            return tasks
        page += 1
        if page > 1000:
            raise RuntimeError('Offline task pagination exceeded safety limit')


def task_status(task, folder_id):
    if task is None:
        return 'not_found_in_task_list'
    # A failed hash remains a failed download even if 115 retained an older
    # task's destination when the same magnet was submitted again.
    if task['status'] == -1:
        return 'failed'
    if task['folder_id'] != folder_id:
        return 'different_folder'
    return {-1:'failed',1:'downloading',2:'completed'}.get(task['status'],'queued_or_unknown')


def save_report(path, folder_id, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'folder_id':folder_id, 'rows':rows}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    codes = list(dict.fromkeys(
        [row['code'] for row in rows if row.get('status') == 'downloading']
        + [row['code'] for row in rows if row.get('status') in ('failed','invalid_input')]
    ))
    path.with_name(path.stem + '_merged_codes.txt').write_text(
        ','.join(codes) + ('\n' if codes else ''), encoding='utf-8',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--folder-id', required=True, help='115 destination directory ID')
    parser.add_argument('--config', type=Path, default=Path.home()/'.codex/config.toml')
    parser.add_argument('--wait', type=float, default=30)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--report', type=Path, default=Path('115_cloud_download_report.json'))
    parser.add_argument('--check-only', action='store_true', help='Read statuses without submitting downloads')
    parser.add_argument('--limit', type=int, help='Optional number of CSV rows to process')
    parser.add_argument('--skip-check', action='store_true', help='Submit and save receipts without waiting/checking')
    args = parser.parse_args()
    if not args.folder_id.isdigit() or args.wait < 0 or not 1 <= args.batch_size <= 20 or (args.limit is not None and args.limit < 1):
        parser.error('Invalid folder ID, wait, batch size, or limit')
    rows = read_input(args.csv)
    if args.limit:
        rows = rows[:args.limit]
    # Reuse receipts after interruption. Never resubmit an ambiguous/accepted call.
    if args.report.exists():
        previous = json.loads(args.report.read_text(encoding='utf-8'))
        if previous.get('folder_id') != args.folder_id:
            raise SystemExit('Existing report targets another folder; choose a different --report')
        receipts = {r.get('info_hash'):r for r in previous['rows'] if r.get('info_hash')}
        for row in rows:
            old = receipts.get(row.get('info_hash'))
            if old:
                for key in ('submission','submission_output','status'):
                    if key in old: row[key] = old[key]
    unique = {}
    for row in rows:
        if row.get('info_hash'):
            unique.setdefault(row['info_hash'], row)
    print(f'CSV rows: {len(rows)}; unique valid magnets: {len(unique)}; folder: {args.folder_id}', flush=True)
    client = NetdiskMCP(load_server_config(args.config.expanduser()), timeout=120)
    try:
        if not args.check_only:
            pending = [r for r in unique.values() if r['submission'] == 'not_submitted']
            for start in range(0, len(pending), args.batch_size):
                batch = pending[start:start + args.batch_size]
                hashes = {r['info_hash'] for r in batch}
                for row in rows:
                    if row.get('info_hash') in hashes: row['submission'] = 'attempting'
                save_report(args.report, args.folder_id, rows)
                print(f'Submitting {start + 1}-{start + len(batch)}/{len(pending)}...', flush=True)
                try:
                    response = client.request('tools/call', {'name':'offline_download', 'arguments':{
                        'magnet_links':[r['magnet'] for r in batch], 'target_path':args.folder_id,
                    }})
                    output = '\n'.join(c.get('text','') for c in response.get('content',[]) if c.get('type')=='text')
                    state = 'unconfirmed' if response.get('isError') else 'accepted_by_helper'
                    for row in rows:
                        if row.get('info_hash') in hashes:
                            row.update(submission=state, submission_output=output)
                    print(f'  Helper: {state}', flush=True)
                    if response.get('isError'):
                        print(output[:1500], flush=True)
                        # A batch can partially succeed. Do not blindly retry it;
                        # keep its receipt and reconcile each hash with task status.
                except (OSError, RuntimeError) as exc:
                    save_report(args.report, args.folder_id, rows)
                    raise
                save_report(args.report, args.folder_id, rows)
                if start + len(batch) < len(pending): time.sleep(1)
        if args.skip_check:
            return
        print(f'Waiting {args.wait:g} seconds before checking...', flush=True)
        remaining = args.wait
        while remaining > 0:
            chunk = min(remaining, 30)
            time.sleep(chunk)
            remaining -= chunk
        tasks = all_tasks(client)
        for row in rows:
            if not row.get('info_hash'): continue
            task = tasks.get(row['info_hash'])
            row['status'] = task_status(task, args.folder_id)
            if task is not None:
                row['task'] = task
                row['destination_matches'] = task['folder_id'] == args.folder_id
        save_report(args.report, args.folder_id, rows)
        print('RESULTS ' + json.dumps(dict(Counter(r.get('status','unconfirmed') for r in rows))), flush=True)
        print('FAILED CODES: ' + ','.join(dict.fromkeys(r['code'] for r in rows if r.get('status') in ('failed','invalid_input'))), flush=True)
        print('DOWNLOADING CODES: ' + ','.join(dict.fromkeys(r['code'] for r in rows if r.get('status') == 'downloading')), flush=True)
        print('Report: ' + str(args.report.resolve()), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
