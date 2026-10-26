import unittest
from unittest.mock import Mock
import check_115_exists_mcp as checker


def page(items, total, offset=0, next_offset=None):
    return {'cid':'123', 'offset':offset, 'total':total, 'items':items,
            'has_more':next_offset is not None, 'next_offset':next_offset}


class SearchTests(unittest.TestCase):
    def test_all_pages_and_resolved_scope(self):
        client = Mock()
        client.search_115.side_effect = [
            page([{'type':'dir','id':'1','name':'MIDE-979'}], 2, next_offset=1),
            page([{'type':'file','id':'2','name':'MIDE-979-U.mp4'}], 2, offset=1),
        ]
        row = checker.check_code(client, 'MIDE-979', '/Selected', page_size=1, delay=0)
        self.assertEqual(row['exists'], 'yes')
        self.assertEqual(row['uncensored'], 'yes')
        self.assertEqual(row['result_count'], 2)
        self.assertEqual(client.search_115.call_args_list[1].args, ('MIDE-979', '123', 1, 1))

    def test_no_results(self):
        client = Mock()
        client.search_115.return_value = page([], 0)
        row = checker.check_code(client, 'MIDE-979', '123')
        self.assertEqual(row['exists'], 'no')
        self.assertEqual(row['error'], '')

    def test_failure_is_unknown(self):
        client = Mock()
        client.search_115.side_effect = RuntimeError('Search failed')
        row = checker.check_code(client, 'MIDE-979', '123')
        self.assertEqual(row['exists'], 'unknown')

    def test_limit_preserves_positive_but_not_absence(self):
        client = Mock()
        client.search_115.return_value = page(
            [{'type':'dir','id':'1','name':'MIDE-979'}], 10, next_offset=1)
        row = checker.check_code(client, 'MIDE-979', '123', max_pages=1, delay=0)
        self.assertEqual(row['exists'], 'yes')
        self.assertEqual(row['uncensored'], 'unknown')

    def test_duplicate_page_is_incomplete(self):
        client = Mock()
        item = {'type':'file','id':'1','name':'another-file.mp4'}
        client.search_115.side_effect = [page([item], 2, next_offset=1), page([item], 2, offset=1)]
        row = checker.check_code(client, 'MIDE-979', '123', delay=0)
        self.assertEqual(row['exists'], 'unknown')
        self.assertIn('repeated', row['error'])


if __name__ == '__main__':
    unittest.main()
