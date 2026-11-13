import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import bt4g_workflow as w
o=Path(__file__).parent;b=json.loads((o/'candidates.json').read_text())
# Manually selected candidate ordinals after reading all saved titles.
s='STREET-121:- MILD-895:123 MILD-889:23 MILD-883:23 MILD-877:123 MILD-836:123 MILD-823:123 MILD-815:123 MILD-809:123 MILD-804:123 MILD-802:123 MILD-799:123 MILD-794:123 MILD-788:13 MILD-782:123 MILD-778:123 MILD-775:123 MILD-764:123 MILD-759:123 MILD-742:1 MILD-738:123 MILD-726:23 MILD-718:123 MILD-714:123 MILD-707:123 MILD-704:23 MILD-698:23 MILD-692:1 MILD-680:123 MILD-670:123 MILD-664:12 MILD-659:123 MILD-656:123 SERO-023:123 SPPR-004:1 OFM-003:123 RKI-076:123 MDB-213:123 MIAD-458:123 SMA-485:123 SMA-475:123 SDMT-094:123 DVDES-305:- VSPDS-485:123 CRIM-005:123 FSET-252:123 CWM-098:123 PMP-114:123 KAZ-036:123 MDB-194:123 HAVD-671:12 SDMT-041:123 UPSM-047:123 CWM-096:12 FTA-123:123 LABS-07:1 SDMT-024:1 WSS-121:123 FSET-241:123 POR-002:- EPT-006:123 ERH-082:123 VICD-180:123 KAZ-006:1 PMS-158:123 MDB-176:123 FTA-111:123 FTA-110:1 BDSR-011:12 OAJ-008:123 HAVD-632:123 SAMA-166:123 PMH-183:123 SDMS-868:1 REAL-363:2 EZD-278:123 OKAD-250:123 DBT-019:123 PBD-381:1 BMW-175:123 BMW-162:1 BMW-140:- MILD-827:123 MILD-694:123 RKI-099:- NIT-054:1'
selected=dict(x.split(':') for x in s.split())
reasons={'STREET-121':'Title identifies MGT-121 and street prose, not STREET-121.','MILD-788':'Candidate identifies MILD-962; its separate 788 index is not MILD-788.','DVDES-305':'Collection lists DVDES-650 and JUFD-305, not DVDES-305.','LABS-07':'Topaz Labs software ZIP is unrelated to the requested video.','SDMT-024':'Collection lists NLD-024 and SDMT-840/860, not SDMT-024.','POR-002':'These are television episodes with Spanish prose and episode numbers, not POR-002.','FTA-110':'ZIP archive is not a verified video release candidate.','BDSR-011':'Candidate identifies BDSR-207, not BDSR-011.','REAL-363':'Candidate identifies ABP-363; real estate prose does not make it REAL-363.','BMW-140':'Number 140 and a BMW group description do not establish BMW-140 release identity.','RKI-099':'Collection lists RKI-023 and PDV-099, not RKI-099.'}
ds=[]
for row in b['rows']:
 if not row['candidates']:continue
 if row['code'] not in selected:raise ValueError('Missing manual decisions '+row['code'])
 keep=selected[row['code']];rank=0
 for i,c in enumerate(row['candidates'],1):
  yes=str(i) in keep
  if yes:rank+=1
  reason=('The title identifies '+row['code']+' as the release; source labels, file extensions and descriptive wording preserve that identifier.') if yes else reasons.get(row['code'],'The unexplained extra -C variant leaves exact release identity uncertain; other candidates name the requested code without it.')
  ds.append({'code':row['code'],'candidate_id':c['candidate_id'],'decision':'accept' if yes else 'reject','reason':reason,'rank':rank if yes else None})
v={'candidate_digest':w.digest(b),'method':'language_model','reviewer':'Codex language model','decisions':ds};w.save_json(o/'review_decisions.json',v);w.apply_review(o,o/'review_decisions.json');print('reviewed',len(ds),'accepted',sum(d['decision']=='accept' for d in ds))
