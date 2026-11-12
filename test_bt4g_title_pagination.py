import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import check_bt4g_titles as checker


CODE = "EXAMPLE-001"
FIRST = checker.BASE_URL + "/search?q=" + CODE


def item(suffix, title=None):
    return {"title": title or CODE, "url": checker.BASE_URL + "/magnet/" + suffix, "size": "1 GB"}


class Browser:
    def __init__(self, pages):
        self.pages = pages
        self.url = ""
        self.index = 0
        self.visited = []

    def evaluate(self, script):
        if script == "location.href":
            return self.url
        if script == checker.READ_RESULTS:
            return {"results": self.pages[self.index][0]}
        if script == checker.READ_PAGINATION:
            return self.pages[self.index][1]
        raise AssertionError("Unexpected script")

    def call(self, method, params):
        assert method == "Page.navigate"
        self.url = params["url"]
        self.index = int(parse_qs(urlsplit(self.url).query).get("p", ["1"])[0]) - 1
        self.visited.append(self.url)


class PaginationTests(unittest.TestCase):
    def test_thai_subtitle_is_not_an_ai_marker(self):
        title = "SSNI-014 Arina Hashimoto [Sub Thai] แพ้กลิ่นน้องสาวแฟน_"
        self.assertEqual(checker.match_title(title), [])
        for title in ["EXAMPLE-001 Thai", "EXAMPLE-001 Dubai", "EXAMPLE-001 productUC"]:
            with self.subTest(title=title):
                self.assertEqual(checker.match_title(title), [])

    def test_explicit_markers_still_match(self):
        for title, markers in [
            ("SSNI-014-U", ["-U"]),
            ("SSNI-014-UC", ["UC"]),
            ("SSNI-014 [AI]", ["AI"]),
            ("SSNI-014-AI", ["AI"]),
            ("SSNI-014 uncensored", ["uncensored"]),
        ]:
            with self.subTest(title=title):
                self.assertEqual(checker.match_title(title), markers)

    def test_reads_all_pages_deduplicates_and_prioritizes_late_match(self):
        normal, ai, uncensored = item("normal"), item("ai", CODE + " AI"), item("uncensored", CODE + "-U")
        client = Browser([
            ([normal, ai], [FIRST + "&p=2", FIRST + "&p=3"]),
            ([ai, item("other")], [FIRST + "&p=3"]),
            ([uncensored], []),
        ])
        with patch.object(checker, "wait_for_result_cdp", return_value={"count": 4}):
            row = checker.search(client, CODE, delay=0)
        self.assertEqual(row["pages_checked"], 3)
        self.assertEqual(row["titles_checked"], 4)
        self.assertEqual(row["scope"], "all_pages")
        self.assertEqual(row["status"], "complete")
        self.assertEqual(checker.preferred_link(row), uncensored["url"])

    def test_no_markers_on_any_page_skips_hash_fetch(self):
        client = Browser([([item("one")], [FIRST + "&p=2"]), ([item("two")], [])])
        with patch.object(checker, "wait_for_result_cdp", return_value={"count": 2}):
            row = checker.search(client, CODE, delay=0)
        with patch.object(checker, "read_torrent_hash") as read:
            checker.add_torrent_hash(client, row)
            read.assert_not_called()
        self.assertEqual(checker.found_value(row), "no")
        self.assertEqual(row["torrent_hash"], "")

    def test_later_page_failure_does_not_claim_absence(self):
        client = Browser([([item("one")], [FIRST + "&p=2"]), ([], [])])
        with patch.object(checker, "wait_for_result_cdp", side_effect=[{"count": 2}, RuntimeError("Page failed")]):
            row = checker.search(client, CODE, delay=0)
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(checker.found_value(row), "unknown")
        self.assertEqual(row["pages_checked"], 1)

    def test_repeated_page_contents_are_detected(self):
        same = item("same")
        client = Browser([([same], [FIRST + "&p=2"]), ([same], [FIRST + "&p=3"])])
        with patch.object(checker, "wait_for_result_cdp", return_value={"count": 3}):
            row = checker.search(client, CODE, delay=0)
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("repeated", row["error"])

    def test_missing_pagination_is_incomplete_when_total_is_unread(self):
        client = Browser([([item("one")], [])])
        with patch.object(checker, "wait_for_result_cdp", return_value={"count": 10}):
            row = checker.search(client, CODE, delay=0)
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(checker.found_value(row), "unknown")


if __name__ == "__main__":
    unittest.main()
