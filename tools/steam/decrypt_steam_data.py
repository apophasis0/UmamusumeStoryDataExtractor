#!/usr/bin/env python3
r"""Steam / 全球版《赛马娘》数据解密工具。

Steam（JP / Global）版的游戏数据与 DMM / Android 版不同：

* ``Persistent/meta``（资源索引 SQLite）使用 SQLite3 Multiple Ciphers 的 ChaCha20 加密；
* ``Persistent/dat/<xx>/<hash>`` 下的 UnityFS 资源包从偏移 256 开始，
  用「meta 表 a 的 e 列（每资源一个 u64）+ 11 字节 ABKey」生成的 88 字节密钥流
  按文件绝对偏移循环 XOR 加密。

本脚本可以把 Steam 版数据转换成 UmamusumeStoryDataExtractor 能直接读取的目录::

    <out>/meta        明文 SQLite
    <out>/dat/<xx>/.. 解密后的资源包（默认只处理 story/home/race timeline）

用法（推荐用 uv 自动准备依赖）::

    # 一步生成提取器输入目录
    uv run --with apsw-sqlite3mc --with numpy python decrypt_steam_data.py prepare \
        --persistent "<游戏>\...\Persistent" --out "F:\UmamusumeSteamData"

    # 或分步执行
    uv run --with apsw-sqlite3mc python decrypt_steam_data.py meta \
        --meta "<游戏>\...\Persistent\meta" --out "<out>\meta"
    uv run --with numpy python decrypt_steam_data.py bundles \
        --meta "<out>\meta" --dat "<游戏>\...\Persistent\dat" --out "<out>\dat"

密钥与算法均为公开信息，参考：

* katboi01/UmaViewer（meta / bundle 密钥）
* TheCing/Trackside ``meta_tool.py``（meta 解密方式）
* Vali-98/umamusu-utils（bundle 逐资源密钥生成方式）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Windows 控制台默认编码可能无法输出中文（如 cp932），统一改用 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 常量（公开密钥）
# ---------------------------------------------------------------------------

# meta：服务器 DB 密钥与 DB_BASE_KEY 前 13 字节逐字节 XOR 后作为 ChaCha20 密钥
DB_BASE_KEY = bytes.fromhex("F170CEA4DFCEA3E1A5D8C70BD1000000")
GLOBAL_DB_KEY = bytes.fromhex(
    "36236b4c2a3921755226327625503f355d77586d4071385e4c3128742959372453"
)
JP_DB_KEY = bytes.fromhex(
    "6d5b65336336632554712d73505363386d34377b356370233734532973433633"
)

# bundle：11 字节基础密钥，与 meta 的 e 列组合成 88 字节密钥流
AB_KEY = bytes.fromhex("532B4631E4A7B9473E7CFB")
BUNDLE_HEADER_SIZE = 256

# 默认只解密提取器需要的三类 timeline（与 Program.fs 中的正则一致）
DEFAULT_PATTERNS = [
    r"story/data/\d+/\d+/storytimeline_\d+",
    r"home/data/(\d+)/(\d+)/hometimeline_\1_\2_\d+",
    r"race/storyrace/text/storyrace_\d+",
]


def meta_key(server: str) -> bytes:
    key = JP_DB_KEY if server == "jp" else GLOBAL_DB_KEY
    return bytes(b ^ DB_BASE_KEY[i % 13] for i, b in enumerate(key))


# ---------------------------------------------------------------------------
# meta 解密
# ---------------------------------------------------------------------------


def decrypt_meta(meta_path: Path, out_path: Path, server: str) -> None:
    """把加密的 meta 复制并解密为普通 SQLite 文件（不修改原文件）。"""
    try:
        import apsw  # type: ignore
    except ImportError:
        sys.exit(
            "缺少 apsw-sqlite3mc，请用：\n"
            "  uv run --with apsw-sqlite3mc python decrypt_steam_data.py ..."
        )

    meta_path = Path(meta_path)
    out_path = Path(out_path)
    if not meta_path.exists():
        sys.exit(f"找不到 meta 文件：{meta_path}")
    if meta_path.resolve() == out_path.resolve():
        sys.exit("输出路径不能与原始 meta 相同（脚本不会修改游戏文件）")

    fd, tmp_name = tempfile.mkstemp(prefix="meta_", suffix=".db")
    os.close(fd)
    shutil.copy2(meta_path, tmp_name)
    conn = apsw.Connection(tmp_name)
    try:
        conn.pragma("cipher", "chacha20")
        conn.pragma("hexkey", meta_key(server).hex())
        status = next(conn.cursor().execute("PRAGMA quick_check"))[0]
        if status != "ok":
            raise SystemExit(f"解密校验失败：{status}（--server 选对了吗？）")
        conn.pragma("rekey", "")
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(tmp_name, out_path)
    count = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True).execute(
        "SELECT count(*) FROM a"
    ).fetchone()[0]
    print(f"meta 解密完成 -> {out_path}（资源索引 {count:,} 行）")


# ---------------------------------------------------------------------------
# bundle 解密
# ---------------------------------------------------------------------------


def bundle_keystream(e: int, length: int) -> bytes:
    """生成 [256, length) 区间对应的 XOR 密钥流（按文件绝对偏移循环）。"""
    key = struct.pack("<Q", e & 0xFFFFFFFFFFFFFFFF)
    ks88 = bytes(b ^ key[j] for i, b in enumerate(AB_KEY) for j in range(8))
    return (ks88 * (length // len(ks88) + 2))[BUNDLE_HEADER_SIZE:length]


def xor_bytes(data: bytes, keystream: bytes) -> bytes:
    try:
        import numpy as np  # type: ignore

        out = np.frombuffer(data, dtype=np.uint8).copy()
        out[BUNDLE_HEADER_SIZE:] ^= np.frombuffer(keystream, dtype=np.uint8)
        return out.tobytes()
    except ImportError:
        body = bytes(a ^ b for a, b in zip(data[BUNDLE_HEADER_SIZE:], keystream))
        return data[:BUNDLE_HEADER_SIZE] + body


def select_rows(meta_path: Path, patterns: list[str], limit: int | None):
    regexes = [re.compile(p) for p in patterns]
    con = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
    rows = []
    for name, h, e in con.execute("SELECT n, h, e FROM a"):
        if any(r.search(name) for r in regexes):
            rows.append((name, h, e))
            if limit and len(rows) >= limit:
                break
    return rows


def decrypt_bundles(
    meta_path: Path,
    dat_dir: Path,
    out_dir: Path,
    patterns: list[str],
    jobs: int,
    limit: int | None,
) -> None:
    meta_path, dat_dir, out_dir = Path(meta_path), Path(dat_dir), Path(out_dir)
    if not meta_path.exists():
        sys.exit(f"找不到 meta 文件：{meta_path}")
    if not dat_dir.exists():
        sys.exit(f"找不到 dat 目录：{dat_dir}")

    rows = select_rows(meta_path, patterns, limit)
    print(f"匹配到 {len(rows):,} 个资源包，开始解密 -> {out_dir}")

    stats = {"ok": 0, "skip": 0, "missing": 0, "error": 0}

    def process(row) -> str:
        name, h, e = row
        if e is None:
            return "error"
        src = dat_dir / h[:2] / h
        dst = out_dir / h[:2] / h
        if dst.exists():
            return "skip"
        if not src.exists():
            return "missing"
        try:
            data = src.read_bytes()
            if len(data) <= BUNDLE_HEADER_SIZE:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                return "ok"
            out = xor_bytes(data, bundle_keystream(e, len(data)))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(out)
            return "ok"
        except Exception as exc:  # noqa: BLE001
            print(f"\n解密失败 {name} ({h}): {exc}")
            return "error"

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for i, result in enumerate(pool.map(process, rows), 1):
            stats[result] += 1
            if i % 2000 == 0 or i == len(rows):
                print(f"  进度 {i:,}/{len(rows):,}  {stats}")

    print(
        f"完成：成功 {stats['ok']:,}，跳过（已存在）{stats['skip']:,}，"
        f"缺失 {stats['missing']:,}，失败 {stats['error']:,}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", choices=["jp", "global"], default="jp",
                        help="选择密钥（默认 jp）")


def cmd_meta(args: argparse.Namespace) -> None:
    decrypt_meta(args.meta, args.out, args.server)


def cmd_bundles(args: argparse.Namespace) -> None:
    patterns = args.pattern or DEFAULT_PATTERNS
    decrypt_bundles(args.meta, args.dat, args.out, patterns, args.jobs, args.limit)


def cmd_prepare(args: argparse.Namespace) -> None:
    persistent = Path(args.persistent)
    out = Path(args.out)
    decrypt_meta(persistent / "meta", out / "meta", args.server)
    patterns = args.pattern or DEFAULT_PATTERNS
    decrypt_bundles(out / "meta", persistent / "dat", out / "dat",
                    patterns, args.jobs, args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Steam / 全球版《赛马娘》meta 与资源包解密工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("meta", help="把加密 meta 解密为普通 SQLite")
    p.add_argument("--meta", type=Path, required=True, help="加密的 meta 文件")
    p.add_argument("--out", type=Path, required=True, help="输出的明文 meta")
    add_common_args(p)
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("bundles", help="批量解密 dat 资源包")
    p.add_argument("--meta", type=Path, required=True, help="明文 meta（先用 meta 子命令生成）")
    p.add_argument("--dat", type=Path, required=True, help="游戏的 dat 目录")
    p.add_argument("--out", type=Path, required=True, help="输出的 dat 目录")
    p.add_argument("--pattern", action="append", default=None,
                   help="资源名正则（可多次指定，替换默认的三类 timeline）")
    p.add_argument("--jobs", type=int, default=8, help="并发数（默认 8）")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 个（测试用）")
    p.set_defaults(func=cmd_bundles)

    p = sub.add_parser("prepare", help="一步生成提取器输入目录（meta + dat）")
    p.add_argument("--persistent", type=Path, required=True, help="游戏的 Persistent 目录")
    p.add_argument("--out", type=Path, required=True, help="输出目录（可直接作为提取器输入）")
    p.add_argument("--pattern", action="append", default=None,
                   help="资源名正则（可多次指定，替换默认的三类 timeline）")
    p.add_argument("--jobs", type=int, default=8, help="并发数（默认 8）")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 个（测试用）")
    add_common_args(p)
    p.set_defaults(func=cmd_prepare)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
