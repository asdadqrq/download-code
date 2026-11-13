import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from bt4g_workflow import MCPBackend
p=Path('outputs/batch-20261111');r=json.loads((p/'receipts_v2.json').read_text());b=MCPBackend();saved={};cache={}
def walk(fid,seen):
 if fid in seen: raise RuntimeError('Repeated folder ID')
 seen.add(fid)
 if fid in cache:return cache[fid]
 d=b.call('netdisk','list',{'cloud':'115','path':fid});text=d.get('text','');entries=[]
 for line in text.splitlines():
  kind='dir' if '[dir] ' in line else 'file' if '[file] ' in line else None
  if not kind:continue
  tail=line.split('['+kind+'] ',1)[1]
  if ' (ID: ' not in tail or not tail.endswith(')'):continue
  name,identity=tail.rsplit(' (ID: ',1);identity=identity[:-1]
  entries.append({'name':name,'id':identity,'type':kind,'parent_id':fid})
  if kind=='dir':entries.extend(walk(identity,seen))
 if not entries:raise RuntimeError('Folder listing unavailable or empty: '+text[:200])
 cache[fid]=entries;return entries
try:
 for row in r['rows']:
  if row['status']!='completed':continue
  fid=str(row['task'].get('file_id',''));item={'file_id':fid}
  try:item['entries']=walk(fid,set())
  except Exception as e:item['error']=str(e);item['fallback_title']=row['selected']['title']
  saved[row['code']]=item
  (p/'destination_files.json').write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n')
 print('Listed',len(saved),'completed tasks; unavailable',sum('error' in x for x in saved.values()))
finally:b.close()
report=p/'final_results_v2.md'
s=report.read_text()+'\n## Completed destination files\n\n'
for code,item in saved.items():
 names=[e['name'] for e in item.get('entries',[]) if e['type']=='file']
 s+='- '+code+': '+('; '.join(names) if names else item.get('fallback_title','')+' (BT4G title fallback; folder listing unavailable)')+'\n'
report.write_text(s)
