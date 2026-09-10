#!/usr/bin/env python3
r"""从语料库数据库导出大模型训练格式。

支持三种模式：

* ``scene``（默认）：每个资源一条 JSONL 记录，含有序多轮对话
* ``chat``：按指定角色生成 ``messages`` 格式（该角色为 assistant）
* ``blocks``：block 级平铺数据（JSONL 或 Parquet，Parquet 需要 pyarrow）

用法::

    # 按场景导出多轮对话
    python tools/db/export_corpus.py --db F:\UmamusumeCorpus\umamusume.db ^
        --out F:\UmamusumeCorpus\exports\scenes.jsonl --mode scene

    # 导出 スペシャルウィーク 的角色扮演对话
    python tools/db/export_corpus.py --db F:\UmamusumeCorpus\umamusume.db ^
        --out F:\UmamusumeCorpus\exports\special_week.jsonl --mode chat --target-chara 1001

    # 导出 Parquet（block 级，需要 pyarrow）
    python tools/db/export_corpus.py --db F:\UmamusumeCorpus\umamusume.db ^
        --out F:\UmamusumeCorpus\exports\blocks.parquet --mode blocks --format parquet
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SYSTEM_TEMPLATE = "あなたは「{name}」として振る舞ってください。"


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_chara(con: sqlite3.Connection, target: str) -> tuple[int, str]:
    if target.isdigit():
        row = con.execute(
            "SELECT chara_id, name FROM characters WHERE chara_id=?", (int(target),)
        ).fetchone()
    else:
        row = con.execute(
            "SELECT chara_id, name FROM characters WHERE name=?", (target,)
        ).fetchone()
        if not row:
            row = con.execute(
                "SELECT chara_id, name FROM characters WHERE name_variants LIKE ?",
                (f"%{target}%",),
            ).fetchone()
    if not row:
        sys.exit(f"找不到角色：{target}")
    return row["chara_id"], row["name"] or target


def fetch_assets(con: sqlite3.Connection, kinds: list[str], limit: int | None):
    where = ""
    params: list = []
    if kinds:
        where = f"WHERE kind IN ({','.join('?' * len(kinds))})"
        params = kinds
    sql = (
        "SELECT id, path, kind, title, story_id, source_type, chara_ids "
        f"FROM assets {where} ORDER BY id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def load_choices(con: sqlite3.Connection) -> dict[int, list[str]]:
    choices: dict[int, list[str]] = {}
    for block_id, text in con.execute(
        "SELECT block_id, text FROM choices ORDER BY block_id, choice_index"
    ):
        choices.setdefault(block_id, []).append(text)
    return choices


def asset_turns(con: sqlite3.Connection, asset, choices: dict[int, list[str]]):
    if asset["kind"] == "race":
        for row in con.execute(
            "SELECT text FROM race_lines WHERE asset_id=? ORDER BY line_index",
            (asset["id"],),
        ):
            yield {"type": "race", "speaker": None, "chara_id": None, "text": row["text"]}
        return
    for row in con.execute(
        "SELECT id, name, chara_id, text, is_narration, is_monologue FROM blocks "
        "WHERE asset_id=? ORDER BY block_index",
        (asset["id"],),
    ):
        if row["is_narration"]:
            turn_type = "narration"
        elif row["is_monologue"]:
            turn_type = "monologue"
        else:
            turn_type = "dialogue"
        yield {
            "type": turn_type,
            "speaker": row["name"] or None,
            "chara_id": row["chara_id"],
            "text": row["text"],
        }
        for text in choices.get(row["id"], []):
            yield {"type": "choice", "speaker": None, "chara_id": None, "text": text}


def write_jsonl(out, record) -> None:
    out.write(json.dumps(record, ensure_ascii=False) + "\n")


def export_scene(con, assets, out, dedup: bool) -> int:
    choices = load_choices(con)
    seen: set[str] = set()
    count = 0
    for i, asset in enumerate(assets, 1):
        turns = []
        for turn in asset_turns(con, asset, choices):
            if dedup and turn["text"]:
                if turn["text"] in seen:
                    continue
                seen.add(turn["text"])
            turns.append(turn)
        if not turns:
            continue
        write_jsonl(
            out,
            {
                "path": asset["path"],
                "kind": asset["kind"],
                "title": asset["title"],
                "story_id": asset["story_id"],
                "source_type": asset["source_type"],
                "chara_ids": asset["chara_ids"],
                "turns": turns,
            },
        )
        count += 1
        if i % 2000 == 0:
            print(f"  已导出 {i:,}/{len(assets):,}", flush=True)
    return count


def export_chat(
    con, assets, out, target_id: int, target_name: str, template: str, dedup: bool
) -> int:
    choices = load_choices(con)
    system = template.format(name=target_name)
    seen: set[str] = set()
    count = 0
    for i, asset in enumerate(assets, 1):
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        has_assistant = False
        for turn in asset_turns(con, asset, choices):
            text = turn["text"]
            if not text:
                continue
            if turn["type"] in ("dialogue", "monologue") and turn["chara_id"] == target_id:
                role, content = "assistant", text
                has_assistant = True
            elif turn["type"] == "choice":
                role, content = "user", text
            elif turn["type"] in ("narration", "race"):
                role, content = "user", text
            else:
                speaker = turn["speaker"]
                role = "user"
                content = f"{speaker}「{text}」" if speaker else text
            if dedup and role == "assistant":
                if content in seen:
                    continue
                seen.add(content)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
        if not has_assistant:
            continue
        write_jsonl(
            out,
            {
                "messages": messages,
                "metadata": {
                    "path": asset["path"],
                    "title": asset["title"],
                    "story_id": asset["story_id"],
                    "source_type": asset["source_type"],
                },
            },
        )
        count += 1
        if i % 2000 == 0:
            print(f"  已导出 {i:,}/{len(assets):,}", flush=True)
    return count


def iter_block_rows(con, assets, choices, dedup: bool):
    seen: set[str] = set()
    for asset in assets:
        for row in con.execute(
            "SELECT id, name, chara_id, text, is_narration, is_monologue FROM blocks "
            "WHERE asset_id=? ORDER BY block_index",
            (asset["id"],),
        ):
            text = row["text"]
            if dedup and text:
                if text in seen:
                    continue
                seen.add(text)
            block_choices = choices.get(row["id"], [])
            if row["is_narration"]:
                turn_type = "narration"
            elif row["is_monologue"]:
                turn_type = "monologue"
            else:
                turn_type = "dialogue"
            yield {
                "asset_path": asset["path"],
                "kind": asset["kind"],
                "title": asset["title"],
                "source_type": asset["source_type"],
                "story_id": asset["story_id"],
                "type": turn_type,
                "speaker": row["name"] or None,
                "chara_id": row["chara_id"],
                "text": text,
                "choices": json.dumps(block_choices, ensure_ascii=False)
                if block_choices
                else None,
            }
        for row in con.execute(
            "SELECT text FROM race_lines WHERE asset_id=? ORDER BY line_index",
            (asset["id"],),
        ):
            yield {
                "asset_path": asset["path"],
                "kind": asset["kind"],
                "title": asset["title"],
                "source_type": asset["source_type"],
                "story_id": asset["story_id"],
                "type": "race",
                "speaker": None,
                "chara_id": None,
                "text": row["text"],
                "choices": None,
            }


def export_blocks(con, assets, out, fmt: str, dedup: bool) -> int:
    choices = load_choices(con)
    rows = iter_block_rows(con, assets, choices, dedup)
    if fmt == "jsonl":
        count = 0
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                write_jsonl(fh, row)
                count += 1
        return count
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("导出 Parquet 需要 pyarrow：uv run --with pyarrow python ...")
    writer = None
    batch: list[dict] = []
    count = 0
    for row in rows:
        batch.append(row)
        count += 1
        if len(batch) >= 50000:
            table = pa.Table.from_pylist(batch)
            if writer is None:
                writer = pq.ParquetWriter(out, table.schema)
            writer.write_table(table)
            batch.clear()
    if batch:
        table = pa.Table.from_pylist(batch)
        if writer is None:
            writer = pq.ParquetWriter(out, table.schema)
        writer.write_table(table)
    if writer is not None:
        writer.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从语料库数据库导出训练格式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", type=Path, required=True, help="语料库 .db 文件")
    parser.add_argument("--out", type=Path, required=True, help="输出文件")
    parser.add_argument("--format", choices=["jsonl", "parquet"], default="jsonl")
    parser.add_argument("--mode", choices=["scene", "chat", "blocks"], default="scene")
    parser.add_argument("--target-chara", help="chat 模式的目标角色（chara_id 或名字）")
    parser.add_argument("--system-template", default=DEFAULT_SYSTEM_TEMPLATE,
                        help="chat 模式的 system 提示，{name} 会替换为角色名")
    parser.add_argument("--kind", help="只导出指定类型，逗号分隔（story,home,race）")
    parser.add_argument("--limit", type=int, default=None, help="最多导出 N 个资源")
    parser.add_argument("--dedup", action="store_true", help="按文本去重（保留首次出现）")
    args = parser.parse_args()

    if args.format == "parquet" and args.mode != "blocks":
        sys.exit("Parquet 目前只支持 --mode blocks")
    if args.mode == "chat" and not args.target_chara:
        sys.exit("chat 模式需要 --target-chara <chara_id|名字>")

    con = open_db(args.db)
    kinds = [k.strip() for k in args.kind.split(",")] if args.kind else []
    assets = fetch_assets(con, kinds, args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"模式={args.mode} 资源数={len(assets):,} -> {args.out}")

    if args.mode == "blocks":
        n = export_blocks(con, assets, args.out, args.format, args.dedup)
    else:
        with open(args.out, "w", encoding="utf-8", newline="\n") as out:
            if args.mode == "scene":
                n = export_scene(con, assets, out, args.dedup)
            else:
                target_id, target_name = resolve_chara(con, args.target_chara)
                print(f"目标角色：{target_name} ({target_id})")
                n = export_chat(
                    con, assets, out, target_id, target_name, args.system_template, args.dedup
                )
    print(f"完成：{n:,} 条记录 -> {args.out}")


if __name__ == "__main__":
    main()
