"""Round-based 115 batching: one chosen magnet per eligible code per round."""
import json
import time

from bt4g_workflow import (digest, validate_review, save_json, video_bytes,
                          MIN_VIDEO_BYTES, task_snapshot, completed_attempt, write_reports)


def transfer_batch(backend, output, folder, *, clock=time.time, sleep=time.sleep):
    if not folder.isdigit():
        raise ValueError('An explicit destination folder ID is required')
    batch=json.loads((output/'candidates.json').read_text())
    review=json.loads((output/'review.json').read_text())
    decisions=validate_review(batch,review)
    path=output/'receipts_v2.json'
    report=json.loads(path.read_text()) if path.exists() else {
        'version':2,'folder_id':folder,'candidate_digest':digest(batch),'review_digest':digest(review),
        'rows':[{'code':r['code'],'status':'pending','attempts':[],'candidate_outcomes':{}} for r in batch['rows']]}
    if (report['folder_id']!=folder or report['candidate_digest']!=digest(batch)
            or report['review_digest']!=digest(review) or len(report['rows'])!=len(batch['rows'])):
        raise ValueError('Receipts do not belong to this destination and reviewed snapshot')
    accepted={}
    for row,source in zip(report['rows'],batch['rows']):
        if row['code']!=source['code']:
            raise ValueError('Receipt code order changed')
        accepted[row['code']]=sorted(
            [c for c in source['candidates'] if decisions[(row['code'],c['candidate_id'])]['decision']=='accept'],
            key=lambda c:decisions[(row['code'],c['candidate_id'])]['rank'])
    blocked=set()
    skip={'failed','duplicate_hash','size_rejected','title_changed'}

    def checkpoint():save_json(path,report)
    def mark_failed(row,a,reason):
        a['outcome']=reason
        row['candidate_outcomes'][a['candidate_id']]='failed'
        row['status']='failed'
    def mark_completed(row,tasks):
        a=completed_attempt(row,tasks,folder)
        if a:
            row.update(status='completed',selected=a,task=tasks[a['hash']])
            return True
        return False

    while True:
        # Resolve one best remaining approved page for EVERY eligible code before
        # the round's single 115 write. No uploads occur inside this selection loop.
        plans=[]
        for row in report['rows']:
            code=row['code']
            if row['status']=='completed' or code in blocked:
                continue
            choices=accepted[code]
            row['status']='ignored' if not choices else 'failed'
            for c in choices:
                cid=c['candidate_id']
                if row['candidate_outcomes'].get(cid) in skip:
                    continue
                a=next((a for a in row['attempts'] if a['candidate_id']==cid),None)
                if a is None:
                    resolved=backend.call('bt4g','bt4g_resolve_candidate',{'page_url':c['page_url']})
                    if resolved.get('status')=='unavailable':
                        row['candidate_outcomes'][cid]='failed';continue
                    if resolved.get('status')!='resolved':
                        row.update(status='blocked',error=resolved.get('error',resolved.get('status')))
                        blocked.add(code);break
                    if resolved.get('title')!=c['title']:
                        row['candidate_outcomes'][cid]='title_changed';continue
                    actual=video_bytes(resolved.get('files',[]))
                    if actual<=MIN_VIDEO_BYTES:
                        row['candidate_outcomes'][cid]='size_rejected';continue
                    h=resolved['hash'].lower()
                    if len(h)!=40 or any(ch not in '0123456789abcdef' for ch in h):
                        raise ValueError('Malformed info hash')
                    if any(old['hash']==h for old in row['attempts']):
                        row['candidate_outcomes'][cid]='duplicate_hash';continue
                    a={**c,'hash':h,'magnet':'magnet:?xt=urn:btih:'+h,
                       'video_bytes':actual,'files':resolved['files'],'submission':'not_submitted'}
                    row['attempts'].append(a)
                row['status']='pending'
                plans.append((row,a))
                break
            checkpoint()

        wanted={a['hash'] for row in report['rows'] for a in row['attempts']}
        tasks=task_snapshot(backend,wanted)
        for row in report['rows']:
            mark_completed(row,tasks)  # Includes earlier timed-out candidates.
        active=[];writes=[]
        for row,a in plans:
            if row['status']=='completed':continue
            task=tasks.get(a['hash'])
            if task and (task.get('status')==-1 or str(task.get('folder_id'))!=folder):
                mark_failed(row,a,'115_failed_or_destination_conflict');continue
            if not task:
                if a['submission']!='not_submitted':
                    row.update(status='blocked',error='Previous submission is ambiguous; do not resubmit')
                    blocked.add(row['code']);continue
                writes.append((row,a))
            active.append((row,a))

        if writes:
            started=clock()
            unique={a['hash']:a['magnet'] for _,a in writes}
            receipt={'batch_id':len(report.setdefault('upload_batches',[]))+1,
                     'codes':[row['code'] for row,_ in writes],
                     'magnets':list(unique.values()),'started_at':started,'status':'attempting'}
            report['upload_batches'].append(receipt)
            for _,a in writes:
                a.update(submission='attempting',started_at=started,batch_id=receipt['batch_id'])
            checkpoint()  # Every per-code receipt is durable before the single batch write.
            print('Submitting batch '+str(receipt['batch_id'])+': '+str(len(unique))+' magnets',flush=True)
            try:
                receipt['response']=backend.call('netdisk','offline_download',{
                    'magnet_links':list(unique.values()),'target_path':folder})
                receipt['status']='accepted'
                for _,a in writes:a['submission']='accepted'
            except Exception as exc:
                receipt.update(status='unconfirmed',error=str(exc))
                for _,a in writes:a.update(submission='unconfirmed',submission_error=str(exc))
                checkpoint()
                if any(s in str(exc).casefold() for s in ('quota','cookie','login expired','登录','配额','限额')):
                    raise RuntimeError('115 account/service blocker: '+str(exc)) from exc
            checkpoint()

        for _,a in active:a.setdefault('started_at',clock())
        while active:
            tasks=task_snapshot(backend,wanted)
            waiting=[]
            for row,a in active:
                if mark_completed(row,tasks):continue
                task=tasks.get(a['hash'])
                if task and (task.get('status')==-1 or str(task.get('folder_id'))!=folder):
                    mark_failed(row,a,'115_failed_or_destination_conflict')
                elif clock()>=a['started_at']+10:
                    if task is None:
                        row.update(status='blocked',error='Task not observable after batch submission; outcome ambiguous')
                        blocked.add(row['code'])
                    else:
                        a['last_task']=task
                        mark_failed(row,a,'not_completed_within_10_seconds')
                else:waiting.append((row,a))
            active=waiting
            checkpoint()
            if active:
                sleep(min(2,max(0,min(a['started_at']+10-clock() for _,a in active))))

        remaining=any(row['status']!='completed' and row['code'] not in blocked
                      and any(row['candidate_outcomes'].get(c['candidate_id']) not in skip
                              for c in accepted[row['code']]) for row in report['rows'])
        if not remaining:break
    checkpoint()
    write_reports(output,report)
    return report
