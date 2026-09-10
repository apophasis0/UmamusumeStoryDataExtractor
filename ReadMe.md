UmamusumeStoryDataExtractor
====

[![build](https://github.com/akemimadoka/UmamusumeStoryDataExtractor/actions/workflows/ci.yml/badge.svg)](https://github.com/akemimadoka/UmamusumeStoryDataExtractor/actions/workflows/ci.yml)

抽取赛马娘的数据并生成兼容于插件 umamusume-localify 的格式的文本

Steam / 全球版的 `meta` 与 `dat` 资源包经过加密，需先用 [`tools/steam/`](tools/steam/README.md)
中的脚本解密后再提取，详见 [tools/steam/README.md](tools/steam/README.md)。

提取结果可通过 [`tools/db/`](tools/db/README.md) 构建成 SQLite 角色扮演语料库
（全文搜索、角色/剧情关联、JSONL/Parquet 训练格式导出）。

本项目与 Cygames 及赛马娘项目无关联，作者不对使用本项目产生的任何问题负责
