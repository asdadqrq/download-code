import unittest
import csv
from unittest.mock import Mock, patch
from threading import Barrier
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import check_google_uncensored as checker


class RetryCsvTests(unittest.TestCase):
    fields = ['code', 'matched', 'error', 'note']

    def write_fixture(self, path):
        rows = [
            dict(zip(self.fields, ['AAA-001', 'yes', '', 'keep success'])),
            dict(zip(self.fields, ['BBB-002', 'error', 'timeout', 'retry this'])),
            dict(zip(self.fields, ['CCC-003', 'unknown', 'no relevant results', 'keep unknown'])),
            dict(zip(self.fields, ['DDD-004', 'no', '', 'keep negative'])),
            dict(zip(self.fields, ['AAA-001', 'error', 'timeout', 'same code, failed row'])),
        ]
        checker.write_results_csv(path, rows, self.fields)
        return rows

    def read_rows(self, path):
        with path.open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def test_retry_errors_merges_only_failed_rows_and_backs_up_original(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'results.csv'
            original = self.write_fixture(path)
            original_bytes = path.read_bytes()
            replacements = [{'code':code, 'matched':'no', 'error':''}
                            for code in ['BBB-002', 'AAA-001']]
            with patch.object(checker.sys, 'argv', [
                'checker', '--retry-errors', '--output', str(path), '--browser', 'none',
            ]), patch.object(checker, 'check_codes', return_value=replacements) as check:
                checker.main()
            check.assert_called_once_with(['BBB-002', 'AAA-001'], limit=4, delay=4.0)
            actual = self.read_rows(path)
            for i in (0, 2, 3):
                self.assertEqual(actual[i], original[i])
            for i in (1, 4):
                self.assertEqual(actual[i]['matched'], 'no')
                self.assertEqual(actual[i]['error'], '')
                self.assertEqual(actual[i]['note'], original[i]['note'])
            self.assertEqual([r['code'] for r in actual], [r['code'] for r in original])
            backups = list(Path(folder).glob('results.csv.*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original_bytes)

    def test_retry_unknown_can_write_separate_output_without_changing_source(self):
        with TemporaryDirectory() as folder:
            source, output = Path(folder)/'source.csv', Path(folder)/'updated.csv'
            self.write_fixture(source)
            original_bytes = source.read_bytes()
            replacements = [{'code':code, 'matched':'yes', 'error':''}
                            for code in ['BBB-002', 'CCC-003', 'AAA-001']]
            with patch.object(checker.sys, 'argv', [
                'checker', '--retry-errors', str(source), '--retry-unknown',
                '--output', str(output), '--browser', 'none',
            ]), patch.object(checker, 'check_codes', return_value=replacements) as check:
                checker.main()
            self.assertEqual(check.call_args.args[0], ['BBB-002', 'CCC-003', 'AAA-001'])
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(self.read_rows(output)[2]['matched'], 'yes')
            self.assertEqual(list(Path(folder).glob('*.bak')), [])

    def test_no_errors_does_not_start_browser_or_rewrite_csv(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'source.csv'
            checker.write_results_csv(path, [{'code':'AAA-001', 'matched':'yes'}], ['code','matched'])
            original_bytes = path.read_bytes()
            with patch.object(checker.sys, 'argv', ['checker', '--retry-errors', str(path)]), \
                    patch.object(checker.ExistingChromeGoogleSearcher, 'select_tabs') as browser:
                checker.main()
            browser.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(folder).glob('*.bak')), [])

    def test_retry_requires_valid_csv_columns_before_search(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'invalid.csv'
            path.write_text('wrong,column\n1,2\n')
            with patch.object(checker.sys, 'argv', ['checker', '--retry-errors', str(path)]), \
                    patch.object(checker, 'check_codes') as check:
                with self.assertRaises(SystemExit) as exc:
                    checker.main()
            self.assertEqual(exc.exception.code, 2)
            check.assert_not_called()

    def test_failed_csv_write_preserves_previous_output(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'source.csv'
            path.write_text('original data')
            with self.assertRaises(ValueError):
                checker.write_results_csv(path, [{'unexpected':'value'}], ['code'])
            self.assertEqual(path.read_text(), 'original data')
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])


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

    def test_chrome_search_uses_noninteractive_events_before_tab_access(self):
        app = Mock()
        app.launchFlags.return_value = 0x100000
        tab = Mock()

        def read_url():
            app.setSendMode_.assert_called_with(0x13)
            app.setLaunchFlags_.assert_called_with(0x100200)
            return 'https://www.google.com/'

        tab.URL.side_effect = read_url
        tab.setURL_.side_effect = AssertionError('URL setter can bring Chrome forward')
        tab.executeJavascript_.side_effect = [
            'navigation_scheduled', '{"ready":true,"results":[]}',
            'navigation_scheduled', '{"ready":true,"results":[]}',
        ]
        browser = checker.ExistingChromeGoogleSearcher(app=app, tab=tab)
        self.assertEqual(browser.search('AAA-001 uncensored'), [])
        self.assertEqual(browser.search('BBB-002 uncensored'), [])
        tab.setURL_.assert_not_called()
        self.assertEqual(tab.executeJavascript_.call_count, 4)
        navigation = tab.executeJavascript_.call_args_list[0].args[0]
        self.assertIn('window.location.assign(', navigation)
        self.assertIn('window.setTimeout(', navigation)
        self.assertIn('AAA-001+uncensored', navigation)
        app.activate.assert_not_called()

    def test_tab_discovery_configures_background_mode_before_browser_events(self):
        app = Mock()
        app.launchFlags.return_value = 0
        window, tab, process = Mock(), Mock(), Mock()
        tab.URL.return_value = 'https://www.google.com/'
        window.tabs.return_value = [tab]
        process.bundleIdentifier.return_value = 'com.google.Chrome'
        process.processIdentifier.return_value = 123
        workspace = Mock()
        workspace.sharedWorkspace.return_value.runningApplications.return_value = [process]
        script_app = Mock()
        script_app.applicationWithProcessIdentifier_.return_value = app

        def read_windows():
            app.setSendMode_.assert_called_with(0x13)
            app.setLaunchFlags_.assert_called_with(0x200)
            return [window]

        app.windows.side_effect = read_windows
        objc = Mock()
        objc.loadBundle.side_effect = lambda name, bundle, **kwargs: bundle.update(
            SBApplication=script_app,
        )
        with patch.dict(checker.sys.modules, {
            'objc': objc, 'AppKit': Mock(NSWorkspace=workspace),
        }):
            self.assertEqual(
                checker.ExistingChromeGoogleSearcher.select_tabs(['AAA-001 uncensored'], 1),
                [(app, tab)],
            )
        app.activate.assert_not_called()
        tab.URL.return_value = 'https://example.com/'
        with patch.dict(checker.sys.modules, {
            'objc': objc, 'AppKit': Mock(NSWorkspace=workspace),
        }):
            with self.assertRaisesRegex(checker.GoogleTabRequiredError, 'Open a Google'):
                checker.ExistingChromeGoogleSearcher.select_tabs(['AAA-001 uncensored'], 1)
        app.classForScriptingClass_.assert_not_called()

    def test_navigation_fails_if_chrome_does_not_acknowledge_scheduling(self):
        tab = Mock()
        tab.executeJavascript_.return_value = None
        browser = checker.ExistingChromeGoogleSearcher(tab=tab)
        with self.assertRaisesRegex(RuntimeError, 'schedule background navigation'):
            browser.navigate('https://www.google.com/')
        tab.setURL_.assert_not_called()

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
        tab.executeJavascript_.return_value = 'navigation_scheduled'
        tab.setURL_.side_effect = AssertionError('Retry must not bring Chrome forward')
        result = {'title':'MDBK-380 uncensored', 'link':'https://example.com/mdbk-380'}
        with patch.object(browser, 'evaluate', side_effect=[
            {'ready':False}, {'ready':True, 'results':[result]},
        ]), patch.object(checker.time, 'monotonic', side_effect=[0, 0, 2, 3, 3]), \
                patch.object(checker.time, 'sleep'):
            self.assertEqual(browser.search('MDBK-380 uncensored'), [result])
        tab.executeJavascript_.assert_called_once()
        tab.setURL_.assert_not_called()

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
