import sys,json,time,os,uuid
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import bt4g_workflow as w
out=Path(__file__).parent
class Bridge:
 def __init__(self):self.local=w.MCPBackend()
 def call(self,server,tool,args):
  if server=='netdisk':return self.local.call(server,tool,args)
  n=uuid.uuid4().hex;p=out/'bridge_request.json';ans=out/'bridge_response.json'
  if ans.exists():ans.unlink()
  w.save_json(p,{'id':n,'server':server,'tool':tool,'args':args})
  deadline=time.time()+900
  while time.time()<deadline:
   if ans.exists():
    d=json.loads(ans.read_text())
    if d.get('id')==n:
     if 'error' in d:raise RuntimeError(d['error'])
     return d['data']
   time.sleep(.1)
  raise RuntimeError('Bridge timed out')
try:w.transfer(Bridge(),out,'2085196368406718756')
finally:
 p=out/'receipts_v2.json'
 if p.exists():w.write_reports(out,json.loads(p.read_text()))
 (out/'bridge_done').write_text('done')
