import unittest
from unittest.mock import patch

import check_google_uncensored as checker


class ProviderPriorityTests(unittest.TestCase):
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
