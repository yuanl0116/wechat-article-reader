from __future__ import annotations

import base64
import csv
import html
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


ROOT = Path(__file__).resolve().parent.parent
SHANGHAI = ZoneInfo("Asia/Shanghai")
STATES = ("成功", "文章已删除或违规", "需要验证", "不是图文文章", "其他失败")
COLUMNS = ("序号", "原链接", "状态", "标题", "公众号", "发布时间", "正文字数", "图片数", "md文件名", "失败原因")
TRACKING_KEYS = {
    "scene", "subscene", "from", "isappinstalled", "clicktime", "enterid", "sessionid",
    "ascene", "devicetype", "version", "nettype", "lang", "exportkey", "pass_ticket",
    "wx_header", "chksm", "mpshare", "srcid", "sharer_shareinfo", "sharer_shareinfo_first",
}


def now() -> datetime:
    return datetime.now(SHANGHAI)


class ArticleError(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


@dataclass
class Link:
    line: int
    original: str
    url: str = ""
    error: str = ""


def normalize_url(raw: str) -> str:
    value = html.unescape(raw.strip()).strip(" \t\r\n\"'<>《》〈〉“”‘’（），。；！、【】[]()")
    # Common copied punctuation can occur after a trailing #rd or before it.
    value = value.split("#", 1)[0].rstrip(" \t\r\n，。；！、）】》.,;!)]>")
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("mp.weixin.qq.com/"):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("链接格式无效") from exc
    if parsed.scheme not in ("http", "https") or parsed.hostname != "mp.weixin.qq.com":
        raise ValueError("不是 mp.weixin.qq.com 公众号链接")
    if parsed.username or parsed.password or port not in (None, 80, 443):
        raise ValueError("公众号链接不应包含登录信息或自定义端口")
    path = parsed.path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]
    if path == "/s":
        params = dict(query)
        if not params.get("__biz") or not params.get("mid"):
            raise ValueError("公众号长链接缺少 __biz 或 mid 参数")
    elif not re.fullmatch(r"/s/[A-Za-z0-9_-]+", path):
        raise ValueError("不是受支持的公众号文章路径（应为 /s/短码 或 /s?参数）")
    return urlunsplit(("https", "mp.weixin.qq.com", path, urlencode(sorted(query)), ""))


def link_key(url: str) -> str:
    parsed = urlsplit(url)
    params = dict(parse_qsl(parsed.query))
    if parsed.path == "/s" and params.get("__biz") and params.get("mid"):
        return json.dumps([params["__biz"], params["mid"], params.get("idx") or "1"], ensure_ascii=False)
    return url


def read_links(path: Path) -> tuple[list[Link], list[dict]]:
    links, duplicates, seen = [], [], {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        item = Link(line_no, raw)
        try:
            item.url = normalize_url(raw)
            key = link_key(item.url)
        except ValueError as exc:
            item.error = str(exc)
            key = "invalid:" + raw
        if key in seen:
            duplicates.append({"行号": line_no, "首次行号": seen[key], "原链接": raw})
            continue
        seen[key] = line_no
        links.append(item)
    return links, duplicates


def js_value(source: str, names: tuple[str, ...]) -> str:
    """Read literal metadata only; never evaluate page JavaScript."""
    for name in names:
        match = re.search(
            r"\b" + re.escape(name) + r"\s*(?:=|:)\s*(?:\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'|(\d{9,13}))",
            source,
        )
        if match:
            value = next(v for v in match.groups() if v is not None)
            value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), value)
            value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), value)
            value = re.sub(r"\\([\\/'\"])", r"\1", value)
            return html.unescape(value).strip()
    return ""


def first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if value:
                return html.unescape(str(value)).strip()
    return ""


