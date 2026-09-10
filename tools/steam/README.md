# Steam / 全球版数据解密与提取

Steam（JP / Global）版《赛马娘》的 `Persistent` 数据与 DMM / Android 版不同，
`UmamusumeStoryDataExtractor` 无法直接读取：

| 数据 | 加密方式 | 说明 |
| --- | --- | --- |
| `meta`（资源索引） | SQLite3 Multiple Ciphers（ChaCha20） | 整文件加密，不是明文 SQLite |
| `dat/<xx>/<hash>` | 逐资源 XOR | UnityFS 资源包，从偏移 `0x100` 开始加密 |

`master.mdb`（游戏数值表）是明文 SQLite，无需处理。

`decrypt_steam_data.py` 可以把 Steam 版数据转换成提取器可直接读取的目录：

```
<out>/
├── meta          # 明文 SQLite（表 a：n=资源名，h=资源哈希，e=逐资源密钥）
└── dat/<xx>/<h>  # 解密后的资源包（默认只含 story/home/race timeline）
```

## 原理与密钥

- **meta**：把服务器 DB 密钥与 `DB_BASE_KEY` 的前 13 字节逐字节 XOR，得到 ChaCha20 密钥
  （JP 与 Global 的服务器密钥不同，用 `--server` 选择）。
- **bundle**：密钥流 `ks[i] = ABKey[i // 8] ^ key_u64[i % 8]`，其中 `key_u64` 是 meta 表 `a`
  的 `e` 列（每个资源一个），`i` 为文件绝对偏移；从 256 开始逐字节 XOR（88 字节循环）。
- 密钥与算法均为公开资料：katboi01/UmaViewer、TheCing/Trackside、Vali-98/umamusu-utils。

## 依赖

- Python 3.10+
- [uv](https://github.com/astral-sh/uv)（推荐，自动安装依赖），或手动安装：
  - `apsw-sqlite3mc` —— 解密 meta（必需）
  - `numpy` —— 批量解密加速（可选，缺失时退回纯 Python）

## 用法

### 一步生成提取器输入目录（推荐）

```powershell
uv run --with apsw-sqlite3mc --with numpy python tools/steam/decrypt_steam_data.py prepare `
    --persistent "F:\SteamLibrary\steamapps\common\UmamusumePrettyDerby_Jpn\UmamusumePrettyDerby_Jpn_Data\Persistent" `
    --out "F:\UmamusumeSteamData"
```

### 分步执行

```powershell
# 1) 解密 meta（不会修改游戏文件）
uv run --with apsw-sqlite3mc python tools/steam/decrypt_steam_data.py meta `
    --meta "<游戏>\...\Persistent\meta" --out "F:\UmamusumeSteamData\meta"

# 2) 批量解密资源包
uv run --with numpy python tools/steam/decrypt_steam_data.py bundles `
    --meta "F:\UmamusumeSteamData\meta" --dat "<游戏>\...\Persistent\dat" --out "F:\UmamusumeSteamData\dat"
```

常用参数：

- `--server jp|global`：选择密钥（默认 `jp`，即 Steam 日服）
- `--pattern <正则>`：可多次指定，替换默认的三类 timeline 正则
- `--jobs N`：并发数（默认 8）
- `--limit N`：只处理前 N 个（测试用）
- 已存在的输出文件会跳过，可随时中断重跑

## 构建提取器

标准环境（VS2022 + .NET 8 SDK）按 CI 命令构建：

```powershell
git submodule update --init
msbuild UmamusumeStoryDataExtractor.sln -t:restore -p:RestorePackagesConfig=true
msbuild UmamusumeStoryDataExtractor.sln -m "-p:Configuration=Release;Platform=x64"
```

> 主程序为 net8.0，CppUtility 必须同为 net8.0（本仓库已修改）。
> 若输出目录中的 `Ijwhost.dll` 是 net6 版本，运行时会报
> `BadImageFormatException`，需使用与 CppUtility 相同 SDK 版本的 `Ijwhost.dll`。

若本机只有 VS 2026 Build Tools（无 v143 工具集、无 F# 支持），可用以下混合方式构建
（已在 Windows + VS 18 Build Tools 上验证；仓库根目录的 `global.json` 已把 SDK 锁定到 8.x）：

```powershell
# 1) C++/CLI：必须用 VS 的 msbuild.exe，工具集覆盖为 v145
& "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\MSBuild\Current\Bin\MSBuild.exe" `
  UmamusumeStoryDataExtractor.CppUtility\UmamusumeStoryDataExtractor.CppUtility.vcxproj `
  "-p:Configuration=Release;Platform=x64;PlatformToolset=v145"

# 2) 复制 dll 到 F# 项目的引用解析路径
New-Item -ItemType Directory -Force UmamusumeStoryDataExtractor.CppUtility\Release | Out-Null
Copy-Item UmamusumeStoryDataExtractor.CppUtility\x64\Release\* UmamusumeStoryDataExtractor.CppUtility\Release\ -Force

# 3) AssetStudio 的 net6.0 引用程序集（一次性）
dotnet msbuild ThirdParty\AssetStudio\AssetStudio\AssetStudio.csproj "-p:Configuration=Release;TargetFramework=net6.0"

# 4) 两个 F# 项目（BuildProjectReferences=false 避免去编 C++）
dotnet msbuild UmamusumeStoryDataExtractor\UmamusumeStoryDataExtractor.fsproj `
  "-p:Configuration=Release;BuildProjectReferences=false" `
  "-p:VCTargetsPath=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\MSBuild\Microsoft\VC\v180\"
dotnet msbuild UmamusumeStoryDataExtractor.Merger\UmamusumeStoryDataExtractor.Merger.fsproj "-p:Configuration=Release"

# 5) 确保 bin 中是 net8 的 Ijwhost.dll
Copy-Item UmamusumeStoryDataExtractor.CppUtility\x64\Release\Ijwhost.dll `
  UmamusumeStoryDataExtractor\bin\Release\net8.0\ -Force
```

## 运行提取器

```powershell
UmamusumeStoryDataExtractor\bin\Release\net8.0\UmamusumeStoryDataExtractor.exe `
    "F:\UmamusumeSteamData" "F:\UmamusumeStoryDataExtract"
```

输出按资源路径组织（`story/...`、`home/...`、`race/...`），已存在的文件会跳过。

本仓库在 Steam JP 版实测结果：**23,331 个 JSON**（story 21,856 / home 1,441 / race 34）。

## 已知问题

- `Merger` 处理包含 race 文件的目录会抛异常（race JSON 根节点是数组，而 Merger 按对象枚举）。
- 密钥来自当前公开资料；若游戏更新更换密钥，需要重新分析。

## 免责声明

仅供个人学习与翻译用途。脚本只读取游戏文件的副本，不会修改游戏安装目录。
