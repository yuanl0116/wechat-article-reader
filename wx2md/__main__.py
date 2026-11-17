import argparse
import sys

from .core import ROOT, project_path, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="wx2md：批量保存公众号文章为 Markdown 和总表，不重试验证页面。")
    parser.add_argument("--input", default=str(ROOT / "input/links.txt"), help="项目内的链接文件，默认 input/links.txt")
    parser.add_argument("--output", default=str(ROOT / "output"), help="项目内的输出父目录，默认 output/")
    args = parser.parse_args()
    try:
        _, report = run_batch(project_path(args.input), project_path(args.output))
    except FileNotFoundError:
        print("未找到链接文件。请把链接发给助手保存到 input/links.txt，或自行创建该文件，一行一个。", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"无法开始或完成批次：{exc}", file=sys.stderr)
        return 1
    if report["用户中断"]:
        return 130
    return 0 if report["状态统计"]["成功"] == report["唯一链接数"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
