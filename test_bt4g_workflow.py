import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import bt4g_workflow as w


def candidate(title='Release title',size=3_000_000_000,suffix='one'):
    return {'title':title,'size_bytes':size,'url':'https://bt4gprx.com/magnet/'+suffix}


class Clock:
    def __init__(self): self.now=100.0
    def time(self): return self.now
    def sleep(self,n): self.now+=n


class Backend:
    def __init__(self,complete_hash=None,files=None):
        self.complete_hash=complete_hash
        self.files=files or [{'name':'movie.mp4','size_bytes':3_000_000_000}]
        self.submitted=[];self.calls=[]
    def call(self,server,name,args):
        self.calls.append((name,args))
        if name=='bt4g_resolve_candidate':
            tail=args['page_url'].rsplit('/',1)[1]
            number={'one':1,'two':2,'three':3}[tail]
            h=str(number)*40
            return {'status':'resolved','title':self.titles[tail],'hash':h,'files':self.files}
        if name=='offline_tasks_115':
            return {'page_count':1,'tasks':[{'info_hash':h,'status':2 if h==self.complete_hash else 1,'folder_id':'123','file_id':'file'} for h in self.submitted]}
        if name=='offline_download':
            self.submitted.append(args['magnet_links'][0].split(':')[-1])
            return {'text':'accepted'}
        raise AssertionError(name)


