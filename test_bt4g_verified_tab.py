import unittest
from unittest.mock import Mock, patch

import check_bt4g_exists as checker


class VerifiedTabTests(unittest.TestCase):
    @patch.object(checker, "CdpClient")
    @patch.object(checker, "read_json")
    def test_attach_prefers_requested_query(self, read, client):
        read.return_value = [
            {"type": "page", "url": "https://115.com/", "webSocketDebuggerUrl": "ws://localhost:9000/a"},
            {"type": "page", "url": checker.BASE_URL + "/search?q=OTHER", "webSocketDebuggerUrl": "ws://localhost:9000/b"},
            {"type": "page", "url": checker.BASE_URL + "/search?q=OREB-003", "webSocketDebuggerUrl": "ws://localhost:9000/c"},
        ]
        checker.attach_running_browser("OREB-003", 9000)
        client.assert_called_once_with("ws://localhost:9000/c", "http://127.0.0.1:9000")

    @patch.object(checker, "read_json", return_value=[])
    def test_attach_does_not_create_browser(self, read):
        with self.assertRaisesRegex(RuntimeError, "No open BT4G"):
            checker.attach_running_browser("OREB-003", 9000)

    @patch.object(checker, "read_json")
    def test_attach_rejects_remote_websocket(self, read):
        read.return_value = [{"type": "page", "url": checker.BASE_URL,
                              "webSocketDebuggerUrl": "ws://example.com/a"}]
        with self.assertRaisesRegex(RuntimeError, "non-local"):
            checker.attach_running_browser("OREB-003", 9000)

    def test_default_is_current_tab(self):
        self.assertEqual(checker.build_parser().parse_args(["OREB-003"]).browser, "current")

    @patch.object(checker, "wait_for_result_cdp", return_value={"count": 2})
    def test_verified_search_is_not_reloaded(self, wait):
        client = Mock()
        client.evaluate.return_value = checker.BASE_URL + "/search?q=OREB-003"
        self.assertEqual(checker.check_query_cdp(client, "OREB-003")["exists"], "yes")
        client.call.assert_not_called()

    @patch.object(checker, "wait_for_result_cdp", return_value={"count": 0})
    def test_resume_only_reads_page(self, wait):
        client = Mock()
        checker.check_query_cdp(client, "OREB-003", resume=True)
        client.call.assert_not_called()
        client.evaluate.assert_not_called()

    @patch.object(checker, "wait_for_result_existing", return_value={"count": 1})
    @patch.object(checker, "navigate_existing_chrome")
    def test_new_window_resume_does_not_reload(self, navigate, wait):
        checker.check_query_existing(10, "OREB-003", resume=True)
        navigate.assert_not_called()

    @patch.object(checker, "wait_for_result_cdp", return_value={"count": 1})
    def test_different_query_navigates(self, wait):
        client = Mock()
        client.evaluate.return_value = checker.BASE_URL
        checker.check_query_cdp(client, "OREB-003")
        client.call.assert_called_once_with(
            "Page.navigate", {"url": checker.BASE_URL + "/search?q=OREB-003"}
        )

    @patch.object(checker, "run_applescript")
    def test_tab_is_pinned_and_never_closed(self, run):
        run.side_effect = ["12\n34\nhttps://bt4gprx.com/search?q=OREB-003", '"ok"']
        client = checker.CurrentChromeClient()
        self.assertEqual(client.evaluate("'ok'"), "ok")
        self.assertIn("set targetTab to tab id 34 of window id 12", run.call_args.args[0])
        client.close()
        self.assertEqual(run.call_count, 2)

    @patch.object(checker, "run_applescript", return_value="12\n34\nhttps://example.com/")
    def test_wrong_site_rejected(self, run):
        with self.assertRaisesRegex(RuntimeError, "Select the verified"):
            checker.CurrentChromeClient()


if __name__ == "__main__":
    unittest.main()
