# PluginBuilder_V2 — Architecture & Audit (refreshed 2026-09-22; baseline 2026-08-26 / v2.1.0)

> Scope: what the repository actually contains and does today, verified by the headless test suite
> (`python -m unittest discover -s tests`) and the integration script (`python dev/ci.py --all`),
> both of which pass on Windows 11 / VS 18 (MSVC 19.51) / TouchDesigner 2025.33070.
> Historic claims that turned out to be wrong are listed at the end so they are not repeated.
>
> **Updates since the 2026-08-26 baseline (v2.1.0):** the test count is now **29** (was 23 at
> 2.1.0; 2.1.3 added six pure tests for parameter persistence + diagnostic dedupe).
> Releases **2.1.1–2.1.4** landed after the original audit: fresh-loader recreation so parameter
> *definitions* refresh on hot-reload (2.1.1), built-in-par persistence + DLL-map rollback (2.1.2),
> build-queue guard fix + `dedupe_consecutive` (2.1.3), and `Header`/`Header2` exclusion from
> bind/persistence (2.1.4). See `CHANGELOG.md`. The sibling **Plugin_FFT** project is the
> real-world consumer exercised by `dev/ci.py --project` (see §6).

## 1. Layout

```text
PluginBuilder_V2/
├── cmake/
│   ├── TDPlugin.cmake          # shared module: td_add_plugin, td_plugin_use_*, td_plugin_add_test/bench, deploy
│   └── TDDeploy.cmake          # rename-in-place deploy step for standalone builds
├── include/                    # C++ SDK headers synced from TouchDesigner 2025.33070 (API 10) + sdk_versions.json
├── source/
│   ├── PluginBuilderCore.py    # TouchDesigner-independent logic (tested headlessly)
│   ├── PluginBuilderExt.py     # TouchDesigner adapter (extension on the COMP)
│   ├── CMakeBlocks.py          # generator for the 15-line per-project CMakeLists.txt
│   ├── CMakePresets.json / launch.vs.json
├── templates/<Name>/{template.json, source/}   # 8 templates incl. SimpleShapesPOP & CudaPOP
├── tests/test_core.py          # 29 unit tests (was 23 at v2.1.0; +6 in 2.1.3)
├── dev/{ci.py, sync_templates.py, deploy.py, settings_template.ini, dev.toe}
├── 3rdParty/{opencv, Python}   # vendored headers/import libs (also found in <TD>/Samples/CPlusPlus/3rdParty)
├── PluginBuilder.tox, SetSettings.toe, README.md, CHANGELOG.md, LICENSE
```

Sibling consumer (outside this tree): `../Plugin_FFT/PluginProjects/FFT` — a full FFT CHOP built
with this module; used as the cross-repo build target by `dev/ci.py --project`.

## 2. Data flow

```
settings.ini ─▶ parse_settings ─▶ discover_toolchain (settings > vswhere > PATH > VS-bundled)
Create Plugin ─▶ validate name / sanitize opType ─▶ CMakeBlocks.assemble + plugin.json + .vscode + template sources
              ─▶ BuildRunner: [configure] cmake -B build -G Ninja -DPLUGIN_BUILDER_DIR=… -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
              ─▶ [build] ninja -C build ─▶ exit code + parsed diagnostics ─▶ Status page / build_errors DAT
Source save   ─▶ OnSourceUpdate (if Compile On Update) ─▶ [build] (coalesced) ─▶ hash-gated copy
Copy          ─▶ safe_replace_file: <Name>.dll → <Name>.dll.old, copy new, runtime DLLs likewise ─▶ reinitpulse
Loader        ─▶ sync_custom_parameters: signature ─▶ pages mirrored on the COMP with BIND expressions
```

## 3. Contracts a plugin project relies on

| Contract | Where |
|---|---|
| `include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)` + `td_add_plugin(<Name> FAMILY <fam>)` | generated `CMakeLists.txt` |
| Output at `build/bin/<Config>/<Name>.dll`; runtime DLLs staged in the same folder | `TDPlugin.cmake` |
| `PLUGINBUILDER_BUILD=1` in the environment ⇒ CMake skips its own deploy step | `TDPlugin.cmake` / `BuildRunner` env |
| `plugin.json` (`name`, `family`, `optype`, `features`, `deps`); legacy `# {'plugin_type': …}` header still read | `PluginBuilderCore.read_plugin_manifest` |
| `__Plugins__/<Name>/<Name>.dll` is what the loader op points at | extension |

## 4. Known limitations / open items

- Windows only (macOS bundle support exists in the official samples' CMake and could be ported into the module).
- The CUDA templates are scaffolded but not built by `dev/ci.py` (they need a CUDA 12.x toolkit).
- The `.tox` embeds the parameter-callback and folder-watch DATs; the extension and support modules are loaded from
  disk, but the small callback DATs are still only inside the binary `.tox` (externalise via `file`/`syncfile`).
- `addError`/`addWarning` on the COMP are best-effort (guarded); the `Build Status` parameter is the reliable channel.
- Parameter round-trip (TD custom pars → generated `Parameters.h/.cpp`) is not implemented.

## 5. Corrected history

| Earlier claim | Reality (fixed in 2.1.0) |
|---|---|
| "Synchronized official TouchDesigner 2026.20000 C++ SDK headers" (2.0.0) | Headers were API 9 (2023). Now API 10 via `dev/sync_templates.py`. |
| "Zero-latency compilation environment" | A persistent `cmd.exe` with no exit codes; failures were invisible. Replaced by `BuildRunner`. |
| `-DPLUGIN_BUILDER_DIR` makes projects relocatable | It was overridden by `CACHE … FORCE` in the generated file. Now honoured. |
| Debounced copy "ensures a single copy of the final linked DLL" | It unloaded the plugin before checking locks and copied unchanged DLLs. Now hash-gated, rename-in-place. |
| `file_locked()` detects a DLL in use | Opening for read succeeds on a mapped DLL. Now tests write access. |

## 6. Relationship to `../Plugin_FFT` (joint contract)

| Shared piece | PluginBuilder side | Plugin_FFT side |
|---|---|---|
| CMake module | `cmake/TDPlugin.cmake` (`td_add_plugin`, `td_plugin_use_fftw3`, `td_plugin_optimize`) | `PluginProjects/FFT/CMakeLists.txt` includes it via `PLUGIN_BUILDER_DIR` (default `../../../PluginBuilder_V2`, overridable with `-D`) |
| Manifest | `plugin.json` schema + reopen-by-name workflow | `plugin.json`: family CHOP, optype `Fftcustom`, deps fftw3/mkl |
| Deploy | in-TD rename-in-place + fresh-loader recreate (2.1.x) | `__Plugins__/FFT/{FFT.dll,libfftw3f-*.dll}` consumed by the loader |
| SDK headers | `include/` + `dev/sync_templates.py` | same headers (API 10) through the module |
| CI | `dev/ci.py --all` (templates) | `dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT` |
| Hot-reload fixes | 2.1.1 parameter-definition refresh, hash-gated swap | discovered while iterating on FFT's menu entries live |

Run **both** CI lines before accepting changes to `cmake/`, `source/PluginBuilderExt.py`
(loader / parameter mirroring), `plugin.json` handling, or `include/`.
