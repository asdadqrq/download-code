import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fetch_ltx_torrents import CsvStore, DateWindow, MIN_BYTES, collect_bt4g, collect_sukebei, info_hash, in_range, parse_date_range, parse_size, parse_sukebei, read_bt4g_magnet, sorted_search_url


def item(digest='a'*40, **changes):
    return {'title':'LTX example', 'magnet':'magnet:?xt=urn:btih:'+digest,
            'create_time':'2021-11-11', 'file_size':'1 GiB', 'file_size_bytes':MIN_BYTES,
            'source':'sukebei','source_url':'https://sukebei.nyaa.si/view/1', **changes}


class FetchTests(unittest.TestCase):
    def test_new_item_is_immediately_first_and_code_is_blank(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'output.csv'
            store=CsvStore(path)
            store.add(item('a'*40))
            store.add(item('b'*40,code='must remain empty'))
            with path.open(encoding='utf-8-sig',newline='') as handle:
                reader=csv.DictReader(handle)
                self.assertEqual(reader.fieldnames[0],'code')
                rows=list(reader)
            self.assertEqual([r['info_hash'] for r in rows],['b'*40,'a'*40])
            self.assertTrue(all(r['code']=='' for r in rows))

    def test_existing_user_filled_codes_are_preserved(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            with path.open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['title','code','magnet'])
                writer.writeheader()
                writer.writerow({'title':'Old LTX title','code':'MANUAL-001','magnet':'magnet:?xt=urn:btih:'+'a'*40})
            store=CsvStore(path)
            store.add(item('b'*40))
            with path.open(encoding='utf-8-sig',newline='') as handle:rows=list(csv.DictReader(handle))
            self.assertEqual(rows[0]['code'],'')
            self.assertEqual(rows[1]['code'],'MANUAL-001')

    def test_bt4g_incremental_does_not_resolve_older_details(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            first=item('a'*40,source='bt4g',source_url='https://bt4gprx.com/magnet/a')
            CsvStore(path).add(first)
            store=CsvStore(path)
            second=item('b'*40,source='bt4g',source_url='https://bt4gprx.com/magnet/b')
            older=item('c'*40,source='bt4g',source_url='https://bt4gprx.com/magnet/c',create_time='2021-11-10')
            client=Mock()
            client.evaluate.side_effect=[
                sorted_search_url('bt4g'),{'items':[first],'links':[sorted_search_url('bt4g',2)]},
                sorted_search_url('bt4g',2),{'items':[second,older],'links':[sorted_search_url('bt4g',3)]},
            ]
            args=SimpleNamespace(timeout=10,delay=0,max_pages=0)
            with patch('fetch_ltx_torrents.PinnedBT4GClient',return_value=client), \
                    patch('fetch_ltx_torrents.fetch',return_value='<rss><channel/></rss>'), \
                    patch('check_bt4g_exists.wait_for_result_cdp',return_value={'count':3}), \
                    patch('fetch_ltx_torrents.read_bt4g_magnet',return_value=second['magnet']) as resolve:
                self.assertEqual(collect_bt4g(store,args),2)
                resolve.assert_called_once_with(client,second['source_url'],10)
            self.assertIn('b'*40,store.seen)
            self.assertNotIn('c'*40,store.seen)

    def test_watermarks_are_per_source_and_fixed_for_the_run(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            original=CsvStore(path)
            original.add(item('a'*40,source='bt4g',create_time='2025-01-01'))
            original.add(item('b'*40,source='sukebei',create_time='2021-11-11'))
            store=CsvStore(path)
            self.assertEqual(store.cutoff('bt4g'),date(2025,1,1))
            self.assertEqual(store.cutoff('sukebei'),date(2021,11,11))
            store.add(item('c'*40,create_time='2026-01-01'))
            self.assertEqual(store.cutoff('sukebei'),date(2021,11,11))
            self.assertIsNone(CsvStore(path,incremental=False).cutoff('sukebei'))
            historical=CsvStore(path,parse_date_range('(2020-01-01,2020-12-31)'))
            self.assertEqual(historical.cutoff('bt4g'),date(2020,1,1))

    def test_same_date_is_inclusive_and_only_older_dates_stop(self):
        window=DateWindow(date(2021,11,11))
        selected,stop=window.select([item(create_time='2021-11-12'),item(create_time='2021-11-11')])
        self.assertEqual(len(selected),2)
        self.assertFalse(stop)
        selected,stop=window.select([item(create_time='2021-11-11'),item(create_time='2021-11-10')])
        self.assertEqual(len(selected),1)
        self.assertTrue(stop)

    def test_date_order_violations_are_rejected(self):
        window=DateWindow()
        window.select([item(create_time='2021-11-11')])
        with self.assertRaisesRegex(RuntimeError,'not ordered'):
            window.select([item(create_time='2021-11-12')])

    def test_new_csv_has_no_incremental_cutoff_and_output_sorts_by_date(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'new.csv'
            store=CsvStore(path)
            self.assertTrue(path.exists())
            self.assertIsNone(store.cutoff('sukebei'))
            store.add(item('a'*40,create_time='2020-01-01'))
            store.add(item('b'*40,create_time='2025-01-01'))
            store.sort_newest_first()
            with path.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
            self.assertEqual([r['create_time'] for r in rows],['2025-01-01','2020-01-01'])

    def test_incremental_pagination_keeps_later_same_day_new_items(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            CsvStore(path).add(item())
            store=CsvStore(path)
            args=SimpleNamespace(timeout=10,delay=0,max_pages=0)
            pages=[([item()],sorted_search_url('sukebei',2)),
                   ([item('b'*40),item('c'*40,create_time='2021-11-10')],sorted_search_url('sukebei',3))]
            with patch('fetch_ltx_torrents.fetch',return_value='html') as fetch, \
                    patch('fetch_ltx_torrents.parse_sukebei',side_effect=pages):
                self.assertEqual(collect_sukebei(store,args),2)
            self.assertEqual(fetch.call_count,2)
            self.assertIn('b'*40,store.seen)
            self.assertNotIn('c'*40,store.seen)
            query=parse_qs(urlsplit(fetch.call_args_list[0].args[1]).query)
            self.assertEqual((query['s'],query['o']),(['id'],['desc']))

    def test_bt4g_direct_magnet_and_download_button(self):
        url='https://bt4gprx.com/magnet/example'
        magnet='magnet:?xt=urn:btih:'+'a'*40
        for state in [
            {'url':url,'magnet':magnet,'download':'','blocked':False},
            {'url':url,'magnet':'','download':'https://downloadtorrentfile.com/hash/'+'a'*40,'blocked':False},
        ]:
            with self.subTest(state=state):
                client=Mock()
                client.evaluate.side_effect=[url,state]
                self.assertEqual(read_bt4g_magnet(client,url,10),magnet)

    def test_existing_row_without_hash_does_not_skip_its_detail_page(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            with path.open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['source_url','magnet'])
                writer.writeheader()
                writer.writerow({'source_url':'https://bt4gprx.com/magnet/example','magnet':''})
            store=CsvStore(path)
            self.assertFalse(store.known_url('https://bt4gprx.com/magnet/example'))

    def test_date_range_and_size_boundaries(self):
        bounds=parse_date_range('(2020-10-11,2021-11-11)')
        self.assertTrue(in_range('2020-10-11',bounds))
        self.assertTrue(in_range('2021-11-11',bounds))
        self.assertFalse(in_range('2021-11-12',bounds))
        self.assertIsNone(parse_date_range('null'))
        self.assertEqual(parse_size('1 GB'),MIN_BYTES)
        self.assertLess(parse_size('1023 MiB'),MIN_BYTES)
        with self.assertRaises(ValueError):parse_date_range('(2021-11-11,2020-10-11)')

    def test_concurrent_cross_site_hash_deduplication(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'output.csv'
            store=CsvStore(path)
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(store.add,[item(),item('A'*40,source='bt4g')]))
            self.assertEqual(sum(result),1)
            with path.open(encoding='utf-8-sig',newline='') as f:self.assertEqual(len(list(csv.DictReader(f))),1)
            self.assertFalse(CsvStore(path).add(item()))

    def test_append_existing_schema_and_filter(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'existing.csv'
            with path.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['note','torrent_hash'])
                writer.writeheader();writer.writerow({'note':'keep, 中文','torrent_hash':'a'*40})
            store=CsvStore(path,parse_date_range('(2020-10-11,2021-11-11)'))
            self.assertFalse(store.add(item()))
            self.assertFalse(store.add(item('b'*40,file_size_bytes=MIN_BYTES-1)))
            self.assertFalse(store.add(item('c'*40,create_time='2022-01-01')))
            self.assertTrue(store.add(item('d'*40)))
            with path.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
            self.assertEqual(rows[1]['note'],'keep, 中文')
            self.assertEqual(len(rows),2)

    def test_sukebei_rows_and_next_page(self):
        page='''<table class="torrent-list"><tbody><tr><td></td>
        <td><a href="/view/123" title="LTX example">LTX example</a></td>
        <td><a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">magnet</a></td>
        <td>1.5 GiB</td><td data-timestamp="0">date</td></tr></tbody></table>
        <ul class="pagination"><a href="/?q=LTX&amp;p=2">2</a></ul>'''
        rows,next_url=parse_sukebei(page,'https://sukebei.nyaa.si/?q=LTX&p=1')
        self.assertEqual(rows[0]['create_time'],'1970-01-01')
        self.assertEqual(info_hash(rows[0]['magnet']),'a'*40)
        self.assertIn('p=2',next_url)
        query=parse_qs(urlsplit(next_url).query)
        self.assertEqual((query['s'],query['o']),(['id'],['desc']))


if __name__=='__main__':unittest.main()
