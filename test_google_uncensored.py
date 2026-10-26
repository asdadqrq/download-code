import unittest
from unittest.mock import Mock, patch
from threading import Barrier
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import check_google_uncensored as checker


class ProviderPriorityTests(unittest.TestCase):
    def test_main_reuses_one_tab_for_sequential_searches(self):
        browser = Mock(method='google_chrome')
        browser.search.side_effect = lambda query, limit: [{
            'title': query, 'snippet': '',
            'link': 'https://example.com/' + query.split()[0],
        }]
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'results.csv'
            with patch.object(checker.sys, 'argv', [
                'check_google_uncensored.py', 'AAA-001,BBB-002',
                '--delay', '0', '-o', str(output),
            ]), patch.object(checker.sys, 'platform', 'darwin'), \
                    patch.object(checker, 'ExistingChromeGoogleSearcher') as searcher_class, \
                    patch.object(checker, 'check_codes_parallel') as parallel:
                app, tab = Mock(), Mock()
                searcher_class.select_tabs.return_value = [(app, tab)]
                searcher_class.return_value = browser
                checker.main()
                searcher_class.select_tabs.assert_called_once_with(
                    ['AAA-001 uncensored', 'BBB-002 uncensored'], 1,
                )
                searcher_class.assert_called_once_with(
                    wait_seconds=4.0, app=app, tab=tab, timeout=30.0, retries=1,
                )
                self.assertEqual(
                    [call.args[0] for call in browser.search.call_args_list],
                    ['AAA-001 uncensored', 'BBB-002 uncensored'],
                )
                parallel.assert_not_called()
                self.assertTrue(output.exists())

    def test_parallel_search_preserves_order_and_limit(self):
        barrier = Barrier(2)
        seen_limits = []

        class Browser:
            method = 'google_chrome'

            def search(self, query, limit=10):
                seen_limits.append(limit)
                barrier.wait(timeout=2)
                code = query.split()[0]
                return [{'title': f'{code} [Uncensored Leaked]', 'snippet': '',
                         'link': f'https://example.com/{code}-decensored'}]

        rows = checker.check_codes_parallel(
            ['AAA-001', 'BBB-002'], [Browser(), Browser()], limit=4, delay=0
        )
        self.assertEqual([row['code'] for row in rows], ['AAA-001', 'BBB-002'])
        self.assertEqual([row['matched'] for row in rows], ['yes', 'yes'])
        self.assertEqual(seen_limits, [4, 4])

    def test_parallel_search_staggers_second_tab(self):
        started = {}

        class Browser:
            method = 'google_chrome'

            def search(self, query, limit=10):
                code = query.split()[0]
                started[code] = time.monotonic()
                return [{'title': f'{code} [Uncensored Leaked]', 'snippet': '',
                         'link': f'https://example.com/{code}-decensored'}]

        rows = checker.check_codes_parallel(
            ['AAA-001', 'BBB-002'], [Browser(), Browser()],
            delay=0, stagger=0.05,
        )
        self.assertEqual([row['matched'] for row in rows], ['yes', 'yes'])
        self.assertGreaterEqual(started['BBB-002'] - started['AAA-001'], 0.04)

    def test_parallel_challenge_stops_later_batches(self):
        class Blocked:
            method = 'google_chrome'

            def search(self, query, limit=4):
                raise checker.GoogleChallengeError('Google unusual-traffic challenge')

        rows = checker.check_codes_parallel(
            ['AAA-001', 'BBB-002', 'CCC-003'], [Blocked(), Blocked()], delay=0
        )
        self.assertEqual([row['matched'] for row in rows], ['unknown'] * 3)
        self.assertIn('Not searched', rows[2]['error'])

    def test_no_google_tab_aborts_before_csv_rows(self):
        class MissingTabBrowser:
            def search(self, query, limit=10):
                raise checker.GoogleTabRequiredError('Open the first code search in Chrome')

        with self.assertRaises(checker.GoogleTabRequiredError):
            checker.check_codes(['MDBK-380'], delay=0, browser_searcher=MissingTabBrowser())

    def test_challenge_stops_remaining_queries(self):
        class BlockedBrowser:
            method = 'google_chrome'
            calls = 0

            def search(self, query, limit=10):
                self.calls += 1
                raise checker.GoogleChallengeError('Google showed unusual traffic')

        browser = BlockedBrowser()
        rows = checker.check_codes(['MDBK-380', 'COSX-063'], delay=0, browser_searcher=browser)
        self.assertEqual(browser.calls, 1)
        self.assertEqual([row['matched'] for row in rows], ['unknown', 'unknown'])
        self.assertIn('unusual traffic', rows[0]['error'])
        self.assertIn('Not searched', rows[1]['error'])

    def test_interactive_challenge_handler_retries_same_code(self):
        class BlockedOnceBrowser:
            method = 'google_chrome'

            def __init__(self):
                self.calls = 0

            def search(self, query, limit=10):
                self.calls += 1
                if self.calls == 1:
                    raise checker.GoogleChallengeError('Google unusual-traffic challenge')
                code = query.split()[0]
                return [{
                    'title': f'{code} [Uncensored Leaked]',
                    'snippet': '',
                    'link': f'https://example.com/{code}-decensored',
                }]

        browser = BlockedOnceBrowser()
        challenges = []
        rows = checker.check_codes(
            ['MDBK-380'], delay=0, browser_searcher=browser,
            challenge_handler=challenges.append,
        )
        self.assertEqual(browser.calls, 2)
        self.assertEqual(len(challenges), 1)
        self.assertEqual(rows[0]['matched'], 'yes')

    def test_parallel_challenge_pauses_once_and_retries_batch(self):
        class BlockedOnceBrowser:
            method = 'google_chrome'

            def __init__(self):
                self.calls = 0

            def search(self, query, limit=10):
                self.calls += 1
                if self.calls == 1:
                    raise checker.GoogleChallengeError('Google unusual-traffic challenge')
                code = query.split()[0]
                return [{
                    'title': f'{code} [Uncensored Leaked]',
                    'snippet': '',
                    'link': f'https://example.com/{code}-decensored',
                }]

        browsers = [BlockedOnceBrowser(), BlockedOnceBrowser()]
        challenges = []
        rows = checker.check_codes_parallel(
            ['AAA-001', 'BBB-002'], browsers, delay=0,
            challenge_handler=challenges.append,
        )
        self.assertEqual([browser.calls for browser in browsers], [2, 2])
        self.assertEqual(len(challenges), 1)
        self.assertEqual([row['matched'] for row in rows], ['yes', 'yes'])

    def test_existing_query_tab_is_read_without_reloading(self):
        browser = checker.ExistingChromeGoogleSearcher()
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored&hl=en'
        with patch.object(browser, 'select_tab', return_value=(Mock(), tab)), \
                patch.object(browser, 'evaluate', return_value={'ready': True, 'results': [], 'blocked': False}):
            self.assertEqual(browser.search('MDBK-380 uncensored'), [])
        tab.setURL_.assert_not_called()

    def test_loading_timeout_retries_in_the_same_tab(self):
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored&hl=en'
        browser = checker.ExistingChromeGoogleSearcher(tab=tab, timeout=1, retries=1)
        result = {'title':'MDBK-380 uncensored', 'link':'https://example.com/mdbk-380'}
        with patch.object(browser, 'evaluate', side_effect=[
            {'ready':False}, {'ready':True, 'results':[result]},
        ]), patch.object(checker.time, 'monotonic', side_effect=[0, 0, 2, 3, 3]), \
                patch.object(checker.time, 'sleep'):
            self.assertEqual(browser.search('MDBK-380 uncensored'), [result])
        tab.setURL_.assert_called_once()

    def test_transient_document_read_failure_is_retried_without_navigation(self):
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored'
        browser = checker.ExistingChromeGoogleSearcher(tab=tab, timeout=1)
        with patch.object(browser, 'evaluate', side_effect=[
            ValueError('Document is navigating'), {'ready':True, 'results':[]},
        ]), patch.object(checker.time, 'monotonic', side_effect=[0, 0, 0.5]), \
                patch.object(checker.time, 'sleep'):
            self.assertEqual(browser.search('MDBK-380 uncensored'), [])
        tab.setURL_.assert_not_called()

    def test_persistent_timeout_includes_document_diagnostics(self):
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored'
        browser = checker.ExistingChromeGoogleSearcher(tab=tab, timeout=1, retries=0)
        state = {'ready':False, 'url':tab.URL(), 'title':'Loading...',
                 'documentState':'loading', 'resultRoot':False}
        with patch.object(browser, 'evaluate', return_value=state), \
                patch.object(checker.time, 'monotonic', side_effect=[0, 0, 2]), \
                patch.object(checker.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'title=Loading.*document=loading'):
                browser.search('MDBK-380 uncensored')

    def test_challenge_is_not_automatically_reloaded(self):
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored'
        browser = checker.ExistingChromeGoogleSearcher(tab=tab)
        for state in [{'blocked':True}, {'consent':True}]:
            with self.subTest(state=state), patch.object(browser, 'evaluate', return_value=state):
                with self.assertRaises(checker.GoogleChallengeError):
                    browser.search('MDBK-380 uncensored')
        tab.setURL_.assert_not_called()

    def test_cosx_release_metadata_confirms_explicit_label(self):
        result = {
            'title': 'COSX-063-RM [Reducing Mosaic] Example title',
            'snippet': 'Movies JAV COSX-063-RM release date Oct. 09, 2024, Uncensored Leaked version. Genres: Example.',
            'link': 'https://example.com/cosx-063-rm',
        }
        self.assertEqual(checker.find_match('COSX-063', [result])[0], 1)

    def test_reducing_mosaic_alone_does_not_claim_uncensored(self):
        result = {'title':'COSX-063-RM [Reducing Mosaic] Example title',
                  'snippet':'', 'link':'https://example.com/cosx-063-rm'}
        self.assertFalse(checker.find_match('COSX-063', [result])[1])

    def test_other_code_label_and_site_boilerplate_do_not_match(self):
        for snippet in [
            'NIMA-063-RM release date Oct. 09, 2024, Uncensored Leaked version.',
            'COSX-063 Example title. Browse all Uncensored Leaked videos.',
            'COSX-063-RM release date Oct. 09, 2024, unrelated description. Uncensored Leaked version.',
            'COSX-063-RM release date Oct. 09, 2024, Uncensoredly version.',
        ]:
            result = {'title':'COSX-063 Example title', 'snippet':snippet,
                      'link':'https://example.com/cosx-063'}
            with self.subTest(snippet=snippet):
                self.assertFalse(checker.find_match('COSX-063', [result])[1])

    def test_browser_match_is_not_overridden_by_bing(self):
        browser = type("Browser", (), {
            "search": lambda self, query, limit=10: [{
                "title": "719MAG-056 [Uncensored Leaked]",
                "snippet": "",
                "link": "https://example.com/video/719mag-056-decensored/",
            }]
        })()
        with patch.object(checker, "search_google", return_value=([{
            "title": "Unrelated result",
            "snippet": "",
            "link": "https://example.com/other",
        }], "bing")) as search:
            rows = checker.check_codes(["719MAG-056"], limit=10, delay=0, browser_searcher=browser)
        search.assert_not_called()
        self.assertEqual(rows[0]["matched"], "yes")
        self.assertEqual(rows[0]["search_method"], "google_chrome")

    def test_empty_browser_results_are_unknown_without_silent_fallback(self):
        browser = type("Browser", (), {"search": lambda self, query, limit=10: []})()
        with patch.object(checker, "search_google", return_value=([], "none")) as search:
            rows = checker.check_codes(["719MAG-056"], limit=10, delay=0, browser_searcher=browser)
        self.assertEqual(rows[0]["matched"], "unknown")
        search.assert_not_called()

    def test_unrelated_results_are_unknown(self):
        unrelated = [{'title':'Corporate chocolate gifts', 'snippet':'Gift boxes',
                      'link':'https://example.com/gifts'}]
        with patch.object(checker, 'search_google', return_value=(unrelated, 'bing')):
            row = checker.check_codes(['719MAG-056'], delay=0)[0]
        self.assertEqual(row['matched'], 'unknown')

    def test_relevant_code_without_label_is_no(self):
        result = [{'title':'719MAG-056 title', 'snippet':'',
                   'link':'https://example.com/video/719mag-056'}]
        with patch.object(checker, 'search_google', return_value=(result, 'google_html')):
            row = checker.check_codes(['719MAG-056'], delay=0)[0]
        self.assertEqual(row['matched'], 'no')

    @patch.object(checker, 'google_cse_search', return_value=None)
    @patch.object(checker, 'google_html_search', return_value=[])
    @patch.object(checker, 'bing_search', return_value=[{'title':'Chocolate gifts','snippet':'','link':'https://example.com/gifts'}])
    @patch.object(checker, 'duckduckgo_lite_search')
    def test_http_chain_skips_unrelated_provider(self, ddg, bing, google, cse):
        ddg.return_value = [{'title':'719MAG-056 [Uncensored Leaked]', 'snippet':'',
                             'link':'https://example.com/video/719mag-056-decensored/'}]
        results, method = checker.search_google('719MAG-056 uncensored')
        self.assertEqual(method, 'duckduckgo_lite')
        self.assertTrue(checker.find_match('719MAG-056', results)[1])

    def test_normal_browser_url_does_not_force_legacy_page(self):
        self.assertNotIn('gbv=', checker.GOOGLE_BROWSER_URL)

    def test_bing_redirect_decoding(self):
        import base64
        url = 'https://example.com/video/719mag-056-decensored/'
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
        self.assertEqual(checker.extract_redirect_url('https://www.bing.com/ck/a?u=a1'+encoded), url)


if __name__ == "__main__":
    unittest.main()
