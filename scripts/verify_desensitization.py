#!/usr/bin/env python3
"""发布前脱敏扫描（fail-closed）。

检查项：
1. 目录内存在 .git（历史提交可能含旧密钥）
2. 存在真实 .env / 数据库 / 运行状态文件
3. Bot Token 格式、私钥块
4. 公网 IPv4（排除私网、回环、文档示例段）
5. 宿主机绝对路径（/home/<user>、/Users/<user>、C:\\Users\\）
6. 私有标识清单（--private-file，0600 JSON 字符串数组，放在扫描目录之外）

用法：
    python scripts/verify_desensitization.py . --private-file ~/private_values.json
"""
import argparse
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
FORBIDDEN_FILES = {".env", "messages.db", "state.json", "llm_endpoints.json", "messages.db-wal", "messages.db-shm"}

PATTERNS = {
    "bot_token": re.compile(r"(?<![\w])\d{7,12}:[\w-]{30,50}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    "home_path": re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)[A-Za-z0-9_.-]+"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
}

# 文档与示例中允许出现的占位地址
IP_ALLOWLIST = {
    "127.0.0.1",
    "0.0.0.0",
    "1.2.3.4",
}


def load_private_values(filename: str, target: Path):
    if not filename:
        return ()
    path = Path(filename).expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("private-file 必须是 0600 的普通文件")
    if path.resolve().is_relative_to(target.resolve()):
        raise ValueError("private-file 必须放在扫描目录之外")
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or any(not isinstance(v, str) or len(v) < 3 for v in values):
        raise ValueError("private-file 必须是字符串数组，且每项长度 >= 3")
    return tuple(values)


def scan(target: Path, private_values) -> list:
    findings = []

    for root, dirs, files in os.walk(target, followlinks=False):
        if ".git" in dirs:
            findings.append({"path": str(Path(root) / ".git"), "type": "git_history"})
            dirs.remove(".git")
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for name in files:
            path = Path(root) / name
            rel = str(path.relative_to(target))
            if name in FORBIDDEN_FILES:
                findings.append({"path": rel, "type": "runtime_secret_or_data"})
            if path.is_symlink():
                findings.append({"path": rel, "type": "symlink"})
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue

            lowered = text.casefold()
            for value in private_values:
                if value.casefold() in lowered:
                    findings.append({"path": rel, "type": "private_identifier"})

            for kind, pattern in PATTERNS.items():
                for match in pattern.finditer(text):
                    value = match.group(0)
                    if kind == "ipv4":
                        if value in IP_ALLOWLIST:
                            continue
                        try:
                            address = ipaddress.ip_address(value)
                        except ValueError:
                            continue
                        if address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
                            continue
                    if kind == "home_path":
                        # 允许文档中的通用示例路径
                        if value.rstrip("/").endswith(("/home/user", "/Users/user", "\\Users\\user")):
                            continue
                    findings.append({
                        "path": rel,
                        "line": text.count("\n", 0, match.start()) + 1,
                        "type": kind,
                    })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--private-file", default=os.environ.get("DESENSITIZATION_PRIVATE_FILE", ""))
    args = parser.parse_args()

    try:
        if not args.target.is_dir():
            raise ValueError("目标必须是目录")
        private_values = load_private_values(args.private_file, args.target)
    except (OSError, ValueError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2

    findings = scan(args.target, private_values)
    print(json.dumps({"status": "FAILED" if findings else "SUCCESS", "findings": findings}, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
