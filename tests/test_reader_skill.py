import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from wx2md.core import ROOT, WebClient, run_batch


spec = importlib.util.spec_from_file_location(
    "reader_skill", ROOT / "skills/wechat-article-reader/scripts/read_article.py",
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
SOURCE = (ROOT / "tests/fixtures/article.html").read_text(encoding="utf-8")
URL = "https://mp.weixin.qq.com/s/valid"


class ReaderSkillTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.directory = Path(temporary.name)
        self.addCleanup(temporary.cleanup)

    def batch(self, image_failure=False):
        input_path = self.directory / "links.txt"
        input_path.write_text(URL + "\nhttps://mp.weixin.qq.com/s/verify\n", encoding="utf-8")
        client = Mock(spec=WebClient)
        client.article.side_effect = [SOURCE, "<p>环境异常，请去验证</p>"]
        client.image.return_value = (b"\x89PNG\r\n\x1a\nfixture", ".png")
        if image_failure:
            client.image.side_effect = requests.Timeout()
        folder, _ = run_batch(input_path, self.directory, client, lambda _: None, lambda _: None)
        return folder

    def test_prepare_preserves_existing_input_and_keeps_long_links_intact(self):
        # Isolate prepare's input directory without touching personal link files.
        with patch.object(reader, "PROJECT", self.directory):
            existing = self.directory / "input/links.txt"
            existing.parent.mkdir()
            existing.write_text("原有链接\n", encoding="utf-8")
            long_url = "https://mp.weixin.qq.com/s?__biz=x&mid=1&idx=1&sn=y"
            one = reader.prepare([URL, long_url, URL])
            two = reader.prepare([URL])
        self.assertNotEqual(one["input_file"], two["input_file"])
        self.assertEqual(Path(one["input_file"]).read_text(encoding="utf-8"), f"{URL}\n{long_url}\n{URL}\n")
        self.assertEqual(existing.read_text(encoding="utf-8"), "原有链接\n")
        self.assertEqual(one["command"][3], one["input_file"])
        with self.assertRaises(ValueError):
            reader.prepare([URL + "\n" + URL])

    def test_index_distinguishes_readable_article_from_verification_page(self):
        index = reader.batch_index(str(self.batch()))
        article, blocked = index["articles"]
        self.assertTrue(article["readable"])
        self.assertEqual(article["archive_status"], "成功")
        self.assertEqual(len(article["images"]), 2)
        self.assertTrue(all(Path(item["本地文件"]).is_file() for item in article["images"]))
        self.assertFalse(blocked["readable"])
        self.assertIsNone(blocked["markdown"])
        self.assertEqual(blocked["archive_status"], "需要验证")

    def test_failed_image_keeps_body_readable_without_claiming_archive_success(self):
        article = reader.batch_index(str(self.batch(image_failure=True)))["articles"][0]
        self.assertTrue(article["readable"])
        self.assertEqual(article["archive_status"], "其他失败")
        self.assertIn("备份失败", article["archive_error"])
        self.assertTrue(all("失败原因" in item for item in article["images"]))

    def test_chunked_read_reconstructs_complete_unicode_article(self):
        article = reader.batch_index(str(self.batch()))["articles"][0]
        offset, chunks = 0, []
        while True:
            chunk = reader.read_chunk(article["markdown"], offset, 71)
            chunks.append(chunk["content"])
            self.assertEqual(chunk["offset"], offset)
            self.assertGreater(chunk["next_offset"], offset)
            offset = chunk["next_offset"]
            if chunk["done"]:
                break
        self.assertEqual("".join(chunks), Path(article["markdown"]).read_text(encoding="utf-8"))
        self.assertEqual(offset, article["characters"])
        self.assertTrue(reader.read_chunk(article["markdown"], offset)["done"])
        for start, limit in ((-1, 100), (offset + 1, 100), (0, 0), (0, 8001)):
            with self.subTest(start=start, limit=limit), self.assertRaises(ValueError):
                reader.read_chunk(article["markdown"], start, limit)

    def test_read_rejects_outside_paths_and_symlinks(self):
        with self.assertRaises(ValueError):
            reader.read_chunk(str(ROOT.parent / "outside.md"))
        alias = self.directory / "outside.md"
        alias.symlink_to(ROOT.parent / "outside.md")
        with self.assertRaises(ValueError):
            reader.read_chunk(str(alias))
        with self.assertRaises(ValueError):
            reader.read_chunk(str(ROOT / "requirements.txt"))


if __name__ == "__main__":
    unittest.main()
