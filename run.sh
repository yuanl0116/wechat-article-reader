#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"
export PYTHONPYCACHEPREFIX="$PROJECT_DIR/.cache/pycache"
if [ ! -x .venv/bin/python ]; then
    echo '正在创建项目独立 Python 环境…'
    PYTHON_BIN=''
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import ssl, sys; assert sys.version_info >= (3, 9); assert ssl.OPENSSL_VERSION.startswith("OpenSSL ")' 2>/dev/null; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
    if [ -z "$PYTHON_BIN" ]; then
        echo '需要 Python 3.9+（使用 OpenSSL）；建议安装 Python 3.12。' >&2
        exit 1
    fi
    "$PYTHON_BIN" -m venv .venv
fi
if ! .venv/bin/python -c 'import bs4, markdownify, requests, yaml' 2>/dev/null; then
    .venv/bin/python -m pip --cache-dir "$PROJECT_DIR/.cache/pip" install -r requirements.txt
fi
exec .venv/bin/python -m wx2md "$@"
