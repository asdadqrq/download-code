"""Read only the exact completed tasks' folders for this batch's final report."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent))
from bt4g_workflow import MCPBackend, save_json


def main():
    receipts = json.loads((ROOT / 'receipts_v2.json').read_text())
    backend = MCPBackend()
    cache = {}
    records = []
    try:
        for row in receipts['rows']:
            if row['status'] != 'completed':
                continue
            record = {'code': row['code'], 'info_hash': row['selected']['hash'],
                      'task_file_id': str(row['task'].get('file_id', '')),
                      'files': [], 'listings': [], 'errors': []}
            pending = [(record['task_file_id'], '')]
            visited = set()
            while pending:
                folder_id, prefix = pending.pop(0)
                if not folder_id or folder_id == '0':
                    record['errors'].append('Completed task has no usable file_id folder.')
                    continue
                if folder_id in visited:
                    continue
                visited.add(folder_id)
                if folder_id not in cache:
                    try:
                        cache[folder_id] = backend.call('netdisk', 'list',
                            {'cloud': '115', 'path': folder_id})
                    except Exception as exc:
                        cache[folder_id] = {'listing_error': str(exc)}
                listing = cache[folder_id]
                record['listings'].append({'folder_id': folder_id, 'path': prefix, 'response': listing})
                if listing.get('listing_error'):
                    record['errors'].append(listing['listing_error'])
                    continue
                text = listing.get('text', '')
                entries = []
                for line in text.splitlines():
                    # Parse the connector's presentation format only. Every
                    # child belongs to this hash's task, regardless of its name.
                    match = re.fullmatch(r'\d+\. \[(dir|file)\] (.*) \(ID: (\d+)\)', line)
                    if match:
                        entries.append(match.groups())
                if not entries:
                    record['errors'].append('Folder could not be listed or was empty: ' + folder_id)
                if len(entries) >= 1000:
                    record['errors'].append('Connector listing may be truncated at 1000 entries: ' + folder_id)
                for kind, name, entry_id in entries:
                    full_name = prefix + name
                    if kind == 'dir':
                        pending.append((entry_id, full_name + '/'))
                    else:
                        record['files'].append(full_name)
            records.append(record)
            save_json(ROOT / 'destination_files.json', {'rows': records})
            print(row['code'] + ': ' + str(len(record['files'])) + ' destination file(s)', flush=True)
    finally:
        backend.close()
    report_path = ROOT / 'final_results_v2.md'
    report = report_path.read_text()
    section = ['\n## Files in the completed tasks\n',
               'Names below come from each completed task’s exact `file_id` folder and child folders.\n']
    for record in records:
        section.append('### ' + record['code'] + '\n')
        section.extend('- ' + name.replace('\n', ' ') for name in record['files'])
        if record['errors']:
            fallback = next(row['selected']['title'] for row in receipts['rows'] if row['code'] == record['code'])
            section.append('\nListing incomplete/unavailable: ' + '; '.join(record['errors']))
            section.append('Fallback reviewed BT4G title: ' + fallback)
        section.append('')
    report_path.write_text(report + '\n'.join(section) + '\n')


if __name__ == '__main__':
    main()
