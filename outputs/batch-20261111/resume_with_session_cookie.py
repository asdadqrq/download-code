import sys,runpy,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import bt4g_workflow as w
cookie=sys.stdin.readline().strip()
if not cookie:raise RuntimeError('Missing session cookie')
original=w.MCPBackend.__init__
def session_backend(self):
 original(self)
 self.config['netdisk'].setdefault('env',{})['NETDISK_115_COOKIE']=cookie
w.MCPBackend.__init__=session_backend
b=w.MCPBackend()
try:
 print(b.call('netdisk','health',{}),flush=True)
 report=w.transfer(b,Path('outputs/batch-20261111'),'3559805669559239927')
 from collections import Counter
 print('Final statuses:',dict(Counter(x['status'] for x in report['rows'])),flush=True)
finally:b.close()
runpy.run_path('outputs/batch-20261111/list_completed.py',run_name='__main__')
