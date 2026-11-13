#!/usr/bin/env python3
"""Read completed download directories using their 115 task file IDs."""
from __future__ import annotations

import json
from pathlib import Path

from check_115_exists_mcp import NetdiskMCP, load_server_config

ROOT = Path(__file__).resolve().parent
REPORT = ROOT/'bt4g_115_retry_report.json'
OUTPUT = ROOT/'bt4g_115_success_titles.json'

def list_task_directory(client, folder_id: str, depth: int = 0, visited: set | None = None) -> list[dict]:
    """List exact completed-task directory contents; use IDs to traverse, never code-name filters."""
    if depth > 8:
        raise RuntimeError('Completed download folder nesting exceeds the 8-level safety limit')
    visited = visited if visited is not None else set()
    if not folder_id.isdigit() or folder_id in visited:
        raise RuntimeError('Invalid or repeated 115 task folder ID')
    visited.add(folder_id)
    result = client.request('tools/call', {'name':'list', 'arguments':{'cloud':'115','path':folder_id}})
    if result.get('isError'):
        text='\n'.join(x.get('text','') for x in result.get('content',[]) if x.get('type')=='text')
        raise RuntimeError(text or '115 folder listing failed')
    text='\n'.join(x.get('text','') for x in result.get('content',[]) if x.get('type')=='text')
    entries=[]
    for line in text.splitlines():
        kind='dir' if '[dir] ' in line else 'file' if '[file] ' in line else ''
        if not kind: continue
        tail=line.split('[dir] ' if kind=='dir' else '[file] ',1)[1]
        marker=' (ID: '
        if marker not in tail or not tail.endswith(')'): continue
        name,identity=tail.rsplit(marker,1)
        identity=identity[:-1]
        if not identity.isdigit(): continue
        entry={'id':identity,'name':name,'type':kind,'parent_id':folder_id}
        entries.append(entry)
        if kind=='dir':
            entries.extend(list_task_directory(client,identity,depth+1,visited))
    return entries


def main():
    report = json.loads(REPORT.read_text())
    saved = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    client = NetdiskMCP(load_server_config(Path.home()/'.codex/config.toml'), timeout=120)
    try:
        for row in report['rows']:
            if row['status'] != 'completed' or row['code'] in saved:
                continue
            root_id = row.get('task',{}).get('file_id')
            try:
                files=list_task_directory(client,str(root_id)) if root_id else []
                names=[x['name'] for x in files] or [row['selected'].get('title',row['code'])+' (BT4G title; destination listing unavailable)']
                listing_error=''
            except (RuntimeError,OSError,ValueError) as exc:
                files=[]
                names=[row['selected'].get('title',row['code'])+' (BT4G title; destination listing unavailable)']
                listing_error=str(exc)
            saved[row['code']] = {'titles':names,
                'files':files,
                'magnet':row['selected']['magnet'],'source_url':row['selected']['source_url'],
                'task_file_id':root_id,'listing_error':listing_error,
                'title_source':'completed 115 task folder' if files else 'verified BT4G title fallback'}
            OUTPUT.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n')
            print(row['code']+': '+str(len(names))+' successful destination title(s)',flush=True)
    finally:
        client.close()


if __name__=='__main__':
    main()
