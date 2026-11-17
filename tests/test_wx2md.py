import csv
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import yaml
from bs4 import BeautifulSoup

from wx2md.core import (
    ROOT, ArticleError, WebClient, article_path, link_key, normalize_url,
    parse_article, project_path, publication_time, read_links, run_batch,
    save_article,
)


SOURCE = (Path(__file__).parent / "fixtures/article.html").read_text(encoding="utf-8")
URL = "https://mp.weixin.qq.com/s/valid"
PNG = b"\x89PNG\r\n\x1a\nfixture"


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def fake_client(self, pages=None):
        client = Mock(spec=WebClient)
        client.article.side_effect = lambda url: (pages or {}).get(url, SOURCE)
        client.image.return_value = (PNG, ".png")
        return client

    def batch(self, text, client=None):
        path = self.directory / "links.txt"
        path.write_text(text, encoding="utf-8-sig")
        client = client or self.fake_client()
        waits, logs = [], []
        folder, report = run_batch(path, self.directory / "output", client, waits.append, logs.append)
        with (folder / "_总表.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        return folder, report, rows, client, waits

    def test_link_cleanup_and_identity(self):
        self.assertEqual(normalize_url(" 《mp.weixin.qq.com/s/abc_-。#rd》 "), "https://mp.weixin.qq.com/s/abc_-")
        a = normalize_url("http://mp.weixin.qq.com/s?sn=one&idx=1&mid=7&__biz=x&amp;scene=12#rd")
        b = normalize_url("https://mp.weixin.qq.com/s?__biz=x&mid=7&idx=1&sn=two")
        self.assertEqual(link_key(a), link_key(b))
        for invalid in ("https://example.org/s/a", "https://mp.weixin.qq.com.evil.test/s/a", "https://mp.weixin.qq.com/", "https://mp.weixin.qq.com/s?sn=x", "https://user@mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com:8000/s/a", "just wrong"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_url(invalid)

    def test_read_links_keeps_original_and_records_duplicates(self):
        path = self.directory / "links.txt"
        path.write_text("\ufeff\n# 备注\n 《mp.weixin.qq.com/s/abc#rd》\nhttps://mp.weixin.qq.com/s/abc?scene=1\nwrong\n", encoding="utf-8")
        links, duplicates = read_links(path)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].original, "《mp.weixin.qq.com/s/abc#rd》")
        self.assertEqual(duplicates[0]["首次行号"], 3)
        self.assertTrue(links[1].error)

    def test_article_metadata_body_and_image_positions(self):
        article = parse_article(SOURCE, URL)
        self.assertEqual(article.title, "2026 升学指南：分数与志愿")
        self.assertEqual(article.account, "升学观察")
        self.assertEqual(article.author, "张老师")
        self.assertEqual(article.published, "2026-11-16 11:30:00")
        self.assertIn("## 一、先看规则", article.body)
        self.assertIn("**重要提醒**", article.body)
        self.assertIn("**保留加粗**", article.body)
        self.assertIn("1. 第一步", article.body)
        self.assertIn("核对科目", article.body)
        self.assertIn("> > 第二层引用", article.body)
        self.assertIn("[政策原文](<https://example.org/info(a)?x=1&y=2>)", article.body)
        self.assertEqual(re.findall(r"【图(\d+)】", article.body), ["1", "1", "2", "2"])
        self.assertLess(article.body.index("图片前的一段"), article.body.index("【图1】"))
        self.assertLess(article.body.index("example1"), article.body.index("图片后的一段"))
        for forbidden in ("console", "color:red", "隐藏内容", "点赞", "扫一扫关注"):
            self.assertNotIn(forbidden, article.body)
        self.assertEqual(len(article.images), 2)
        self.assertEqual(article.images[0], "https://mmbiz.qpic.cn/example1?wx_fmt=png")

    def test_dates_and_missing_author(self):
        article = parse_article(SOURCE.replace('<span class="rich_media_meta_text">张老师</span>', ""), URL)
        self.assertEqual(article.author, "")
        for markup, expected in (
            ('<em id="publish_time">2026年11月16日 09:20</em>', "2026-11-16 09:20:00"),
            ('<meta property="article:published_time" content="2026-11-16T00:30:00Z">', "2026-11-16 08:30:00"),
            ('<em id="publish_time">2026-02-30</em>', ""),
        ):
            self.assertEqual(publication_time(markup, BeautifulSoup(markup, "html.parser")), expected)

    def test_script_metadata_fallback(self):
        source = '''<script>var msg_title = "中文\\x26标题"; var nickname='公众号'; var ct=1794799800;</script><div id="js_content"><p>正文</p></div>'''
        article = parse_article(source, URL)
        self.assertEqual(article.title, "中文&标题")
        self.assertEqual(article.account, "公众号")

    def test_nonstandard_message_with_description(self):
        source = '<h1 id="activity-name">图片消息</h1><a id="js_name">公众号</a><em id="publish_time">2026-11-16</em><div id="js_image_desc"><p>图片消息里可归档的描述文字。</p></div>'
        article = parse_article(source, URL)
        self.assertIn("可归档的描述文字", article.body)
        self.assertEqual(article.published, "2026-11-16")

    def test_follow_guidance_removed_without_losing_adjacent_paragraph(self):
        prompt = "哈喽，我是阿林老师，在教育行业摸爬滚打十余年，喜欢我的文章，请点个关注哦！"
        source = f'<div id="js_content"><section><span><span style="font-weight: bold">{prompt}</span></span><br><br><span>第一段正文。</span><br><br><span>建议关注孩子的情绪，喜欢我的文章也不应影响这一段。</span></section><section class="mp_profile_iframe_wrp"><mp-common-profile>关注我们</mp-common-profile></section></div>'
        article = parse_article(source, URL)
        self.assertNotIn(prompt, article.body)
        self.assertNotIn("关注我们", article.body)
        self.assertIn("第一段正文", article.body)
        self.assertIn("建议关注孩子的情绪", article.body)
        self.assertEqual(article.removed_guidance, [prompt])

    def test_follow_only_message_has_no_archivable_text(self):
        with self.assertRaises(ArticleError) as caught:
            parse_article('<div id="js_content"><p>点击上方蓝字，关注公众号。</p></div>', URL)
        self.assertEqual(caught.exception.status, "不是图文文章")

    def test_linked_image_retains_image_position_and_click_target(self):
        source = '<div id="js_content"><p>前文</p><a href="https://example.org/policy"><img data-src="https://mmbiz.qpic.cn/test.png"></a><p>后文</p></div>'
        article = parse_article(source, URL)
        self.assertIn('[图片超链接](<https://example.org/policy>)', article.body)
        self.assertIn('![](<https://mmbiz.qpic.cn/test.png>)', article.body)
        self.assertLess(article.body.index('前文'), article.body.index('【图1】'))
        self.assertLess(article.body.index('【图1】'), article.body.index('后文'))
        self.assertEqual(re.findall(r'【图(\d+)】', article.body), ['1', '1'])

    def test_failure_classification(self):
        cases = (
            ("<p>环境异常，请去验证</p>", "需要验证"),
            ("<p>该内容已被发布者删除</p>", "文章已删除或违规"),
            ("<p>此内容因投诉无法查看</p>", "文章已删除或违规"),
            ('<div id="js_content"><img src="https://example.org/a.png"></div>', "不是图文文章"),
            ("<p>这是视频号消息</p>", "不是图文文章"),
            ("<p>链接已过期</p>", "其他失败"),
            ("<html><body>unknown error</body></html>", "其他失败"),
        )
        for source, state in cases:
            with self.subTest(state=state, source=source), self.assertRaises(ArticleError) as caught:
                parse_article(source, URL)
            self.assertEqual(caught.exception.status, state)

    def test_batch_success_bad_link_duplicate_and_frontmatter(self):
        folder, report, rows, client, waits = self.batch(URL + "#rd\n\nwrong link\n" + URL + "?scene=1\n")
        self.assertEqual(report["状态统计"]["成功"], 1)
        self.assertEqual(report["状态统计"]["其他失败"], 1)
        self.assertEqual(report["跳过重复数"], 1)
        self.assertEqual(client.article.call_count, 1)
        self.assertEqual(len(list(folder.glob("*.md"))), 1)
        self.assertEqual(len(rows), 2)
        markdown = (folder / rows[0]["md文件名"]).read_text(encoding="utf-8")
        metadata = yaml.safe_load(markdown.split("---\n", 2)[1])
        self.assertEqual(list(metadata), ["标题", "公众号", "作者", "发布时间", "原链接", "抓取时间", "正文字数", "图片数"])
        self.assertEqual(metadata["原链接"], URL + "#rd")
        backups = folder / (Path(rows[0]["md文件名"]).stem + "_images")
        self.assertEqual(len(list(backups.iterdir())), int(rows[0]["图片数"]))
        self.assertTrue((folder / "_总表.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(len(waits), 1)  # one image-to-image wait, no invalid-URL request

    def test_validation_stop_and_unrequested_rows(self):
        client = self.fake_client()
        client.article.return_value = "<p>环境异常 去验证</p>"
        client.article.side_effect = None
        folder, report, rows, _, waits = self.batch("\n".join(f"https://mp.weixin.qq.com/s/test{i}" for i in range(7)), client)
        self.assertEqual(client.article.call_count, 3)
        self.assertEqual(report["验证页占请求比例"], 1)
        self.assertTrue(report["因验证过多提前停止"])
        self.assertEqual([row["状态"] for row in rows], ["需要验证"] * 3 + ["其他失败"] * 4)
        self.assertIn("未请求", rows[3]["失败原因"])
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(3 <= delay <= 6 for delay in waits))
        self.assertEqual(len(list(folder.glob("*.md"))), 0)

    def test_mixed_errors_continue_and_collision_gets_suffix(self):
        pages = {
            "https://mp.weixin.qq.com/s/deleted": "<p>该内容已被发布者删除</p>",
            "https://mp.weixin.qq.com/s/verify": "<p>环境异常 去验证</p>",
        }
        folder, report, rows, _, _ = self.batch("\n".join("https://mp.weixin.qq.com/s/" + name for name in ("a", "deleted", "verify", "b")), self.fake_client(pages))
        self.assertEqual([row["状态"] for row in rows], ["成功", "文章已删除或违规", "需要验证", "成功"])
        self.assertTrue(rows[3]["md文件名"].endswith("_2.md"))
        self.assertEqual(len(list(folder.glob("*.md"))), 2)
        self.assertFalse(report["因验证过多提前停止"])

    def test_failed_image_never_reported_as_success_or_retried(self):
        client = self.fake_client()
        client.article.side_effect = None
        client.article.return_value = SOURCE.replace("https://mmbiz.qpic.cn/example2?wx_fmt=gif", "//mmbiz.qpic.cn/example1?wx_fmt=png")
        client.image.side_effect = requests.Timeout()
        folder, report, rows, _, _ = self.batch(URL, client)
        self.assertEqual(rows[0]["状态"], "其他失败")
        self.assertIn("图1备份失败", rows[0]["失败原因"])
        self.assertIn("图2备份失败", rows[0]["失败原因"])
        self.assertEqual(client.image.call_count, 1)
        self.assertTrue((folder / rows[0]["md文件名"]).exists())
        self.assertEqual(report["状态统计"]["成功"], 0)

    def test_missing_metadata_keeps_body_but_fails(self):
        client = self.fake_client()
        client.article.side_effect = None
        client.article.return_value = '<div id="js_content"><p>需要保留的正文</p></div>'
        folder, _, rows, _, _ = self.batch(URL, client)
        self.assertEqual(rows[0]["状态"], "其他失败")
        self.assertIn("标题、公众号、发布时间", rows[0]["失败原因"])
        self.assertIn("需要保留的正文", (folder / rows[0]["md文件名"]).read_text(encoding="utf-8"))

    def test_network_failure_then_success(self):
        client = self.fake_client()
        client.article.side_effect = [requests.ConnectionError(), SOURCE]
        _, report, rows, _, _ = self.batch(URL + "\nhttps://mp.weixin.qq.com/s/second", client)
        self.assertEqual([row["状态"] for row in rows], ["其他失败", "成功"])
        self.assertIn("网络连接失败", rows[0]["失败原因"])
        self.assertEqual(report["文章请求数"], 2)

    def test_keyboard_interrupt_retains_summary(self):
        client = self.fake_client()
        client.article.side_effect = KeyboardInterrupt()
        folder, report, rows, _, _ = self.batch(URL + "\nhttps://mp.weixin.qq.com/s/second", client)
        self.assertTrue(report["用户中断"])
        self.assertEqual(client.article.call_count, 1)
        self.assertIn("未请求", rows[1]["失败原因"])
        self.assertEqual(json.loads((folder / "_运行报告.json").read_text(encoding="utf-8"))["唯一链接数"], 2)

    def test_project_boundary_and_symlink(self):
        with self.assertRaises(ValueError):
            project_path(str(ROOT.parent / "outside.txt"))
        alias = self.directory / "outside"
        alias.symlink_to(ROOT.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            project_path(str(alias / "something.txt"))

    def test_unsafe_title_characters_and_unicode_name_length(self):
        article = parse_article(SOURCE, URL)
        article.title = '../a:b/c\\d*e?f|g<>"\n'
        path = article_path(self.directory, article)
        self.assertEqual(path.parent, self.directory)
        self.assertNotRegex(path.name, r'[<>:"/\\|?*\n]')
        article.account = "😀" * 100
        article.title = "😀" * 100
        path = article_path(self.directory, article)
        self.assertLess(len((path.stem + "_images").encode("utf-8")), 256)


class TransportTest(unittest.TestCase):
    def response(self, status=200, data=b"ok", headers=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status_code = status
        response.headers = headers or {}
        response.iter_content.return_value = iter([data])
        return response

    def test_429_and_captcha_redirect_no_retry(self):
        for response in (
            self.response(429),
            self.response(302, headers={"Location": "/mp/wappoc_appmsgcaptcha?x=1"}),
        ):
            client = WebClient()
            with patch.object(client.session, "get", return_value=response) as get:
                with self.assertRaises(ArticleError) as caught:
                    client.article(URL)
                self.assertEqual(caught.exception.status, "需要验证")
                self.assertEqual(get.call_count, 1)

    def test_external_redirect_is_not_followed(self):
        client = WebClient()
        with patch.object(client.session, "get", return_value=self.response(302, headers={"Location": "https://example.org"})) as get:
            with self.assertRaises(ArticleError):
                client.article(URL)
            self.assertEqual(get.call_count, 1)

    def test_image_html_error_rejected_and_formats_detected(self):
        client = WebClient()
        with patch.object(client, "_get", return_value=("<html>去验证</html>".encode(), "image/png")):
            with self.assertRaises(ValueError):
                client.image("https://mmbiz.qpic.cn/test", URL)
        for data, ext in ((PNG, ".png"), (b"\xff\xd8\xffdata", ".jpg"), (b"GIF89adata", ".gif"), (b"RIFFxxxxWEBPdata", ".webp")):
            with patch.object(client, "_get", return_value=(data, "application/octet-stream")):
                self.assertEqual(client.image("https://mmbiz.qpic.cn/test", URL), (data, ext))


if __name__ == "__main__":
    unittest.main()
