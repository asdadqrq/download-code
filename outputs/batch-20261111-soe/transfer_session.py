import sys,runpy,copy
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from check_115_exists_mcp import NetdiskMCP
cookie=sys.stdin.readline().strip()
assert cookie
original=NetdiskMCP.__init__
def session_init(self,config,*args,**kwargs):
 config=copy.deepcopy(config)
 if 'NETDISK_115_COOKIE' in config.get('env',{}):config['env']['NETDISK_115_COOKIE']=cookie
 original(self,config,*args,**kwargs)
NetdiskMCP.__init__=session_init
sys.argv=['run_workflow.py','transfer','--output-dir','outputs/batch-20261111-soe','--folder-id','3559818274415838212']
runpy.run_path('/Users/fangdingding/.codex/skills/bt4g-to-115/scripts/run_workflow.py',run_name='__main__')