def publication_time(source: str, soup: BeautifulSoup) -> str:
    stamp = js_value(source, ("ct", "create_time", "createTime", "oriCreateTime"))
    if re.fullmatch(r"\d{9,13}", stamp):
        try:
            seconds = int(stamp) / (1000 if len(stamp) == 13 else 1)
            return datetime.fromtimestamp(seconds, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            pass
    value = first_text(soup, ("#publish_time", 'meta[property="article:published_time"]'))
    value = value or js_value(source, ("publish_time", "publishTime"))
    match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})(?:日)?(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", value)
    if not match:
        return ""
    # ISO metadata may carry a timezone; convert it before formatting.
    try:
        iso = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if iso.tzinfo:
            return iso.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    year, month, day, hour, minute, second = match.groups()
    try:
        date = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return ""
    return date.strftime("%Y-%m-%d %H:%M:%S" if hour else "%Y-%m-%d")


@dataclass
class Article:
    title: str
    account: str
    author: str
    published: str
    body: str
    word_count: int
    images: list[str] = field(default_factory=list)
    removed_guidance: list[str] = field(default_factory=list)


def remove_follow_guidance(content) -> list[str]:
    """Remove narrow, standalone subscription prompts; retain topic paragraphs."""
    removed = []
    patterns = (
        r"^(?:哈[喽啰]|大家好)[，,！!\s]*我是.{1,80}?喜欢我的文章[，,\s]*请.{0,10}?关注[哦呀吧啊！!。.\s]*$",
        r"^(?:点击|点开|点一下)上方.{0,30}关注.{0,30}[！!。\s]*$",
        r"^(?:长按|扫描|扫码).{0,20}二维码.{0,20}关注.{0,30}[！!。\s]*$",
    )
    for node in list(content.select("p, section, div, span")):
        if node.parent is None or node.find("img"):
            continue
        text = node.get_text("", strip=True)
        if len(text) <= 120 and any(re.fullmatch(pattern, text) for pattern in patterns):
            removed.append(text)
            node.decompose()
    return removed


class WechatMarkdown(MarkdownConverter):
    def convert_a(self, el, text, parent_tags):
        href = el.get("href", "")
        if href:
            el["href"] = "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
            if el.find("img"):
                # Image markers contain paragraph breaks, which cannot live
                # inside a Markdown link label. Keep the click target nearby.
                return text + f"\n\n[图片超链接]({el['href']})\n\n"
        return super().convert_a(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        number = el.get("data-wx2md-number")
        if not number:
            return ""
        alt = re.sub(r"[\r\n]+", " ", el.get("alt", "")).replace("[", r"\[").replace("]", r"\]")
        source = el["src"].replace("<", "%3C").replace(">", "%3E")
        # Angle brackets keep parentheses and spaces in image URLs unambiguous.
        return f"\n\n【图{number}】\n\n![{alt}](<{source}>)\n\n【图{number}】\n\n"


def parse_article(source: str, base_url: str) -> Article:
    soup = BeautifulSoup(source, "html.parser")
    content = soup.select_one("#js_content")
    if content is None or not content.get_text("", strip=True):
        # Known standalone descriptions are eligible even for nonstandard
        # messages. Never fall back to the whole page, which contains UI text.
        for selector in ("#js_image_desc", "#js_video_desc", ".share_notice .js_share_text"):
            description = soup.select_one(selector)
            if description and description.get_text("", strip=True):
                content = description
                break
    visible = soup.get_text(" ", strip=True)
    has_body = content is not None and bool(content.get_text("", strip=True))
    # Only inspect page text outside a valid article, never script payloads or an
    # article discussing verification/deletion. This avoids false positives.
    if not has_body:
        if any(token in visible for token in ("环境异常", "去验证", "完成验证", "访问过于频繁", "安全验证", "请在微信客户端打开链接")):
            raise ArticleError("需要验证", "页面要求验证或在微信客户端访问；未重试、未尝试绕过")
        if any(token in visible for token in ("该内容已被发布者删除", "内容已被发布者删除", "此内容因违规无法查看", "此内容因投诉无法查看", "该内容暂时无法查看", "此内容已被删除", "该内容已被删除", "内容已删除", "帐号已被屏蔽", "此帐号已被屏蔽", "内容违规", "接相关投诉")):
            raise ArticleError("文章已删除或违规", "页面提示文章已删除、违规或因投诉无法查看")
    if content is None:
        if any(token in visible for token in ("链接已过期", "参数错误", "页面不存在", "链接无效", "链接已失效", "系统出错")):
            raise ArticleError("其他失败", "微信返回无效链接、过期链接或页面错误提示")
        if soup.select_one("#js_video_page, #js_image_desc, .weui-msg") or any(token in visible for token in ("视频号", "图片消息", "分享一篇文章")):
            raise ArticleError("不是图文文章", "页面没有标准图文正文，未找到可归档文字")
        raise ArticleError("其他失败", "未找到公众号正文 #js_content；可能是无效链接或页面结构变化")

    title = first_text(soup, ("#activity-name", ".rich_media_title", 'meta[property="og:title"]')) or js_value(source, ("msg_title",))
    account = first_text(soup, ("#js_name", ".profile_nickname", 'meta[property="og:site_name"]')) or js_value(source, ("nickname",))
    author = first_text(soup, ("#js_author_name", 'meta[name="author"]')) or js_value(source, ("author", "msg_author"))
    if not author:
        for node in soup.select("#meta_content .rich_media_meta_text"):
            value = node.get_text(" ", strip=True)
            if value and value not in ("原创", "Original", "已修改", account) and node.get("id") != "publish_time":
                author = value
                break
    published = publication_time(source, soup)

    # Remove UI containers by structural identity, not keywords in the article.
    for node in list(content.select("script, style, noscript, template, iframe, mp-common-profile, mp-style-type, .mp_profile_iframe_wrp, .qr_code_pc, .rich_media_tool, .rich_media_extra, .js_profile_qrcode, #js_pc_qr_code, #js_profile_qrcode, #js_sponsor_ad_area, #js_toobar3, #js_bottom_ad_area")):
        if node.parent is None:
            continue
        node.decompose()
    for node in list(content.select("[hidden], [style]")):
        if node.parent is None:
            continue
        style = node.get("style", "")
        # #js_content itself is often visibility:hidden until WeChat JS runs;
        # discard only genuinely hidden descendants, never the root body.
        if node.has_attr("hidden") or re.search(r"display\s*:\s*none\b", style, re.I):
            node.decompose()
    removed_guidance = remove_follow_guidance(content)
    # WeChat editors commonly express emphasis as styled spans rather than
    # <strong>. Convert that explicit styling before dropping CSS.
    for node in content.select("span[style]"):
        if re.search(r"font-weight\s*:\s*(?:bold|bolder|[6-9]00)\b", node["style"], re.I):
            if node.find_parent(["strong", "b"]) is None:
                node.name = "strong"
    for node in content.select("a[href]"):
        href = html.unescape(node["href"]).strip()
        if href.lower().startswith(("javascript:", "data:")):
            node.unwrap()
        else:
            node["href"] = urljoin(base_url, href)
    images = []
    for node in content.select("img"):
        src = html.unescape(node.get("data-src") or node.get("data-original") or node.get("src") or "").strip()
        if not src:
            node.decompose()
            continue
        src = urljoin(base_url, src)
        if urlsplit(src).scheme not in ("http", "https", "data"):
            node.decompose()
            continue
        images.append(src)
        node["src"] = src
        node["data-wx2md-number"] = str(len(images))
    # Word count = non-whitespace visible body characters, excluding image
    # markers, Markdown syntax, metadata, and image alt labels.
    word_count = len(re.sub(r"\s", "", content.get_text("", strip=True)))
    if word_count == 0:
        raise ArticleError("不是图文文章", "正文没有可提取文字（可能是纯图片、视频或转载卡片消息）")
    converter = WechatMarkdown(heading_style="ATX", bullets="-", escape_underscores=False)
    body = converter.convert_soup(content).strip()
    body = re.sub(r"\n[ \t]+\n", "\n\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return Article(title, account, author, published, body, word_count, images, removed_guidance)


class WebClient:
    """One request per URL, finite redirects, no automatic retry."""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _get(self, url: str, limit: int, article: bool = False, referer: str = "") -> tuple[bytes, str]:
        for _ in range(6):
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("不支持的下载地址")
            if article and parsed.hostname != "mp.weixin.qq.com":
                raise ArticleError("其他失败", "文章跳转到非公众号域名，已停止请求")
            with self.session.get(url, timeout=(10, 30), allow_redirects=False, stream=True, headers={"Referer": referer} if referer else {}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if "Location" not in response.headers:
                        raise ValueError("跳转响应缺少目标地址")
                    url = urljoin(url, response.headers["Location"])
                    if article and any(part in urlsplit(url).path for part in ("/mp/verify", "/mp/wappoc_appmsgcaptcha", "/mp/verifycode")):
                        raise ArticleError("需要验证", "公众号跳转到验证页面；未继续请求、未重试")
                    continue
                if article and response.status_code == 429:
                    raise ArticleError("需要验证", "公众号返回 HTTP 429（访问过于频繁）；未重试")
                # 403 bodies can contain the actual verification explanation.
                if response.status_code != 403 or not article:
                    response.raise_for_status()
                chunks, length = [], 0
                for chunk in response.iter_content(64 * 1024):
                    length += len(chunk)
                    if length > limit:
                        raise ValueError(f"下载内容超过 {limit // 1024 // 1024} MB 上限")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("Content-Type", "")
        raise ValueError("跳转次数过多")

    def article(self, url: str) -> str:
        data, _ = self._get(url, 20 * 1024 * 1024, article=True)
        return data.decode("utf-8-sig", errors="replace")

    def image(self, url: str, referer: str) -> tuple[bytes, str]:
        if url.startswith("data:"):
            header, encoded = url.split(",", 1)
            if ";base64" not in header or len(encoded) > 40 * 1024 * 1024:
                raise ValueError("不支持或过大的内嵌图片")
            data = base64.b64decode(encoded, validate=True)
        else:
            data, _ = self._get(url, 30 * 1024 * 1024, referer=referer)
        # Validate magic bytes so verification/error HTML is never saved as an image.
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            ext = ".webp"
        elif len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
            ext = ".avif"
        elif data.startswith(b"BM"):
            ext = ".bmp"
        else:
            raise ValueError("响应不是受支持的图片（可能返回验证页或错误页）")
        return data, ext


def safe_name(value: str, maximum: int = 40) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:maximum].rstrip(" .") or "未命名"


def article_path(folder: Path, article: Article) -> Path:
    stem = "_".join((article.published[:10] or "日期未知", safe_name(article.account or "公众号未知", 30), safe_name(article.title[:20])))
    candidate, number = folder / (stem + ".md"), 2
    while candidate.exists() or candidate.with_name(candidate.stem + "_images").exists():
        candidate = folder / f"{stem}_{number}.md"
        number += 1
    return candidate


def save_article(article: Article, original: str, url: str, folder: Path, client: WebClient, sleep: Callable = time.sleep) -> tuple[str, list[str], dict]:
    path = article_path(folder, article)
    image_dir = path.with_name(path.stem + "_images")
    image_dir.mkdir()
    errors, backups, cache = [], [], {}
    for number, image_url in enumerate(article.images, 1):
        try:
            if image_url not in cache:
                if cache:
                    sleep(random.uniform(0.3, 0.8))
                # Cache failures too: duplicate failed images must not be retried.
                cache[image_url] = None
                cache[image_url] = client.image(image_url, url)
            if cache[image_url] is None:
                raise ValueError("相同图片此前下载失败，未重复请求")
            data, ext = cache[image_url]
            image_path = image_dir / f"{number:03d}{ext}"
            image_path.write_bytes(data)
            backups.append({"图号": number, "原图链接": image_url, "本地文件": str(image_path.relative_to(folder))})
        except (requests.RequestException, ValueError, OSError) as exc:
            reason = f"图{number}备份失败：{concise_error(exc)}"
            errors.append(reason)
            backups.append({"图号": number, "原图链接": image_url, "失败原因": reason})
    metadata = {
        "标题": article.title, "公众号": article.account, "作者": article.author,
        "发布时间": article.published, "原链接": original,
        "抓取时间": now().isoformat(timespec="seconds"), "正文字数": article.word_count,
        "图片数": len(article.images),
    }
    path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + article.body + "\n", encoding="utf-8")
    missing = [key for key in ("标题", "公众号", "发布时间") if not metadata[key]]
    if missing:
        errors.insert(0, "未提取到必要信息：" + "、".join(missing))
    return path.name, errors, {"md文件名": path.name, "图片备份": backups, "已清理关注引导": article.removed_guidance}


def concise_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "网络请求超时；未重试"
    if isinstance(exc, requests.HTTPError):
        return f"HTTP {exc.response.status_code}；未重试"
    if isinstance(exc, requests.RequestException):
        return "网络连接失败（请检查网络或 VPN）；未重试"
    return str(exc).replace("\n", " ")[:400]


def project_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("输入和输出路径都必须在本项目目录内") from exc
    return path


def create_batch(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%d_%H%M%S")
    folder, number = parent / stamp, 2
    while folder.exists():
        folder = parent / f"{stamp}_{number}"
        number += 1
    folder.mkdir()
    return folder


def write_csv(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def run_batch(input_path: Path, output_parent: Path, client: Optional[WebClient] = None, sleep: Callable = time.sleep, log: Callable = print) -> tuple[Path, dict]:
    # Enforce the workspace boundary even for programmatic callers and symlinks.
    input_path = project_path(str(input_path))
    output_parent = project_path(str(output_parent))
    links, duplicates = read_links(input_path)
    if not links:
        raise ValueError("输入文件没有文章链接；请一行放一个链接")
    folder = create_batch(output_parent)
    # Snapshot the exact input so a later input edit never changes batch evidence.
    (folder / "_输入链接.txt").write_text(input_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    client = client or WebClient()
    rows, details = [], []
    attempted = verification_count = 0
    stopped = interrupted = False
    started = now().isoformat(timespec="seconds")
    log(f"共 {len(links)} 个唯一链接，跳过 {len(duplicates)} 条重复。输出：{folder}")
    for index, item in enumerate(links, 1):
        row = dict.fromkeys(COLUMNS, "")
        row.update({"序号": index, "原链接": item.original, "正文字数": 0, "图片数": 0})
        rows.append(row)
        if stopped or interrupted:
            row.update({"状态": "其他失败", "失败原因": "本批验证页占比过高，已停止；此链接未请求" if stopped else "用户中断批次；此链接未请求"})
            continue
        try:
            if item.error:
                raise ArticleError("其他失败", item.error + "；未发起网络请求")
            if attempted:
                delay = random.uniform(3, 6)
                log(f"等待 {delay:.1f} 秒…")
                sleep(delay)
            attempted += 1
            log(f"[{index}/{len(links)}] 正在抓取第 {item.line} 行链接…")
            source = client.article(item.url)
            snapshot_dir = folder / "_原始网页"
            snapshot_dir.mkdir(exist_ok=True)
            (snapshot_dir / f"{index:03d}.html").write_text(source, encoding="utf-8")
            article = parse_article(source, item.url)
            row.update({"标题": article.title, "公众号": article.account, "发布时间": article.published, "正文字数": article.word_count, "图片数": len(article.images)})
            filename, errors, detail = save_article(article, item.original, item.url, folder, client, sleep)
            row.update({"md文件名": filename, "状态": "其他失败" if errors else "成功", "失败原因": "；".join(errors)})
            details.append({"序号": index, "输入行号": item.line, **detail})
        except ArticleError as exc:
            row.update({"状态": exc.status, "失败原因": str(exc)})
            if exc.status == "需要验证":
                verification_count += 1
        except (requests.RequestException, ValueError, OSError) as exc:
            row.update({"状态": "其他失败", "失败原因": concise_error(exc)})
        except KeyboardInterrupt:
            interrupted = True
            row.update({"状态": "其他失败", "失败原因": "用户中断批次；当前文章未完整归档"})
        write_csv(folder / "_总表.csv", rows)
        log(f"  {row['状态']}：{row['标题'] or row['失败原因']}")
        # Stop as soon as 3+ requests yield >=50% verification pages. Remaining
        # inputs remain visible in the CSV as unrequested, never mislabelled.
        if attempted >= 3 and verification_count / attempted >= 0.5:
            stopped = True
            log(f"验证页 {verification_count}/{attempted}（{verification_count / attempted:.0%}），停止后续请求。")
    write_csv(folder / "_总表.csv", rows)
    counts = {state: sum(row["状态"] == state for row in rows) for state in STATES}
    report = {
        "开始时间": started, "结束时间": now().isoformat(timespec="seconds"), "输入文件": str(input_path),
        "唯一链接数": len(links), "跳过重复数": len(duplicates), "重复链接": duplicates,
        "文章请求数": attempted, "验证页数": verification_count,
        "验证页占请求比例": round(verification_count / attempted, 4) if attempted else 0,
        "因验证过多提前停止": stopped, "用户中断": interrupted,
        "状态统计": counts, "文章详情": details,
    }
    (folder / "_运行报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"\n完成：成功 {counts['成功']} 条，失败 {len(rows) - counts['成功']} 条；跳过重复 {len(duplicates)} 条。")
    for state in STATES[1:]:
        log(f"  {state}：{counts[state]} 条")
    log(f"输出目录：{folder}")
    return folder, report
