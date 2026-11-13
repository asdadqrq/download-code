import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch

from fetch_sehuatang_ltx import ThreadCsv,tid_from_link,parse_date_range,next_page,collect,wait_page

BASE='https://sehuatang.net/search.php?mod=forum&searchid=test&kw=LTX&page='


def item(tid,section='AI专区',date='2026-11-12',**extra):
    return {'title':'LTX example '+str(tid),'link':'https://sehuatang.net/forum.php?mod=viewthread&tid='+str(tid)+'&highlight=LTX',
            'date':date,'section':section,**extra}


class ForumTests(unittest.TestCase):
    def test_non_first_start_page_is_rejected(self):
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv')
            with self.assertRaisesRegex(ValueError,'page 1'):
                collect(Mock(),store,initial_url=BASE+'1',delay=0,start_page=194)

    def test_normal_run_starts_fresh_search_instead_of_reusing_old_cache(self):
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv')
            state={'url':BASE+'1','items':[],'links':[],'total':0}
            with patch('fetch_sehuatang_ltx.fresh_search',return_value=state) as fresh:
                self.assertEqual(collect(Mock(),store,delay=0),1)
                fresh.assert_called_once()

    def test_search_url_on_later_page_is_reset_to_page_one(self):
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv')
            client=Mock()
            state={'url':BASE+'1','items':[item(2)],'links':[],'total':1}
            with patch('fetch_sehuatang_ltx.wait_page',return_value=state):
                self.assertEqual(collect(client,store,initial_url=BASE+'194',delay=0),1)
            self.assertIn('page=1',client.call.call_args_list[0].args[1]['url'])

    def test_rate_limit_waits_then_retries_the_same_page(self):
        client=Mock()
        client.evaluate.side_effect=[
            {'url':BASE+'2','rateSeconds':2},
            {'url':BASE+'2','ready':True,'keyword':'LTX'},
        ]
        with patch('fetch_sehuatang_ltx.time.sleep') as sleep:
            result=wait_page(client,expected=BASE+'2')
        self.assertTrue(result['ready'])
        self.assertEqual(sleep.call_args_list[0].args,(3,))
        client.call.assert_called_once_with('Page.navigate',{'url':BASE+'2'})

    def test_tid_ignores_highlights_pages_and_seo_format(self):
        self.assertEqual(tid_from_link(item(123)['link']),'123')
        self.assertEqual(tid_from_link('https://sehuatang.net/thread-123-2-1.html'),'123')
        self.assertEqual(tid_from_link('https://sehuatang.net/forum.php?mod=viewthread&tid=123&page=5'),'123')
        self.assertEqual(tid_from_link('https://evil.example/forum.php?tid=123'),'')

    def test_excluded_sections_and_empty_spare_column(self):
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv')
            self.assertFalse(store.offer(item(1,'求片问答悬赏区')))
            self.assertFalse(store.offer(item(2,'综合讨论区')))
            self.assertFalse(store.offer(item(4,'投诉建议区')))
            self.assertFalse(store.offer(item(5,'个人导航')))
            self.assertTrue(store.offer(item(3,code='must stay blank')))
            store.flush()
            with store.path.open(encoding='utf-8-sig',newline='') as handle:
                reader=csv.DictReader(handle);rows=list(reader)
            self.assertEqual(reader.fieldnames,['title','link','date','code'])
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['code'],'')

    def test_existing_tid_skipped_new_rows_prepended_and_manual_codes_preserved(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'results.csv'
            with path.open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['title','link','date','code'])
                writer.writeheader();writer.writerow({'title':'old','link':'https://sehuatang.net/thread-123-1-1.html','date':'2020-01-01','code':'MANUAL'})
            store=ThreadCsv(path)
            self.assertFalse(store.offer(item(123)))
            self.assertTrue(store.offer(item(124)))
            self.assertFalse(store.offer(item(124)))
            store.flush()
            with path.open(encoding='utf-8-sig',newline='') as handle:rows=list(csv.DictReader(handle))
            self.assertEqual([tid_from_link(r['link']) for r in rows],['124','123'])
            self.assertEqual(rows[1]['code'],'MANUAL')

    def test_date_range_is_inclusive_and_null_means_unrestricted(self):
        self.assertIsNone(parse_date_range('null'))
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv',parse_date_range('(2026-11-01,2026-11-12)'))
            self.assertTrue(store.offer(item(1,date='2026-11-01 09:30')))
            self.assertTrue(store.offer(item(2,date='2026-11-12 19:15')))
            self.assertFalse(store.offer(item(3,date='2026-10-31')))
            self.assertFalse(store.offer(item(4,date='2026-11-13')))

    def test_first_existing_tid_stops_without_reading_later_items_or_pages(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'results.csv'
            original=ThreadCsv(path)
            original.offer(item(1));original.flush()
            store=ThreadCsv(path)
            client=Mock()
            state={'url':BASE+'1','items':[item(1),item(2)],'links':[BASE+'2'],'total':100}
            with patch('fetch_sehuatang_ltx.wait_page',return_value=state) as read, \
                    patch('fetch_sehuatang_ltx.next_page') as following:
                self.assertEqual(collect(client,store,initial_url=BASE+'1',delay=0),1)
            read.assert_called_once()
            following.assert_not_called()
            self.assertEqual(client.call.call_count,1)
            self.assertNotIn('2',store.seen)

    def test_new_items_before_existing_tid_are_saved_at_top(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'results.csv'
            original=ThreadCsv(path)
            original.offer(item(1));original.flush()
            store=ThreadCsv(path)
            state={'url':BASE+'1','items':[item(3),item(1),item(2)],'links':[BASE+'2'],'total':100}
            with patch('fetch_sehuatang_ltx.wait_page',return_value=state):
                collect(Mock(),store,initial_url=BASE+'1',delay=0)
            with path.open(encoding='utf-8-sig',newline='') as handle:rows=list(csv.DictReader(handle))
            self.assertEqual([tid_from_link(r['link']) for r in rows],['3','1'])

    def test_new_csv_still_reads_every_page(self):
        with TemporaryDirectory() as directory:
            store=ThreadCsv(Path(directory)/'results.csv')
            states=[{'url':BASE+'1','items':[item(1)],'links':[BASE+'2'],'total':2},
                    {'url':BASE+'2','items':[item(2,date='2019-01-01')],'links':[],'total':2}]
            with patch('fetch_sehuatang_ltx.wait_page',side_effect=states):
                self.assertEqual(collect(Mock(),store,initial_url=BASE+'1',delay=0),2)
            self.assertEqual(store.seen,{'1','2'})

    def test_missing_next_page_is_reported(self):
        with self.assertRaisesRegex(RuntimeError,'gap'):
            next_page({'url':BASE+'1','links':[BASE+'3']})


if __name__=='__main__':unittest.main()
