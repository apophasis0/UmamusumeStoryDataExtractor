#!/usr/bin/env python3
r"""把提取出的剧情 JSON + 游戏 master.mdb 构建成角色扮演语料库（SQLite）。

数据来源：

* ``--extract-dir``：UmamusumeStoryDataExtractor 的输出目录（每个资源一个 JSON）
* ``--master``：游戏的 ``master.mdb``（明文 SQLite，可选）

产出（默认 ``F:\UmamusumeCorpus\umamusume.db``）：

* ``assets`` / ``blocks`` / ``choices`` / ``color_texts`` / ``race_lines``：剧情语料
* ``characters`` / ``story_meta``：从 master.mdb 派生的角色与剧情元数据
* ``blocks_fts``：FTS5 trigram 全文索引（支持日文子串搜索）
* ``master_*``：master.mdb 的全部表原样导入

用法::

    python tools/db/build_corpus_db.py \
        --extract-dir "F:\UmamusumeStoryDataExtract" \
        --master "F:\...\Persistent\master\master.mdb" \
        --out "F:\UmamusumeCorpus\umamusume.db"

只使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

STORY_ID_PATTERNS = [
    re.compile(r"storytimeline_(\d+)"),
    re.compile(r"hometimeline_\d+_\d+_(\d+)"),
]

SCHEMA = """
CREATE TABLE assets (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    title       TEXT,
    story_id    INTEGER,
    source_type TEXT,
    chara_ids   TEXT,
    block_count INTEGER NOT NULL DEFAULT 0,
    char_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE blocks (
    id           INTEGER PRIMARY KEY,
    asset_id     INTEGER NOT NULL REFERENCES assets(id),
    block_index  INTEGER NOT NULL,
    name         TEXT,
    chara_id     INTEGER,
    text         TEXT,
    is_narration INTEGER NOT NULL DEFAULT 0,
    is_monologue INTEGER NOT NULL DEFAULT 0,
    UNIQUE(asset_id, block_index)
);

CREATE TABLE choices (
    block_id     INTEGER NOT NULL REFERENCES blocks(id),
    choice_index INTEGER NOT NULL,
    text         TEXT,
    PRIMARY KEY (block_id, choice_index)
);

CREATE TABLE color_texts (
    block_id    INTEGER NOT NULL REFERENCES blocks(id),
    color_index INTEGER NOT NULL,
    text        TEXT,
    PRIMARY KEY (block_id, color_index)
);

CREATE TABLE race_lines (
    asset_id   INTEGER NOT NULL REFERENCES assets(id),
    line_index INTEGER NOT NULL,
    text       TEXT,
    PRIMARY KEY (asset_id, line_index)
);

CREATE TABLE characters (
    chara_id      INTEGER PRIMARY KEY,
    name          TEXT,
    cv            TEXT,
    name_variants TEXT
);

CREATE TABLE story_meta (
    story_id      INTEGER PRIMARY KEY,
    source_type   TEXT,
    chara_id      INTEGER,
    chara_ids     TEXT,
    episode_index INTEGER,
    part_id       INTEGER,
    story_number  INTEGER,
    event_id      INTEGER,
    title         TEXT
);

CREATE VIEW v_scene_turns AS
SELECT a.path, a.kind, a.title, a.story_id, a.source_type,
       b.block_index, b.name, b.chara_id, b.text,
       b.is_narration, b.is_monologue
FROM blocks b
JOIN assets a ON a.id = b.asset_id
ORDER BY a.path, b.block_index;
"""

FTS_DDL = """
CREATE VIRTUAL TABLE blocks_fts USING fts5(
    text, name,
    content='blocks', content_rowid='id',
    tokenize='trigram'
);
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_story_id(path: str) -> int | None:
    for pattern in STORY_ID_PATTERNS:
        m = pattern.search(path)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# master.mdb
# ---------------------------------------------------------------------------


def import_master(con: sqlite3.Connection, master_path: Path) -> None:
    """把 master.mdb 的全部表（含主键/索引）复制进主库，保持原表名。"""
    existing = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master")}
    con.execute("ATTACH DATABASE ? AS master", (str(master_path),))
    try:
        objects = con.execute(
            "SELECT type, name, sql FROM master.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        collisions = [
            name for type_, name, _ in objects if type_ == "table" and name in existing
        ]
        if collisions:
            raise SystemExit(
                f"master 表名与语料库表冲突：{collisions}，请用 --skip-master 或改名"
            )

        tables = [(t, n, s) for t, n, s in objects if t == "table"]
        others = [(t, n, s) for t, n, s in objects if t != "table"]
        for type_, name, sql in tables + others:
            con.execute(sql)
        for _, name, _ in tables:
            con.execute(f'INSERT INTO main."{name}" SELECT * FROM master."{name}"')
        log(f"master.mdb 导入完成：{len(tables)} 张表")
    finally:
        con.execute("DETACH DATABASE master")


def build_characters(con: sqlite3.Connection) -> dict[str, int]:
    """从 chara_data / text_data 构建角色表，返回 名字->chara_id 映射。"""
    names: dict[int, str] = {}
    cvs: dict[int, str] = {}
    variants: dict[str, int] = {}

    def add_variant(text: str, chara_id: int) -> None:
        text = text.strip()
        if text:
            variants.setdefault(text, chara_id)

    for cat in (6, 170, 182):
        for idx, text in con.execute(
            'SELECT "index", text FROM text_data WHERE category=? AND text IS NOT NULL',
            (cat,),
        ):
            names.setdefault(idx, text)
            add_variant(text, idx)
    for cat in (4, 5):
        for idx, text in con.execute(
            'SELECT "index", text FROM text_data WHERE category=? AND text IS NOT NULL',
            (cat,),
        ):
            add_variant(re.sub(r"^\[[^\]]*\]", "", text), idx // 100)
    for idx, text in con.execute(
        'SELECT "index", text FROM text_data WHERE category=7 AND text IS NOT NULL'
    ):
        cvs.setdefault(idx, text)

    variants_by_chara: dict[int, list[str]] = {}
    for name, chara_id in variants.items():
        variants_by_chara.setdefault(chara_id, []).append(name)

    rows = []
    for (chara_id,) in con.execute("SELECT id FROM chara_data ORDER BY id"):
        rows.append(
            (
                chara_id,
                names.get(chara_id),
                cvs.get(chara_id),
                "\n".join(variants_by_chara.get(chara_id, [])),
            )
        )
    con.executemany(
        "INSERT INTO characters(chara_id, name, cv, name_variants) VALUES (?,?,?,?)",
        rows,
    )
    log(f"角色表：{len(rows)} 个角色，{len(variants)} 个名字变体")
    return variants


def build_story_meta(con: sqlite3.Connection) -> dict[int, dict]:
    """从 master 剧情表汇总 story_id -> 元数据。"""
    meta: dict[int, dict] = {}

    def add(story_id: int | None, **kw) -> None:
        if story_id and story_id > 0 and story_id not in meta:
            meta[story_id] = kw

    titles_chara = dict(
        con.execute('SELECT "index", text FROM text_data WHERE category=92')
    )
    titles_main = dict(
        con.execute('SELECT "index", text FROM text_data WHERE category=94')
    )
    titles_single = dict(
        con.execute('SELECT "index", text FROM text_data WHERE category=181')
    )
    titles_event = dict(
        con.execute('SELECT "index", text FROM text_data WHERE category=191')
    )

    for sid, c1, c2, c3, num in con.execute(
        "SELECT story_id, chara_id_1, chara_id_2, chara_id_3, num FROM home_story_trigger"
    ):
        chara_ids = [c for c in (c1, c2, c3) if c]
        add(
            sid,
            source_type="home",
            chara_id=chara_ids[0] if chara_ids else None,
            chara_ids=",".join(map(str, chara_ids)) or None,
            episode_index=num,
        )

    for sid, chara_id, episode in con.execute(
        "SELECT story_id, chara_id, episode_index FROM chara_story_data"
    ):
        add(
            sid,
            source_type="chara",
            chara_id=chara_id,
            chara_ids=str(chara_id) if chara_id else None,
            episode_index=episode,
            title=titles_chara.get(sid),
        )

    for row in con.execute(
        "SELECT id, part_id, episode_index, story_number, "
        "story_id_1, story_id_2, story_id_3, story_id_4, story_id_5 FROM main_story_data"
    ):
        mid, part, episode, number = row[:4]
        for sid in row[4:]:
            add(
                sid,
                source_type="main",
                episode_index=episode,
                part_id=part,
                story_number=number,
                title=titles_main.get(mid),
            )

    for row in con.execute(
        "SELECT id, story_event_id, episode_index_id, "
        "story_id_1, story_id_2, story_id_3, story_id_4, story_id_5 FROM story_event_story_data"
    ):
        eid, event_id, episode = row[:3]
        for sid in row[3:]:
            add(
                sid,
                source_type="event",
                episode_index=episode,
                event_id=event_id,
                title=titles_event.get(eid),
            )

    for sid, card_chara, support_chara in con.execute(
        "SELECT story_id, card_chara_id, support_chara_id FROM single_mode_story_data "
        "WHERE story_id > 0"
    ):
        chara_id = card_chara or support_chara or None
        add(
            sid,
            source_type="single_mode",
            chara_id=chara_id,
            chara_ids=str(chara_id) if chara_id else None,
            title=titles_single.get(sid),
        )

    con.executemany(
        "INSERT INTO story_meta(story_id, source_type, chara_id, chara_ids, "
        "episode_index, part_id, story_number, event_id, title) "
        "VALUES (:story_id, :source_type, :chara_id, :chara_ids, "
        ":episode_index, :part_id, :story_number, :event_id, :title)",
        [
            {
                "story_id": sid,
                "source_type": m.get("source_type"),
                "chara_id": m.get("chara_id"),
                "chara_ids": m.get("chara_ids"),
                "episode_index": m.get("episode_index"),
                "part_id": m.get("part_id"),
                "story_number": m.get("story_number"),
                "event_id": m.get("event_id"),
                "title": m.get("title"),
            }
            for sid, m in meta.items()
        ],
    )
    log(f"剧情元数据：{len(meta)} 个 story_id")
    return meta


# ---------------------------------------------------------------------------
# 提取数据导入
# ---------------------------------------------------------------------------


def import_extracted(
    con: sqlite3.Connection,
    extract_dir: Path,
    variants: dict[str, int],
    story_meta: dict[int, dict],
    limit: int | None,
) -> None:
    files = [
        os.path.join(dp, f)
        for dp, _, fs in os.walk(extract_dir)
        for f in fs
        if f.endswith(".json")
    ]
    files.sort()
    if limit:
        files = files[:limit]
    log(f"待导入 JSON：{len(files):,}")

    stats = {"assets": 0, "blocks": 0, "choices": 0, "colors": 0, "race": 0, "mapped": 0}
    con.execute("BEGIN")
    for i, file_path in enumerate(files, 1):
        rel = os.path.relpath(file_path, extract_dir).replace("\\", "/")
        path = rel[:-5] if rel.endswith(".json") else rel
        kind = path.split("/", 1)[0]
        with open(file_path, encoding="utf-8") as fh:
            data = json.load(fh)

        story_id = parse_story_id(path)
        sm = story_meta.get(story_id) if story_id else None
        source_type = sm.get("source_type") if sm else None
        chara_ids = sm.get("chara_ids") if sm else None
        title = None
        block_count = 0
        char_count = 0

        if isinstance(data, list):  # race
            cur = con.execute(
                "INSERT INTO assets(path, kind, title, story_id, source_type, chara_ids, "
                "block_count, char_count) VALUES (?,?,?,?,?,?,?,?)",
                (path, kind, None, story_id, source_type, chara_ids, 0, 0),
            )
            asset_id = cur.lastrowid
            lines = [(asset_id, j, t) for j, t in enumerate(data)]
            con.executemany(
                "INSERT INTO race_lines(asset_id, line_index, text) VALUES (?,?,?)", lines
            )
            stats["assets"] += 1
            stats["race"] += len(lines)
            continue

        title = data.get("Title")
        blocks = data.get("TextBlockList") or []
        block_rows = []
        choice_rows = []
        color_rows = []
        for j, block in enumerate(blocks):
            if not block:
                continue
            name = block.get("Name")
            text = block.get("Text")
            is_narration = 1 if not name else 0
            is_monologue = 1 if name == "モノローグ" else 0
            chara_id = variants.get(name) if name else None
            if chara_id:
                stats["mapped"] += 1
            block_rows.append((j, name, chara_id, text, is_narration, is_monologue))
            char_count += len(text or "") + len(name or "")
        block_count = len(block_rows)

        cur = con.execute(
            "INSERT INTO assets(path, kind, title, story_id, source_type, chara_ids, "
            "block_count, char_count) VALUES (?,?,?,?,?,?,?,?)",
            (path, kind, title, story_id, source_type, chara_ids, block_count, char_count),
        )
        asset_id = cur.lastrowid
        con.executemany(
            "INSERT INTO blocks(asset_id, block_index, name, chara_id, text, "
            "is_narration, is_monologue) VALUES (?,?,?,?,?,?,?)",
            [(asset_id, *row) for row in block_rows],
        )
        # 同一事务内连续插入，可用 last_insert_rowid 反推各 block 的 id
        last_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        first_id = last_id - len(block_rows) + 1
        block_ids = {row[0]: first_id + k for k, row in enumerate(block_rows)}
        for j, block in enumerate(blocks):
            if not block or j not in block_ids:
                continue
            block_id = block_ids[j]
            for k, text in enumerate(block.get("ChoiceDataList") or []):
                choice_rows.append((block_id, k, text))
            for k, text in enumerate(block.get("ColorTextInfoList") or []):
                color_rows.append((block_id, k, text))
        con.executemany(
            "INSERT INTO choices(block_id, choice_index, text) VALUES (?,?,?)", choice_rows
        )
        con.executemany(
            "INSERT INTO color_texts(block_id, color_index, text) VALUES (?,?,?)", color_rows
        )
        stats["assets"] += 1
        stats["blocks"] += block_count
        stats["choices"] += len(choice_rows)
        stats["colors"] += len(color_rows)
        if i % 5000 == 0:
            log(f"  已导入 {i:,}/{len(files):,}")
    con.execute("COMMIT")

    mapped_pct = stats["mapped"] / stats["blocks"] * 100 if stats["blocks"] else 0
    log(
        f"导入完成：assets={stats['assets']:,} blocks={stats['blocks']:,} "
        f"choices={stats['choices']:,} colors={stats['colors']:,} race_lines={stats['race']:,}"
    )
    log(f"说话人映射：{stats['mapped']:,}/{stats['blocks']:,}（{mapped_pct:.1f}%）")


def build_indexes(con: sqlite3.Connection, with_fts: bool) -> None:
    con.executescript(
        """
        CREATE INDEX idx_blocks_asset ON blocks(asset_id);
        CREATE INDEX idx_blocks_chara ON blocks(chara_id);
        CREATE INDEX idx_assets_story ON assets(story_id);
        CREATE INDEX idx_assets_kind ON assets(kind);
        """
    )
    if with_fts:
        con.executescript(FTS_DDL)
        log("构建 FTS5 trigram 索引（可能需 1-3 分钟）...")
        con.execute("INSERT INTO blocks_fts(blocks_fts) VALUES('rebuild')")
        con.executescript(
            """
            CREATE TRIGGER blocks_ai AFTER INSERT ON blocks BEGIN
                INSERT INTO blocks_fts(rowid, text, name) VALUES (new.id, new.text, new.name);
            END;
            CREATE TRIGGER blocks_ad AFTER DELETE ON blocks BEGIN
                INSERT INTO blocks_fts(blocks_fts, rowid, text, name)
                VALUES ('delete', old.id, old.text, old.name);
            END;
            CREATE TRIGGER blocks_au AFTER UPDATE ON blocks BEGIN
                INSERT INTO blocks_fts(blocks_fts, rowid, text, name)
                VALUES ('delete', old.id, old.text, old.name);
                INSERT INTO blocks_fts(rowid, text, name) VALUES (new.id, new.text, new.name);
            END;
            """
        )
    con.execute("ANALYZE")
    con.execute("PRAGMA optimize")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把提取的剧情 JSON + master.mdb 构建为 SQLite 语料库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--extract-dir", type=Path, required=True, help="提取结果目录")
    parser.add_argument("--master", type=Path, help="master.mdb 路径（默认导入）")
    parser.add_argument("--out", type=Path, required=True, help="输出 .db 路径")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--skip-master", action="store_true", help="不导入 master.mdb")
    parser.add_argument("--skip-fts", action="store_true", help="不建全文索引")
    parser.add_argument("--limit", type=int, default=None, help="只导入前 N 个 JSON（测试用）")
    args = parser.parse_args()

    if not args.extract_dir.is_dir():
        sys.exit(f"提取目录不存在：{args.extract_dir}")
    if not args.skip_master and not args.master:
        sys.exit("需要 --master <master.mdb>，或使用 --skip-master")
    if args.master and not args.master.exists():
        sys.exit(f"master.mdb 不存在：{args.master}")
    if args.out.exists():
        if not args.force:
            sys.exit(f"输出已存在：{args.out}（加 --force 覆盖）")
        args.out.unlink()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    con = sqlite3.connect(args.out, isolation_level=None)
    try:
        con.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;"
        )
        con.executescript(SCHEMA)

        if args.skip_master:
            variants, story_meta = {}, {}
            log("跳过 master.mdb 导入")
        else:
            import_master(con, args.master)
            variants = build_characters(con)
            story_meta = build_story_meta(con)

        import_extracted(con, args.extract_dir, variants, story_meta, args.limit)
        build_indexes(con, not args.skip_fts)

        log("统计：")
        for table in ("assets", "blocks", "choices", "color_texts", "race_lines",
                      "characters", "story_meta"):
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            log(f"  {table}: {n:,}")
        by_kind = con.execute(
            "SELECT kind, count(*) FROM assets GROUP BY kind ORDER BY kind"
        ).fetchall()
        log(f"  按类型：{', '.join(f'{k}={n:,}' for k, n in by_kind)}")
        mapped = con.execute("SELECT count(*) FROM blocks WHERE chara_id IS NOT NULL").fetchone()[0]
        with_story = con.execute(
            "SELECT count(*) FROM assets WHERE source_type IS NOT NULL"
        ).fetchone()[0]
        log(f"  已映射角色台词：{mapped:,}；已关联 master 剧情的资源：{with_story:,}")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    log(f"完成 -> {args.out}（{time.time() - t0:.0f} 秒，{args.out.stat().st_size / 1e6:.0f} MB）")


if __name__ == "__main__":
    main()
