#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 - "$PROJECT_DIR" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1]) / "skills/wechat-article-reader"
skills_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "skills"
target = skills_dir / source.name
skills_dir.mkdir(parents=True, exist_ok=True)
if target.exists() or target.is_symlink():
    if target.is_symlink() and target.resolve() == source.resolve():
        print(f"已安装：{target}")
        sys.exit(0)
    sys.exit(f"安装位置已存在其他文件，未覆盖：{target}")
target.symlink_to(source, target_is_directory=True)
print(f"已安装：{target}")
print(f"绑定项目：{source.parent.parent}")
PY
