import unittest
from unittest.mock import patch

import extract_seesaawiki_codes as extractor


class ExtractionTests(unittest.TestCase):
    def test_non_padded_date(self):
        rows = self.extract('<div class="user-area">2026/5/27 HONB-486<br>Plain text</div>')
        self.assertEqual((rows[0]['code'], rows[0]['release_date']), ('HONB-486','2026/05/27'))

    def test_changed_date_ignores_deleted_old_date(self):
        rows = self.extract('''<div class="user-area">2025/02/21 発売日変更<del>2025/01/24</del> SAVR-539<br>
            <a href="https://video.dmm.co.jp/av/content/?id=savr00539">Example title</a></div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['code'],rows[0]['release_date']), ('SAVR-539','2025/02/21'))

    def test_lowercase_multi_part_code(self):
        rows = self.extract('''<div class="user-area">2025/10/11 scute-1535-sakura<br>
            <a href="https://video.dmm.co.jp/amateur/content/?id=scute1535">Example title</a></div>''')
        self.assertEqual(rows[0]['code'], 'SCUTE-1535-SAKURA')

    def test_fc2_plain_title_and_complete_code(self):
        rows = self.extract('''<div class="user-area">2023/06/12 FC2-PPV-3396287<br>
            Plain title<br><br>2023/05/31 OTHER-001<br>
            <a href="https://video.dmm.co.jp/av/content/?id=other">Different title</a></div>''')
        self.assertEqual(rows[0]['code'], 'FC2-PPV-3396287')
        self.assertEqual(rows[0]['title'], 'Plain title')
        self.assertEqual(rows[0]['detail_url'], '')

    def test_fc2_article_link(self):
        rows = self.extract('''<div class="user-area">2023/07/14 FC2PPV-3582565 FC2PPV 3582565<br>
            <a href="https://adult.contents.fc2.com/article/3582565/">Example title</a></div>''')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['code'],'FC2PPV-3582565')
        self.assertEqual(rows[0]['title'],'Example title')

    def test_inline_date_and_code(self):
        rows = self.extract('<div class="user-area"><b>2026/5/27</b> <span>HONB-486</span><br>Plain text</div>')
        self.assertEqual(rows[0]['code'],'HONB-486')

    def test_prestige_release(self):
        rows = self.extract('''<div class="user-area">2021/07/09 ABW-112<br>
            <a href="https://www.prestige-av.com/goods/goods_detail.php?sku=abw-112">Example title</a>
            </div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "ABW-112")
        self.assertEqual(rows[0]["release_date"], "2021/07/09")
        self.assertEqual(rows[0]["title"], "Example title")

    def test_unknown_provider_keeps_code_without_borrowing_title(self):
        rows = self.extract('''<div class="user-area">2021/07/09 EXAMPLE-001<br>
            <a href="https://example.org/product/1">Unrecognized title</a>
            </div>''')
        self.assertEqual(rows[0]["code"], "EXAMPLE-001")
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["detail_url"], "")

    def test_prestige_images_and_lookalikes_are_rejected(self):
        self.assertFalse(extractor.is_detail_link("https://www.prestige-av.com/api/media/goods/example.jpg"))
        self.assertFalse(extractor.is_detail_link("https://prestige-av.com.example.org/goods/goods_detail.php?sku=test"))

    def test_new_dmm_urls_preserve_source_codes(self):
        for code, category, cid in [
            ("ORENA-110", "amateur", "orena110"),
            ("3DSVR-1847", "av", "13dsvr01847"),
            ("COSX-099", "av", "cosx00099"),
        ]:
            with self.subTest(code=code):
                rows = self.extract(f'''<div class="user-area">2025/12/01 {code}<br>
                    <a href="https://video.dmm.co.jp/{category}/content/?id={cid}">Example title</a></div>''')
                self.assertEqual([row["code"] for row in rows], [code])
                self.assertEqual(rows[0]["title"], "Example title")

    def test_new_dmm_url_requires_product_id(self):
        for url in ["https://video.dmm.co.jp/av/content/", "https://video.dmm.co.jp/av/content/?id=",
                    "https://video.dmm.co.jp.example.com/av/content/?id=test"]:
            self.assertFalse(extractor.is_detail_link(url))

    def test_reference_keeps_missing_metadata_blank(self):
        rows = self.extract('''<div class="user-area">2023/06/13 EXAMPLE-001<br>
            <a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=example001/">Compilation title</a><br>
            Includes <a href="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=h_021clt00064/">CLT-064</a>
            </div>''')
        self.assertEqual([r["code"] for r in rows], ["EXAMPLE-001", "CLT-064"])
        self.assertEqual(rows[1]["release_date"], "")
        self.assertEqual(rows[1]["title"], "")

    def test_reference_is_not_used_as_another_release_title(self):
        rows = self.extract('''<div class="user-area">2023/06/13 MISSING-001<br>
            Includes <a href="https://video.dmm.co.jp/av/content/?id=clt00064">CLT-064</a></div>''')
        self.assertEqual([r["code"] for r in rows], ["MISSING-001", "CLT-064"])
        self.assertEqual(rows[0]["title"], "")

    def test_reference_duplicate_does_not_replace_dated_entry(self):
        rows = self.extract('''<div class="user-area">
            <a href="https://video.dmm.co.jp/av/content/?id=clt00064">CLT-064</a><br>
            2023/06/13 CLT-064<br>
            <a href="https://video.dmm.co.jp/av/content/?id=clt00064">Actual title</a><br>
            <a href="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=clt00064/">CLT-064</a></div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Actual title")
        self.assertEqual(rows[0]["release_date"], "2023/06/13")

    def extract(self, html):
        with patch.object(extractor, "fetch_html", return_value=html):
            return extractor.extract_page("https://seesaawiki.jp/w/sougouwiki/d/test")

    def test_mgstage_entry_at_end_of_page(self):
        rows = self.extract('''<div class="user-area">2023/05/31 200GANA-2851<br>
            <a href="https://www.mgstage.com/product/product_detail/200GANA-2851/">Example title</a>
            </div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "200GANA-2851")
        self.assertEqual(rows[0]["title"], "Example title")
        self.assertEqual(rows[0]["release_date"], "2023/05/31")

    def test_mgstage_does_not_take_later_dmm_title(self):
        rows = self.extract('''<div class="user-area">2023/05/31 200GANA-2851<br>
            <a href="https://www.mgstage.com/product/product_detail/200GANA-2851/">First title</a><br>
            2023/05/30 EXAMPLE-001<br>
            <a href="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=example001/">Second title</a>
            </div>''')
        self.assertEqual([r["title"] for r in rows], ["First title", "Second title"])

    def test_missing_link_never_borrows_next_release(self):
        rows = self.extract('''<div class="user-area">2023/05/31 MISSING-001<br>
            Plain text<br>2023/05/30 EXAMPLE-001<br>
            <a href="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=example001/">Second title</a>
            </div>''')
        self.assertEqual([r["code"] for r in rows], ["MISSING-001", "EXAMPLE-001"])
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[1]["title"], "Second title")

    def test_external_lookalike_is_not_a_provider(self):
        self.assertFalse(extractor.is_detail_link("https://mgstage.com.example.org/product/product_detail/test/"))
        self.assertFalse(extractor.is_detail_link("https://image.mgstage.com/images/test.jpg"))


if __name__ == "__main__":
    unittest.main()
