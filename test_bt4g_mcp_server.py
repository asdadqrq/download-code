import unittest
from unittest.mock import Mock, patch

import bt4g_mcp_server as server


class ExactMatchTests(unittest.TestCase):
    @patch.object(server,'wait_for_result_cdp',return_value={'count':1})
    def test_suffixed_request_searches_base_and_follows_base_query_pagination(self,wait):
        client=Mock()
        page2=server.BASE_URL+'/search?q=IBW-495&p=2'
        def evaluate(script):
            if script==server.READ_RESULTS:
                return {'results':[{'title':'IBW-495Z.mp4','url':server.BASE_URL+'/magnet/z','size':'3 GB'}]}
            if script==server.READ_PAGES:
                return [page2] if wait.call_count==1 else []
            return ''
        client.evaluate.side_effect=evaluate
        row=server.collect_candidate_pages(client,'IBW-495Z')
        self.assertEqual(row['code'],'IBW-495Z')
        self.assertEqual(row['query_code'],'IBW-495')
        self.assertEqual(row['pages_checked'],2)
        self.assertEqual(row['candidates'][0]['title'],'IBW-495Z.mp4')
        self.assertEqual(wait.call_args_list[0].args[1],server.BASE_URL+'/search?q=IBW-495')
        self.assertEqual(wait.call_args_list[1].args[1],page2)
    @patch.object(server, 'wait_for_result_cdp', return_value={'count':1})
    def test_new_collector_never_uses_regex_identity_matching(self, wait):
        client=Mock()
        def evaluate(script):
            if script==server.READ_RESULTS:
                return {'results':[{'title':'Love 191 Photos.zip','url':server.BASE_URL+'/magnet/example','size':'3 GB'}]}
            if script==server.READ_PAGES:return []
            return ''
        client.evaluate.side_effect=evaluate
        with patch.object(server,'matches_code',side_effect=AssertionError('Identity must be reviewed by the model')):
            row=server.collect_candidate_pages(client,'LOVE-191')
        self.assertEqual(row['status'],'complete')
        self.assertEqual(len(row['candidates']),1)
        self.assertNotIn('magnet',row['candidates'][0])
    def test_literal_identifier_suffix_is_preserved(self):
        self.assertTrue(server.matches_code('IBW-601Z', '[ibw_601z].mp4'))
        self.assertFalse(server.matches_code('IBW-601Z', 'IBW-601.mp4'))
        self.assertFalse(server.matches_code('IBW-601', 'IBW-601Z.mp4'))
        self.assertFalse(server.matches_code('IBW-601Z', 'IBW-601ZZ.mp4'))
        self.assertTrue(server.matches_code('200GANA-1688', '200GANA-1688.mp4'))
        self.assertFalse(server.matches_code('GANA-1688', '200GANA-1688.mp4'))

    def test_accepts_code_separators_and_case(self):
        for title in ("[ONCE-031].avi", "once_031", "once 031", "ONCE031", "ONCE-031-HD"):
            with self.subTest(title=title):
                self.assertTrue(server.matches_code("ONCE-031", title))

    def test_rejects_original_false_positives_and_longer_identifiers(self):
        cases = [("ONCE-031", "KMHRS-031 Once something"),
                 ("FSET-261", "FSET-435 261"),
                 ("VICD-191", "VICD-272 ALB-191"),
                 ("AT-041", "MIDV-041 something at"),
                 ("NATURAL-109", "NATURAL HIGH NHDT-109"),
                 ("ONCE-031", "XONCE-031"), ("ONCE-031", "ONCE-0310"),
                 ("ONCE-031", "ONCE-031Z")]
        for code, title in cases:
            with self.subTest(code=code, title=title):
                self.assertFalse(server.matches_code(code, title))

    def test_invalid_input_does_not_attach_browser(self):
        with patch.object(server, "BT4GChromeClient") as client:
            for codes in ([], ["https://example.com"], ["A-1"] * 11):
                with self.assertRaises(ValueError):
                    server.bt4g_search_exact(codes)
            client.assert_not_called()

    @patch.object(server, "wait_for_result_cdp", return_value={"count": 1})
    def test_pagination_finds_exact_code_on_second_page(self, wait):
        client = Mock()
        page2 = server.BASE_URL + "/search?q=IENE-015&p=2"
        detail = server.BASE_URL + "/magnet/abc"
        pages = iter([
            {"results": [{"title": "015 IENE-382", "url": server.BASE_URL + "/magnet/wrong"}]},
            {"results": [{"title": "IENE-015", "url": detail}]},
        ])
        def evaluate(script):
            if script == "location.href":
                return ""
            if script == server.READ_RESULTS:
                return next(pages)
            if script == server.READ_PAGES:
                return [page2] if wait.call_count == 1 else []
        client.evaluate.side_effect = evaluate
        with patch.object(server, "read_detail", return_value={"hash": "a" * 40, "magnet": "magnet:?xt=urn:btih:" + "a" * 40, "title": "IENE-015"}) as read:
            row = server.search_code(client, "IENE-015")
        self.assertEqual(row["status"], "found")
        self.assertEqual(row["pages_checked"], 2)
        read.assert_called_once_with(client, detail, "IENE-015")
        self.assertNotIn("title", row["matches"][0])

    @patch.object(server, "wait_for_result_cdp", return_value={"count": 1})
    def test_page_limit_is_not_reported_as_absence(self, wait):
        client = Mock()
        def evaluate(script):
            if script == server.READ_RESULTS:
                return {"results": [{"title": "FSET-435 261", "url": server.BASE_URL + "/magnet/abc"}]}
            if script == server.READ_PAGES:
                return [server.BASE_URL + "/search?q=FSET-261&p=2"]
            return ""
        client.evaluate.side_effect = evaluate
        row = server.search_code(client, "FSET-261", max_pages=1)
        self.assertEqual(row["status"], "page_limit_reached")
        self.assertFalse(row["search_exhausted"])

    @patch.object(server, "wait_for_result_cdp", return_value={"blocked": True})
    def test_verification_never_prompts_stdin(self, wait):
        with patch("builtins.input", side_effect=AssertionError("stdin must belong to MCP")):
            row = server.search_code(Mock(), "ONCE-031")
        self.assertEqual(row["status"], "verification_required")

    def test_detail_title_must_match_before_hash_is_returned(self):
        client = Mock()
        url = server.BASE_URL + "/magnet/abc"
        client.evaluate.side_effect = [url, {"url": url, "title": "ONCE-032", "download": "https://downloadtorrentfile.com/hash/" + "a" * 40}]
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            server.read_detail(client, url, "ONCE-031")

    def test_extract_rejects_offsite_urls(self):
        with self.assertRaises(ValueError):
            server.bt4g_extract_magnets(["https://example.com/magnet/abc"])

    @patch.object(server, "wait_for_result_cdp", return_value={"count": 2})
    def test_retry_skips_failed_hash_and_returns_another(self, wait):
        client = Mock()
        def evaluate(script):
            if script == server.READ_RESULTS:
                return {"results": [
                    {"title": "ONCE-031", "url": server.BASE_URL + "/magnet/one"},
                    {"title": "ONCE-031", "url": server.BASE_URL + "/magnet/two"},
                ]}
            if script == server.READ_PAGES:
                return []
            return ""
        client.evaluate.side_effect = evaluate
        with patch.object(server, "read_detail", side_effect=[
            {"hash": "a" * 40, "magnet": "magnet:?xt=urn:btih:" + "a" * 40},
            {"hash": "b" * 40, "magnet": "magnet:?xt=urn:btih:" + "b" * 40},
        ]):
            row = server.search_code(client, "ONCE-031", exclude_hashes=["a" * 40])
        self.assertEqual(row['status'], 'found')
        self.assertEqual(row['matches'][0]['hash'], 'b' * 40)


if __name__ == "__main__":
    unittest.main()
