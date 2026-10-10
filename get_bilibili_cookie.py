#!/usr/bin/env python3
"""Export your own Bilibili cookies locally; makes no network requests.

Install: python3 -m pip install -U browser-cookie3
Run: python3 get_bilibili_cookie.py --browser chrome
Use --cookie-file to select a particular browser profile's cookie database.
"""
import argparse
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="导出本机浏览器的 Bilibili Cookie")
    parser.add_argument("--browser", choices=["chrome", "edge", "firefox", "safari", "brave", "chromium"], default="chrome")
    parser.add_argument("--cookie-file", help="可选：指定已登录浏览器配置的 Cookie 数据库路径")
    parser.add_argument("--output", default="bilibili_cookie.txt", help="输出 Cookie 请求头字符串（默认不覆盖已有文件）")
    args = parser.parse_args()
    try:
        import browser_cookie3
    except ImportError:
        parser.exit(1, "请先运行：python3 -m pip install -U browser-cookie3\n")
    try:
        kwargs = {"domain_name": "bilibili.com"}
        if args.cookie_file:
            kwargs["cookie_file"] = str(Path(args.cookie_file).expanduser())
        jar = getattr(browser_cookie3, args.browser)(**kwargs)
    except Exception as exc:
        print(f"读取失败（{type(exc).__name__}）。请确认浏览器选择和配置路径；可退出浏览器后重试。", file=sys.stderr)
        print("如果系统询问钥匙串访问，请确认是你运行的 Python。浏览器加密机制可能导致此库无法读取。", file=sys.stderr)
        return 1

    # Scope to the main Bilibili website; don't combine subdomain/path cookies.
    selected = {}
    now = time.time()
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if domain not in ("bilibili.com", "www.bilibili.com"):
            continue
        if domain == "bilibili.com" and not cookie.domain_specified:
            continue
        if cookie.path != "/" or (cookie.expires is not None and cookie.expires <= now):
            continue
        priority = int(domain == "www.bilibili.com")
        if cookie.name not in selected or priority > selected[cookie.name][0]:
            selected[cookie.name] = (priority, cookie.value)
    if not selected.get("SESSDATA", (0, ""))[1]:
        print("未找到 SESSDATA。请在所选浏览器的对应配置中登录 B 站后重试。", file=sys.stderr)
        return 1
    header = "; ".join(f"{name}={value}" for name, (_, value) in sorted(selected.items()))
    output = Path(args.output).expanduser().absolute()
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(header + "\n")
    except FileExistsError:
        print("输出文件已存在，请用 --output 指定新文件名。", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"保存失败（{type(exc).__name__}）。请检查目标目录和权限。", file=sys.stderr)
        return 1
    print(f"已保存 {len(selected)} 个 Cookie：{output}")
    print("已检测到 SESSDATA；未联网验证登录是否仍有效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