class WorkflowTests(unittest.TestCase):
    def test_search_suffix_changes_query_not_identifier(self):
        self.assertEqual(w.search_query_for_code('IBW-495Z'),'IBW-495')
        self.assertEqual(w.search_query_for_code('AOZ-219Z'),'AOZ-219')
        self.assertEqual(w.search_query_for_code('IBW-00495ZX'),'IBW-00495')
        self.assertEqual(w.search_query_for_code('T28-430'),'T28-430')
        self.assertEqual(w.search_query_for_code('200GANA-1688'),'200GANA-1688')
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.out=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def prepared(self,items=None,accept=True):
        cs=w.rank_candidates(items or [candidate()])
        batch={'version':2,'codes':['LOVE-191'],'search_finished':True,
               'rows':[{'code':'LOVE-191','status':'complete','candidates':cs}]}
        w.save_json(self.out/'candidates.json',batch)
        review={'candidate_digest':w.digest(batch),'method':'language_model','reviewer':'test fixture, not a live review',
                'decisions':[{'code':'LOVE-191','candidate_id':c['candidate_id'],
                    'decision':'accept' if accept else 'reject','reason':'Explicit fixture decision',
                    'rank':i+1 if accept else None} for i,c in enumerate(cs)]}
        w.save_json(self.out/'review_decisions.json',review)
        w.apply_review(self.out,self.out/'review_decisions.json')
        return batch,review
    def test_rank_soft_preferences_hard_floor_and_top_three(self):
        items=[candidate('Thz longest',1_000_000_000,'one'),candidate('short',3_000_000_000,'two'),
               candidate('Thz short',3_000_000_000,'three'),candidate('under',900_000_000,'four'),
               candidate('only',901_000_000,'five')]
        self.assertEqual([c['title'] for c in w.rank_candidates(items)],['Thz short','Thz longest','short'])
        self.assertEqual(len(w.rank_candidates([items[-1]])),1)
    def test_collector_does_not_pretend_to_classify_titles(self):
        cs=w.rank_candidates([candidate('In The Mood For Love 191 Photos.zip')])
        self.assertEqual(len(cs),1)
        self.assertNotIn('magnet',cs[0]);self.assertNotIn('hash',cs[0])
    def test_no_review_no_network_calls(self):
        w.save_json(self.out/'candidates.json',{})
        backend=Backend()
        with self.assertRaises(FileNotFoundError):w.transfer(backend,self.out,'123')
        self.assertEqual(backend.calls,[])
    def test_stale_incomplete_and_missing_decisions_fail(self):
        batch,review=self.prepared()
        bad=copy.deepcopy(batch);bad['rows'][0]['candidates'][0]['title']='changed'
        with self.assertRaises(ValueError):w.validate_review(bad,review)
        bad=copy.deepcopy(batch);bad['search_finished']=False
        with self.assertRaises(ValueError):w.validate_review(bad,review)
        bad=copy.deepcopy(review);bad['decisions']=[]
        with self.assertRaises(ValueError):w.validate_review(batch,bad)
    def test_rejected_titles_are_ignored_without_magnet_extraction(self):
        self.prepared(accept=False)
        backend=Backend();clock=Clock()
        result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(result['rows'][0]['status'],'ignored');self.assertEqual(backend.calls,[])
    def test_retry_next_candidate_after_ten_seconds_and_stop_on_success(self):
        self.prepared([candidate('a',suffix='one'),candidate('bb',suffix='two'),candidate('ccc',suffix='three')])
        backend=Backend(complete_hash='2'*40);backend.titles={'one':'a','two':'bb','three':'ccc'}
        clock=Clock();result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(backend.submitted,['1'*40,'2'*40]);self.assertGreaterEqual(clock.now,110)
        self.assertEqual(result['rows'][0]['status'],'completed')
    def test_all_three_fail_no_fourth_and_resume_does_not_resubmit(self):
        self.prepared([candidate('a',suffix='one'),candidate('bb',suffix='two'),candidate('ccc',suffix='three')])
        backend=Backend();backend.titles={'one':'a','two':'bb','three':'ccc'};clock=Clock()
        result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(result['rows'][0]['status'],'failed');self.assertEqual(len(backend.submitted),3)
        w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(len(backend.submitted),3)
        self.assertEqual((self.out/'failed_codes.txt').read_text().strip(),'LOVE-191')
    def test_actual_video_floor_not_torrent_total(self):
        self.prepared();backend=Backend(files=[{'name':'pictures.zip','size_bytes':5_000_000_000},
                                               {'name':'preview.mp4','size_bytes':900_000_000}])
        backend.titles={'one':'Release title'};clock=Clock()
        result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(backend.submitted,[]);self.assertEqual(result['rows'][0]['status'],'failed')
    def test_changed_title_cannot_reuse_model_approval(self):
        self.prepared();backend=Backend();backend.titles={'one':'Different title'};clock=Clock()
        w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(backend.submitted,[])
    def test_parse_visible_video_sizes(self):
        files=w.parse_file_lines(['partA.mp4','950 MB','partB.mp4 1.2 GB','photo.zip 5GB'])
        self.assertEqual(w.video_bytes(files),2_150_000_000)
    def test_asf_is_a_video_format_not_an_unknown_attachment(self):
        files=w.parse_file_lines(['release.ASF','3.03GB'])
        self.assertEqual(w.video_bytes(files),3_030_000_000)
    def test_unavailable_page_advances_to_next_approved_page(self):
        self.prepared([candidate('a',suffix='one'),candidate('bb',suffix='two')])
        backend=Backend(complete_hash='2'*40);backend.titles={'two':'bb'}
        original=backend.call
        def call(server,name,args):
            if name=='bt4g_resolve_candidate' and args['page_url'].endswith('/one'):
                return {'status':'unavailable','error':'detail page missing'}
            return original(server,name,args)
        backend.call=call;clock=Clock()
        result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(backend.submitted,['2'*40]);self.assertEqual(result['rows'][0]['status'],'completed')
    def test_late_completion_recognized_on_resume_without_resubmission(self):
        self.prepared();backend=Backend();backend.titles={'one':'Release title'};clock=Clock()
        w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        backend.complete_hash='1'*40
        result=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(result['rows'][0]['status'],'completed');self.assertEqual(len(backend.submitted),1)
    def test_ambiguous_missing_submission_is_not_repeated(self):
        self.prepared();backend=Backend();backend.titles={'one':'Release title'}
        original=backend.call
        def call(server,name,args):
            if name=='offline_tasks_115':return {'page_count':1,'tasks':[]}
            return original(server,name,args)
        backend.call=call;clock=Clock()
        first=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(first['rows'][0]['status'],'blocked')
        w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual(len(backend.submitted),1)
    def test_100_codes_one_batch_then_only_failed_codes_in_retry_batch(self):
        codes=['TEST-'+str(i).zfill(3) for i in range(100)]
        rows=[]
        for i,code in enumerate(codes):
            cs=w.rank_candidates([
                candidate('approved video',suffix='one-'+str(i)),
                candidate('approved video',suffix='two-'+str(i))])
            rows.append({'code':code,'status':'complete','candidates':cs})
        batch={'version':2,'codes':codes,'search_finished':True,'rows':rows}
        review={'candidate_digest':w.digest(batch),'method':'language_model','reviewer':'batch protocol test fixture',
            'decisions':[{'code':r['code'],'candidate_id':c['candidate_id'],'decision':'accept',
                          'reason':'Fixture approval for batching test, not a live semantic judgment','rank':i+1}
                         for r in rows for i,c in enumerate(r['candidates'])]}
        w.save_json(self.out/'candidates.json',batch)
        w.save_json(self.out/'review_decisions.json',review)
        w.apply_review(self.out,self.out/'review_decisions.json')
        class BatchBackend:
            def __init__(self):self.batches=[];self.tasks={};self.events=[]
            def call(self,server,name,args):
                self.events.append(name)
                if name=='bt4g_resolve_candidate':
                    h=hashlib.sha1(args['page_url'].encode()).hexdigest()
                    return {'status':'resolved','title':'approved video','hash':h,
                            'files':[{'name':'movie.mp4','size_bytes':3_000_000_000}]}
                if name=='offline_tasks_115':return {'page_count':1,'tasks':list(self.tasks.values())}
                if name=='offline_download':
                    links=args['magnet_links'];self.batches.append(links)
                    for i,link in enumerate(links):
                        h=link.split(':')[-1]
                        self.tasks[h]={'info_hash':h,'status':1 if len(self.batches)==1 and i<2 else 2,
                                       'folder_id':'123','file_id':'video'}
                    return {'text':'accepted'}
                raise AssertionError(name)
        backend=BatchBackend();clock=Clock()
        report=w.transfer(backend,self.out,'123',clock=clock.time,sleep=clock.sleep)
        self.assertEqual([len(b) for b in backend.batches],[100,2])
        first_write=backend.events.index('offline_download')
        self.assertEqual(backend.events[:first_write].count('bt4g_resolve_candidate'),100)
        self.assertTrue(all(r['status']=='completed' for r in report['rows']))
        self.assertEqual((self.out/'failed_codes.txt').read_text().strip(),'')


if __name__=='__main__':unittest.main()
