# AGENTS.md

Extracts story/home/race text from Umamusume (赛马娘) game data into JSON for translation tooling compatible with the umamusume-localify plugin. Windows-only. No tests, linters, or formatters configured.

## Projects
- `UmamusumeStoryDataExtractor/` — main CLI (F#, net8.0, x64). All logic lives in `Program.fs` (~300 lines): entrypoint, write-only JSON converters, Unity asset extraction.
- `UmamusumeStoryDataExtractor.Merger/` — secondary CLI (F#, net8.0). Flattens the extracted per-story JSONs into one flat JSON file.
- `UmamusumeStoryDataExtractor.CppUtility/` — C++/CLI mixed-mode DLL (VC v143, `<CLRSupport>NetCore</CLRSupport>`). Exposes `CppUtility.GetCppStdHash` = MSVC `std::hash<std::wstring_view>` over UTF-16; its `uint64` outputs are the keys used to look up translations. This hash is implementation-specific — do not "fix" it to plain .NET hashing without considering the consuming hash maps.
- `ThirdParty/AssetStudio/` — git submodule (Perfare/AssetStudio, pinned commit) used as a library (`AssetsManager`, `MonoBehaviour`) to load Unity assets. **Initialize before building:** `git submodule update --init` (currently not checked out in a fresh clone).
- `tools/steam/` — Python helpers for the encrypted Steam/Global release (decrypt `meta` + `dat` bundles into a directory the extractor can read); see `tools/steam/README.md`.

## Build (Windows only)
Requires Visual Studio 2022 (MSVC v143 for the vcxproj) + a .NET SDK. Use msbuild from a VS Developer environment; plain `dotnet build` cannot build the solution because it contains a vcxproj. CI (`ci.yml`) does:

```
msbuild UmamusumeStoryDataExtractor.sln -t:restore -p:RestorePackagesConfig=true
msbuild UmamusumeStoryDataExtractor.sln -m "-p:Configuration=Release;Platform=x64"
```

Release artifact = extractor output dir + merger output dir merged into one package. Main exe is x64 (`PlatformTarget x64`).

## Usage (needs game data not in this repo)
- Extractor: `UmamusumeStoryDataExtractor <GameDataDir> <OutputDir> [HashJsonDir]`
  - Discovers assets by reading sqlite DB `<GameDataDir>/meta` (`select n, h from a`) and matching paths against `StoryTimelinePattern` / `HomeTimelinePattern` / `RaceTimelinePattern` (top of `Program.fs`). New story kinds added by the game likely need a new pattern here.
  - Loads each asset from `<GameDataDir>/dat/<h[:2]>/<h>`; one `AssetsManager` per asset is deliberate (see Chinese comment in `Program.fs`), don't "optimize" into a shared instance.
  - Writes one JSON per asset under `<OutputDir>` mirroring the asset's path name; **existing output files are skipped** (reruns are incremental).
  - Optional `[HashJsonDir]`: every `*.json` in it is parsed as `Dictionary<string,string>`; keys that parse as `uint64` are matched against `CppUtility.GetCppStdHash(text)` and the mapped value substitutes the original text (hash-keyed localization, e.g. from the Merger workflow/community maps).
  - Parallel (one thread per CPU) with progress bar; Ctrl-C aborts cleanly.
- Merger: `UmamusumeStoryDataExtractor.Merger <ExtractedDataDir> <OutputJsonPath>` — recursively collects every root-level string property of every extracted `.json` into one flat output object (first occurrence wins for duplicate keys; order is nondeterministic due to `ConcurrentQueue` + `Parallel.ForEach`).

## Data shapes and conventions
- Extractor output format: story files are `{"Title": ..., "TextBlockList": [...]}`; each entry carries `Name`, `Text`, `ChoiceDataList`, `ColorTextInfoList` and optional nested `Siblings` (multiple Clips per block); race files are a plain array of texts. JSON is written with `UnsafeRelaxedJsonEscaping` + `Indented`.
- The custom `JsonConverter`s in `Program.fs` are write-only (`Read` throws `NotImplementedException`); rely on F# `option`/`ValueOption` unwrapping patterns already used in the file.
- Comments, commit messages, and ReadMe are in Chinese; code identifiers are English.
