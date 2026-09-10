# 角色扮演语料库（SQLite）

把 `tools/steam/` 提取出的剧情 JSON 与游戏 `master.mdb` 构建成一个 SQLite 数据库，
用于查询、筛选并导出大模型角色扮演语料。

## 数据规模（Steam JP 版实测）

| 表 | 行数 | 说明 |
| --- | ---: | --- |
| `assets` | 23,331 | story 21,856 / home 1,441 / race 34 |
| `blocks` | 676,488 | 文本块（653,181 条有正文） |
| `choices` | 120,936 | 玩家选项 |
| `color_texts` | 12,258 | 正文彩色片段（渲染用） |
| `race_lines` | 1,057 | 赛事文本 |
| `characters` | 173 | 可玩角色（185 个名字变体） |
| `story_meta` | 18,658 | 剧情 ID → 章节/活动/育成元数据 |
| `master_*` | 626 张表 | master.mdb 全量导入（保持原表名） |

数据库约 260MB（含 FTS5 trigram 索引）；构建耗时约 20 秒。

## 技术要点

- **SQLite + FTS5 trigram**：`blocks_fts` 支持日文/中文任意子串搜索（`MATCH 'けっぱります'`）
- **master.mdb 全量导入**：保留原表名、主键与索引，可直接 JOIN `text_data`、`chara_data` 等
- **角色映射**：从 `text_data` cat 4/5/6/7/170/182 提取名字变体，精确匹配 `blocks.name`
  - 有名有姓的台词映射率 **79.3%**（474,223/598,298）
  - 空名字 = 旁白（`is_narration=1`）；`モノローグ` = 内心独白（`is_monologue=1`）
  - `？？？`、`<username>`、路人/实况等保留原始名字，`chara_id` 为空
- **剧情关联**：资源文件名尾部即 master `story_id`（如 `storytimeline_041001001` → 41001001），
  19,470 个资源（83.4%）可关联到 `story_meta`（来源类型/章节/活动/育成）

## 构建

```powershell
python tools/db/build_corpus_db.py `
    --extract-dir "F:\UmamusumeStoryDataExtract" `
    --master "F:\SteamLibrary\...\Persistent\master\master.mdb" `
    --out "F:\UmamusumeCorpus\umamusume.db"
```

参数：`--force` 覆盖重建、`--skip-master`、`--skip-fts`、`--limit N`（测试）。
只依赖 Python 标准库。

## 导出

```powershell
# 1) 按场景的多轮对话（每资源一条，含有序 turns）
python tools/db/export_corpus.py --db F:\UmamusumeCorpus\umamusume.db `
    --out F:\UmamusumeCorpus\exports\scenes.jsonl --mode scene

# 2) 指定角色的 chat 训练格式（该角色为 assistant）
python tools/db/export_corpus.py --db F:\UmamusumeCorpus\umamusume.db `
    --out F:\UmamusumeCorpus\exports\special_week.jsonl --mode chat --target-chara 1001

# 3) block 级平铺数据（Parquet 需要 pyarrow）
uv run --no-project --with pyarrow python tools/db/export_corpus.py `
    --db F:\UmamusumeCorpus\umamusume.db `
    --out F:\UmamusumeCorpus\exports\blocks.parquet --mode blocks --format parquet
```

通用参数：`--kind story,home`、`--limit N`、`--dedup`（按文本去重）、
`--system-template "あなたは「{name}」です。"`（chat 模式）。

已生成的全量导出（`F:\UmamusumeCorpus\exports\`）：

| 文件 | 大小 | 内容 |
| --- | ---: | --- |
| `scenes.jsonl` | 126MB | 23,331 条场景记录 |
| `blocks.parquet` | 33MB | 677,545 行（676,488 blocks + 1,057 race） |
| `chat_1001_special_week.jsonl` | 3.2MB | スペシャルウィーク 的 655 条对话 |

## 表结构

```
assets(id, path UNIQUE, kind, title, story_id, source_type, chara_ids, block_count, char_count)
blocks(id, asset_id, block_index, name, chara_id, text, is_narration, is_monologue)
choices(block_id, choice_index, text)
color_texts(block_id, color_index, text)
race_lines(asset_id, line_index, text)
characters(chara_id PK, name, cv, name_variants)      -- master 派生
story_meta(story_id PK, source_type, chara_id, chara_ids,
           episode_index, part_id, story_number, event_id, title)
blocks_fts(text, name, content='blocks', tokenize='trigram')
v_scene_turns                                          -- 常用查询视图
```

## 常用查询

```sql
-- 日文子串搜索（FTS5 trigram）
SELECT a.path, b.name, b.text
FROM blocks_fts f
JOIN blocks b ON b.id = f.rowid
JOIN assets a ON a.id = b.asset_id
WHERE blocks_fts MATCH 'けっぱります' LIMIT 20;

-- 某个角色的全部台词（1001 = スペシャルウィーク）
SELECT a.path, b.block_index, b.text
FROM blocks b JOIN assets a ON a.id = b.asset_id
WHERE b.chara_id = 1001 ORDER BY a.path, b.block_index LIMIT 20;

-- 某个场景的完整对话（含旁白与选项）
SELECT b.block_index, b.name, b.text, b.is_narration,
       (SELECT group_concat(c.text, ' / ') FROM choices c WHERE c.block_id = b.id) AS choices
FROM blocks b JOIN assets a ON a.id = b.asset_id
WHERE a.path = 'story/data/04/1001/storytimeline_041001001'
ORDER BY b.block_index;

-- 角色台词量排行
SELECT c.name, count(*) AS lines
FROM blocks b JOIN characters c ON c.chara_id = b.chara_id
GROUP BY b.chara_id ORDER BY lines DESC LIMIT 10;

-- 按剧情来源统计
SELECT source_type, count(*) FROM assets GROUP BY source_type ORDER BY 2 DESC;
```

## 已知限制

- 仅可玩角色能映射到 `chara_id`；路人、实况、`？？？` 等保留原始名字
- `title` 字段对部分主页剧情是占位值（如 `"0"`），剧情标题以 `story_meta.title` 为准
- `color_texts` 只是正文的彩色片段，不是独立台词
- 数据库为一次性全量构建；更新数据请加 `--force` 重建
