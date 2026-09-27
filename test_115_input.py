import contextlib
import io
import unittest
from unittest.mock import Mock

from check_115_exists import parse_codes, filename_is_uncensored, select_115_tab, build_parser


class ChromeTabTests(unittest.TestCase):
    def test_uses_open_115_tab_from_correct_chrome_process(self):
        old_profile = Mock()
        old_tab = Mock()
        old_tab.URL.return_value = "https://www.google.com/sorry/index"
        old_profile.windows.return_value = [Mock(**{
            "activeTab.return_value": old_tab, "tabs.return_value": [old_tab]
        })]
        normal_profile = Mock()
        file_tab = Mock()
        file_tab.URL.return_value = "https://115.com/?cid=0&mode=wangpan"
        normal_profile.windows.return_value = [Mock(**{
            "activeTab.return_value": file_tab, "tabs.return_value": [file_tab]
        })]
        self.assertEqual(select_115_tab([old_profile, normal_profile]), (normal_profile, file_tab))

    def test_default_reuses_existing_chrome(self):
        self.assertEqual(build_parser().parse_args(["UZU-021"]).browser, "existing")


class SuffixTests(unittest.TestCase):
    def test_resolution_before_marker(self):
        for code, name in [
            ("MKMP-540", "MKMP-540-4K-U"),
            ("MKMP-540", "www.98T.la@mkmp-540-4K-U.mp4"),
            ("MMUS-078", "mmus-078-4k-U"),
            ("MKMP-540", "MKMP-540-1080p-UC"),
            ("MKMP-540", "MKMP-540-2160p-uncensored"),
            ("MKMP-540", "MKMP-540-4K-U\u7248"),
        ]:
            with self.subTest(name=name):
                self.assertTrue(filename_is_uncensored(name, code))

    def test_resolution_without_marker_is_not_uncensored(self):
        for name in ["MMUS-078-4k_003^WM.mp4", "MMUS-078-4K", "MMUS-078-4K-UHD",
                     "MMUS-078-4K-UC2", "MMUS-078-4K-Ucut", "MMUS-078-4K-other-U",
                     "MMUS-079-4K-U"]:
            with self.subTest(name=name):
                self.assertFalse(filename_is_uncensored(name, "MMUS-078"))

    def test_chinese_annotations(self):
        for code, filename in [
            ("NNPJ-507", "nnpj-507-U\u7edd\u7f8e\u7ea2\u8272\u6bdb\u8863"),
            ("IPX-785", "IPX-785-U\u73cd\u73e0\u9879\u94fe"),
            ("IPX-785", "IPX-785-UC\u73cd\u73e0.mp4"),
            ("IPX-785", "IPX-785-uncensored\u7248.mp4"),
        ]:
            with self.subTest(filename=filename):
                self.assertTrue(filename_is_uncensored(filename, code))

    def test_longer_ascii_suffixes_are_not_markers(self):
        for suffix in ["UHD", "UNCUT", "UC2", "UNCENSOREDLY", "AI"]:
            with self.subTest(suffix=suffix):
                self.assertFalse(filename_is_uncensored("IPX-785-" + suffix, "IPX-785"))

    def test_existing_markers_and_wrong_codes(self):
        for suffix in ["U", "uc.mp4", "uncensored-HD", "U_iris2.mp4"]:
            self.assertTrue(filename_is_uncensored("IPX-785-" + suffix, "IPX-785"))
        self.assertFalse(filename_is_uncensored("IPX-786-U\u7248", "IPX-785"))


class InputTests(unittest.TestCase):
    def parse(self, values):
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            codes = parse_codes(values)
        return codes, warning.getvalue()

    def test_stray_price_does_not_abort_batch(self):
        codes, warning = self.parse(["MMUS-067,-NAD 8.00,BLK-591,BF-666,IPX-767"])
        self.assertEqual(codes, ["MMUS-067", "BLK-591", "BF-666", "IPX-767"])
        self.assertIn("-NAD, 8.00", warning)

    def test_shell_split_arguments(self):
        codes, warning = self.parse(["MMUS-067,-NAD", "8.00,BLK-591"])
        self.assertEqual(codes, ["MMUS-067", "BLK-591"])
        self.assertIn("skipped invalid input", warning)

    def test_valid_formats_and_deduplication(self):
        codes, warning = self.parse(["umso-650,T38-030,300NTK-545,HODV-21927,KING-0092,UMSO-650"])
        self.assertEqual(codes, ["UMSO-650", "T38-030", "300NTK-545", "HODV-21927", "KING-0092"])
        self.assertEqual(warning, "")

    def test_only_invalid_input_stops_before_browser(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "No valid codes"):
                parse_codes(["-NAD 8.00"])


if __name__ == "__main__":
    unittest.main()
