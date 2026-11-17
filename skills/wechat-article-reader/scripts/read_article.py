#!/usr/bin/env python3
"""Small, dependency-free bridge from a wx2md batch to an agent's reading tools."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[3]


def inside_project(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(PROJECT)
    except ValueError as exc:
        raise ValueError("路径必须在 wx2md 项目内") from exc
    return path


def prepare(urls: list[str]) -> dict:
    if not urls or any(not url.strip() or "\n" in url or "\r" in url for url in urls):
        raise ValueError("每个参数必须是一个非空、单行的文章链接")
    # The downloader remains responsible for URL validation and deduplication.
    path = inside_project(str(PROJECT / "input" / f"skill_{uuid.uuid4().hex}.txt"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(url.strip() for url in urls) + "\n")
    return {
        "project_dir": str(PROJECT),
        "input_file": str(path),
        "command": ["sh", str(PROJECT / "run.sh"), "--input", str(path), "--output", str(PROJECT / "output")],
    }


def batch_index(value: str) -> dict:
    folder = inside_project(value)
    with (folder / "_总表.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    report_path = folder / "_运行报告.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    details = {int(item["序号"]): item for item in report.get("文章详情", [])}
    articles = []
    for row in rows:
        number = int(row["序号"])
        md = None
        if row["md文件名"]:
            md = inside_project(str(folder / row["md文件名"]))
            if md.parent != folder or md.suffix != ".md":
                raise ValueError("总表中的 Markdown 路径不属于本批次")
        readable = md is not None and md.is_file() and int(row["正文字数"]) > 0
        images = []
        for image in details.get(number, {}).get("图片备份", []):
            item = dict(image)
            if item.get("本地文件"):
                local = inside_project(str(folder / item["本地文件"]))
                local.relative_to(folder)
                item["本地文件"] = str(local)
                item["exists"] = local.is_file()
            images.append(item)
        articles.append({
            "number": number, "url": row["原链接"], "title": row["标题"],
            "account": row["公众号"], "published": row["发布时间"],
            "archive_status": row["状态"], "archive_error": row["失败原因"],
            "readable": readable, "markdown": str(md) if md else None,
            "characters": len(md.read_text(encoding="utf-8")) if readable else 0,
            "image_count": int(row["图片数"]), "images": images,
        })
    return {"batch_dir": str(folder), "articles": articles}


def read_chunk(value: str, offset: int = 0, limit: int = 3000) -> dict:
    path = inside_project(value)
    if path.suffix != ".md":
        raise ValueError("只读取 Markdown 文章文件")
    if not 1 <= limit <= 8000:
        raise ValueError("每块字符数必须在 1–8000 之间")
    content = path.read_text(encoding="utf-8")
    if not 0 <= offset <= len(content):
        raise ValueError("起始位置超出文章范围")
    end = min(offset + limit, len(content))
    return {
        "markdown": str(path), "offset": offset, "next_offset": end,
        "total_characters": len(content), "done": end == len(content),
        "content": content[offset:end],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="准备公众号下载任务、列出归档文章、分块阅读正文。")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("project", help="显示绑定的 wx2md 项目路径")
    create = commands.add_parser("prepare", help="为本次链接创建独立输入文件")
    create.add_argument("urls", nargs="+")
    index = commands.add_parser("index", help="列出本批文章的归档状态及可读文件")
    index.add_argument("batch")
    read = commands.add_parser("read", help="按字符位置读取一块 Markdown")
    read.add_argument("markdown")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=3000)
    args = parser.parse_args()
    try:
        if not (PROJECT / "wx2md/core.py").is_file():
            raise ValueError("未找到 wx2md 项目；请通过项目内的 install-skill.sh 安装，保留项目目录")
        if args.action == "project":
            result = {"project_dir": str(PROJECT)}
        elif args.action == "prepare":
            result = prepare(args.urls)
        elif args.action == "index":
            result = batch_index(args.batch)
        else:
            result = read_chunk(args.markdown, args.offset, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print(f"无法准备或读取文章：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
