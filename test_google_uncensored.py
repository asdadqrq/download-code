import unittest
from unittest.mock import Mock, patch
from threading import Barrier

import check_google_uncensored as checker


class ProviderPriorityTests(unittest.TestCase):
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

    def test_existing_query_tab_is_read_without_reloading(self):
        browser = checker.ExistingChromeGoogleSearcher()
        tab = Mock()
        tab.URL.return_value = 'https://www.google.com/search?q=MDBK-380+uncensored&hl=en'
        with patch.object(browser, 'select_tab', return_value=(Mock(), tab)), \
                patch.object(browser, 'evaluate', return_value={'ready': True, 'results': [], 'blocked': False}):
            self.assertEqual(browser.search('MDBK-380 uncensored'), [])
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
