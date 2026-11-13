"""Verify requested file bytes and organize existing copies; never upload files."""
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib, json, sys, unicodedata

OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(OUT.parent.parent))
from check_115_exists_mcp import NetdiskMCP, load_server_config
PARENT='3550427032766645270'
SECRET=Path('/tmp/115-auth-check-organize-20261111')
cookie=SECRET.read_text().strip()
SECRET.unlink()
config=load_server_config(Path('/Users/fangdingding/.codex/config.toml'))
config['env']={**config.get('env',{}),'NETDISK_115_COOKIE':cookie}
config['env_vars']=[key for key in config.get('env_vars',[]) if key!='NETDISK_115_COOKIE']
client=NetdiskMCP(config,timeout=60)
receipt=OUT/'results.json'
if receipt.exists():
 client.close()
 raise SystemExit('Existing receipts must be reconciled before rerunning.')
report={'parent_id':PARENT,'checked_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'rows':[]}

def save():
 tmp=receipt.with_suffix('.tmp')
 tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2))
 tmp.replace(receipt)

def api(path,params,post=False):
 encoded=urlencode(params)
 url='https://webapi.115.com'+path+('' if post else '?'+encoded)
 request=Request(url,data=encoded.encode() if post else None,headers={
  'Cookie':cookie,'Referer':'https://115.com/','User-Agent':'Mozilla/5.0',
  'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'})
 with urlopen(request,timeout=60) as response:data=json.load(response)
 if not data.get('state'):
  raise RuntimeError('115 request failed: '+str(data.get('error') or data.get('error_msg') or data.get('errno')))
 return data

def listing(folder):
 items=[];offset=0
 while True:
  data=api('/files',{'cid':folder,'aid':1,'offset':offset,'limit':1000,'show_dir':1,'o':'file_name','asc':1})
  path=[str(part.get('cid')) for part in data.get('path',[])]
  if str(folder) not in path or PARENT not in path:
   raise RuntimeError('Folder listing is outside the authorized parent')
  page=data.get('data',[])
  items.extend(page)
  offset+=len(page)
  if offset>=int(data.get('count',len(items))):return items
  if not page:raise RuntimeError('Incomplete folder listing')

def create(name):
 result=client.request('tools/call',{'name':'create_folder_115','arguments':{'name':name,'parent_path':PARENT}})
 text='\n'.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
 if result.get('isError'):raise RuntimeError(text)
 folder=result.get('structuredContent') or json.loads(text)
 if str(folder['parent_id'])!=PARENT or folder['name']!=name:raise RuntimeError('Unexpected folder returned')
 return folder

def hash_match(item,row):
 return bool(item.get('fid')) and int(item.get('s',-1))==row['size'] and str(item.get('sha','')).upper()==row['sha1']

try:
 # Establish authenticated access before hashing and before any mutation.
 listing(PARENT)
 downloads=Path('/Users/fangdingding/Downloads')
 for code,name in json.loads((OUT/'requested_files.json').read_text()):
  path=downloads/name
  row={'code':code,'local_path':str(path),'folder_name':code+'-U','status':'checking','searches':[]}
  report['rows'].append(row)
  if not path.is_file():
   choices=[p for p in downloads.iterdir() if unicodedata.normalize('NFC',p.name)==unicodedata.normalize('NFC',name)]
   if len(choices)==1 and choices[0].is_file():path=choices[0];row['local_path']=str(path)
   else:
    row['status']='local_missing';save();print(code+': local file missing',flush=True);continue
  before=path.stat();sha=hashlib.sha1()
  with path.open('rb') as handle:
   for block in iter(lambda:handle.read(8*1024*1024),b''):sha.update(block)
  after=path.stat()
  if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
   row['status']='local_changed';save();print(code+': local file changed while hashing',flush=True);continue
  row.update(size=after.st_size,sha1=sha.hexdigest().upper())
  cloud={}
  for query in [code,code.replace('-','_'),code.replace('-','')]:
   offset=0
   while True:
    result=client.search_115(query,PARENT,offset,1000)
    row['searches'].append({'query':query,'offset':offset,'total':result['total'],'has_more':result['has_more']})
    cloud.update({item['id']:item for item in result['items'] if item['type']=='file'})
    if not result['has_more']:break
    offset=result['next_offset']
   if any(item['size']==row['size'] and item['sha1'].upper()==row['sha1'] for item in cloud.values()):break
  row['cloud_candidates']=list(cloud.values())
  row['exact_matches']=[item for item in cloud.values() if item['size']==row['size'] and item['sha1'].upper()==row['sha1']]
  row['status']='exact_copy_found' if row['exact_matches'] else ('different_cloud_version' if cloud else 'not_found')
  save();print(code+': '+row['status'],flush=True)
 # Organize only byte-identical cloud files already verified in this parent.
 for row in report['rows']:
  if row['status']!='exact_copy_found':continue
  try:
   row['status']='creating_folder';save()
   folder=create(row['folder_name']);row['folder']=folder
   destination=listing(folder['id'])
   identical=[item for item in destination if hash_match(item,row)]
   if identical:
    row.update(status='already_organized',organized_file_id=str(identical[0]['fid']));save()
    print(row['folder_name']+': already organized and hash verified',flush=True);continue
   source=next((item for item in row['exact_matches'] if item['parent_id']==PARENT),row['exact_matches'][0])
   sources=[item for item in listing(source['parent_id']) if str(item.get('fid'))==source['id'] and hash_match(item,row)]
   if len(sources)!=1:raise RuntimeError('Source metadata changed before move')
   row.update(status='moving',source_file_id=source['id'],source_parent_id=source['parent_id']);save()
   response=api('/files/move',{'pid':folder['id'],'fid[0]':source['id']},post=True)
   row['move_response']={key:response[key] for key in ('state','errno','error') if key in response};save()
   actual=[item for item in listing(folder['id']) if str(item.get('fid'))==source['id'] and hash_match(item,row)]
   if len(actual)!=1:raise RuntimeError('Move not yet confirmed by direct destination listing')
   row.update(status='organized',organized_file_id=source['id']);save()
   print(row['folder_name']+': moved and hash verified',flush=True)
  except Exception as exc:
   row.update(status='blocked',error=str(exc));save()
   print(row['code']+': blocked; receipt saved, mutation will not be repeated',flush=True)
finally:
 client.close()
 save()
 lines=['# 115 file check and organization','','Parent folder: `'+PARENT+'`','',
        'Cloud copies were verified against local byte sizes and SHA-1 hashes. Only exact copies were organized.','',
        '| Code | Exact uploaded copy | Organization | Folder ID |','|---|---|---|---|']
 for row in report['rows']:
  found='Yes' if row.get('exact_matches') else 'Not confirmed'
  lines.append('| '+row['code']+' | '+found+' | '+row['status']+' | '+str(row.get('folder',{}).get('id',''))+' |')
 lines.append('')
 (OUT/'report.md').write_text('\n'.join(lines))
