# PluginBuilder V2

> **Paired project:** this repository is the *build system*; the sibling
> [`../Plugin_FFT`](../Plugin_FFT) repository is a full *consumer* of it — a real-time FFT analysis
> CHOP that is configured, compiled, hot-reloaded and tested through PluginBuilder. Whenever a
> contract below (CMake functions, `plugin.json`, loader chain, `__Plugins__/` layout) changes,
> Plugin_FFT is the integration case that exercises it: `python dev/ci.py --project
> ../Plugin_FFT/PluginProjects/FFT` is the cross-repo smoke test, and several hot-reload fixes in
> the changelog (e.g. 2.1.1's parameter-definition refresh) were discovered while iterating on the
> FFT plugin. See *Relationship to Plugin_FFT* at the end of this file.

PluginBuilder is a development tool for building, hot-reloading and installing C++ custom operators
(CHOP / TOP / DAT / SOP / **POP**) for TouchDesigner, from inside TouchDesigner. It drives CMake + Ninja
through a persistent MSVC environment, recompiles on every source save (usually well under a second),
swaps the new DLL into the running `.toe` without an unload gap, mirrors the plugin's custom parameters
(pages preserved) onto the PluginBuilder COMP, and reports build results (status, timings, parsed compiler
errors) on the COMP itself.

V2 compiles against the **CHOP API 10 / Common API 3** headers of TouchDesigner **2025.33230** (`include/`,
synced from the installed `Samples/CPlusPlus` by `dev/sync_templates.py`; see `include/sdk_versions.json`) and
warns when the running TouchDesigner ships a different SDK version.

> **Release status (2026-11-18).** The Python, CMake and templates are release-ready (51 unit tests, every
> non-CUDA template builds warning-free on MSVC 14.52, the Plugin_FFT cross-repo build passes). **`PluginBuilder.tox`
> is not**: it is still the 2024 upstream export and must be re-exported from a live session with
> `dev/deploy.py` (which checks that the `PluginBuilderExt` / `PluginBuilderCore` / `CMakeBlocks` DATs are
> file-linked to `source/`) before it is distributed. See `../RELEASE_AUDIT_2026-11-18.md`.

## Requirements

- **TouchDesigner** 2025.33230 (the build the headers were synced from; tested). Other 2025 builds should work —
  `setParameterEnableStates` (Common API 3) occupies a former reserved vtable slot — but are untested; the extension
  warns when the running TouchDesigner's SDK version differs. Commercial / pro / educational license (CPlusPlus
  operators).
- **Visual Studio** 2022 or 2026 with the *Desktop development with C++* workload (found automatically via `vswhere`).
- **CMake** ≥ 3.21 and **Ninja** — the copies bundled with Visual Studio's *C++ CMake tools* component are
  found automatically; otherwise put them on `PATH` or set the paths in `settings.ini`.
- **Windows** x64. (Optional) CUDA Toolkit 12.x for the CUDA templates.

## Installation

1. Clone / download PluginBuilder_V2.
2. Open `SetSettings.toe`, set `PluginBuilderDir` in the `settings` DAT (everything else is optional — see
   `dev/settings_template.ini`) and run it. `settings.ini` is written to `%APPDATA%/IntentDev/PluginBuilder/`.
3. (Optional) `python dev/sync_templates.py` to resync `include/` and `templates/` from a different TouchDesigner build.

## Usage

1. Drag `PluginBuilder.tox` into a TouchDesigner project **saved on disk**.
2. Enter a **Plugin Name** (letters, digits, `_`; it becomes the CMake target, class name and DLL name — the
   TouchDesigner opType is derived and validated automatically, e.g. `FFT` → `Fft`, with a collision check
   against built-in operators).
3. Pick a **Plugin Template** (BasicCHOP, CHOPWithPythonClass, BasicDAT, CPUMemoryTOP, CudaTOP, SimpleShapesSOP,
   SimpleShapesPOP, CudaPOP — discovered from `templates/*/template.json`).
4. **Create Plugin** generates
   - `PluginProjects/<Name>/CMakeLists.txt` (15 lines; includes `cmake/TDPlugin.cmake`), `plugin.json`,
     `CMakePresets.json`, `launch.vs.json`, `.vscode/` (attach-to-TouchDesigner + IntelliSense via
     `compile_commands.json`) and `source/` from the template,
   - `__Plugins__/<Name>/` for the deployed DLL(s),
   - the loader chain (`in1` → `plugin_loader` → `out1`) inside the COMP,
   and starts the first configure + build.
5. Edit the sources. With **Compile On Update** on, every save triggers a Ninja build; when it succeeds and the DLL
   actually changed (hash-gated), the DLL and any runtime DLLs are swapped in with the rename-in-place trick and
   the loader is reloaded with **fresh parameter definitions** (unload, recreate the `plugin_loader` node if
   TouchDesigner kept the old parameter objects, load, restore every value / expression / binding) — so changed
   menu entries, labels and ranges appear without restarting TouchDesigner. Custom parameters are mirrored onto
   the COMP with their original pages and BIND expressions.
6. **Status** page: `Build Status`, `Last Build`, `Loaded DLL`, `Cancel Build` (kills the whole build process
   tree), `Clean Build Dir`, `Force Reload Plugin`, `Run Tests (ctest)`, `Generate Parameters (parameters.json)`
   (ParamGen) and `Bake for Performance` (a standalone `cplusplus` node with constant parameters, for shows). Compiler diagnostics are parsed into `builder/build_errors`
   (severity, file, line, code, message) and the first error is shown as a COMP error.
7. **Install Plugin** copies `__Plugins__/<Name>/` to `Documents/Derivative/Plugins/<Name>/`.

Type the name of an existing `PluginProjects/<Name>` to reopen it (the manifest tells PluginBuilder the family).

### Extending a project's CMake

```cmake
td_add_plugin(MyPlugin FAMILY CHOP)
td_plugin_use_fftw3(MyPlugin)              # vendored 3rdParty/fftw3 (plugin, PluginBuilder or TD samples tree)
td_plugin_use_opencv(MyPlugin)             # vendored OpenCV
td_plugin_use_python(MyPlugin)             # embedded CPython headers / import lib
td_plugin_optimize(MyPlugin AVX2 FAST_MATH LTO)
td_plugin_add_runtime_dll(MyPlugin "${SOME_DLL}")
td_plugin_add_test(MyPlugin my_tests SOURCES tests/main.cpp)   # headless ctest target
td_plugin_add_bench(MyPlugin my_bench SOURCES bench/main.cpp)
```
See `cmake/TDPlugin.cmake` for the full list and the `TD_PLUGIN_WARNINGS` / `TD_PLUGIN_ASAN` options.

## Visual Studio / VS Code

- **Visual Studio**: open the project folder as a CMake project; `launch.vs.json` launches TouchDesigner with the
  `.toe` under the debugger (turn *Compile On Update* off first so VS owns the build).
- **VS Code**: `.vscode/launch.json` has *Attach to running TouchDesigner* (the hot-reload-friendly way) and
  *Launch TouchDesigner*; IntelliSense uses `build/compile_commands.json`.

## Development

```
python -m unittest discover -s tests -v      # 51 headless tests (Core + the Ext against a fake TD surface)
python dev/ci.py --all                       # + scaffold every template and build the non-CUDA ones
python dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT   # cross-repo: build + ctest the FFT CHOP in its build/ci tree
python dev/sync_templates.py                 # resync SDK headers + templates from the installed TouchDesigner
```
`source/PluginBuilderCore.py` holds all TouchDesigner-independent logic (naming, settings, toolchain
discovery, templates, manifests, diagnostics parsing, the build runner, hot-swap file replacement);
`source/PluginBuilderExt.py` is the thin TouchDesigner adapter. The extension loads both from disk under
`PluginBuilderDir/source`, so the `.tox` always runs the current code. `dev/deploy.py` (run inside `dev/dev.toe`)
re-exports `PluginBuilder.tox`.

### Source map (where to look first)

| File / folder | What lives there |
|---|---|
| `source/PluginBuilderCore.py` | Pure Python: naming/validation, settings, template loading + rendering, CMake text assembly, `plugin.json` manifest, MSVC/toolchain discovery, `safe_replace_file` / lock detection, SDK-version checks, diagnostic parsing, `BuildRunner` job queue, parameter-persistence classification |
| `source/PluginBuilderExt.py` | TouchDesigner adapter: status page, path properties, create/install plugin, loader chain, build orchestration, hash-gated hot reload, fresh-loader recreation, parameter mirroring with BIND expressions |
| `source/CMakeBlocks.py` | Emits the ~15-line generated `CMakeLists.txt` (header comment + `include(TDPlugin.cmake)` + `td_add_plugin`) |
| `cmake/TDPlugin.cmake` | The shared CMake module every generated project includes: `td_add_plugin`, `td_plugin_use_*` (fftw3 / cuda / python / opencv), `td_plugin_optimize`, runtime-DLL staging, test/bench targets |
| `cmake/TDDeploy.cmake` | Rename-in-place post-build deploy for *standalone* (non-PluginBuilder) builds — same trick the extension uses at runtime |
| `templates/*/` | Nine scaffolds (BasicCHOP, CHOPWithPythonClass, BasicDAT, CPUMemoryTOP, CudaTOP, SimpleShapesPOP, CudaPOP, SimpleShapesSOP, **RealtimeCHOP** — the lock-free worker + triple-buffer pattern with tests, bench and a ParamGen spec); each is `template.json` + `source/` with `#__OP_*__#` placeholders, plus a `NOTES.md` |
| `include/` | Vendored Derivative C++ SDK headers (CHOP API 10, Common API 3; TD 2025.33230) + `sdk_versions.json` |
| `source/ParamGen.py` | `parameters.json` → `Parameters.h/.cpp` (names, enum menus, one `eval()` per cook, clamps, `enable_if`) |
| `tests/` | `test_core.py` (Core) and `test_ext.py` (the Ext against a fake TouchDesigner surface: mirroring, enable-state sync, reload snapshot/restore and rollback) |
| `dev/` | `ci.py` (headless scaffold+build), `sync_templates.py` (re-import SDK + templates from TD), `deploy.py` (re-export `.tox`), `settings_template.ini` |

## Relationship to Plugin_FFT

[`Plugin_FFT`](../Plugin_FFT) is the reference consumer of this builder — a production C++ CHOP
(FFTW3 / oneMKL spectrum analysis) that lives in a sibling folder and is built *through* this tool:

| Shared contract | How Plugin_FFT uses it |
|---|---|
| `include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)` + `td_add_plugin(FFT FAMILY CHOP)` | `Plugin_FFT/PluginProjects/FFT/CMakeLists.txt` resolves `PLUGIN_BUILDER_DIR` via the sibling path `../../../PluginBuilder_V2` (or `-DPLUGIN_BUILDER_DIR=`) |
| `td_plugin_use_fftw3(... VERSION 3.3.11-avx2 DYNAMIC)` | Pulls the vendored FFTW from `Plugin_FFT/PluginProjects/FFT/3rdParty/fftw3` with no import library linked into the plugin DLL |
| `td_plugin_optimize(FFT AVX2 FAST_MATH LTO)` | `/O2 /Oi /Ot /Gy /arch:AVX2 /fp:fast /GL /LTCG` |
| `plugin.json` manifest | Declares family `CHOP`, optype `Fftcustom`, deps — reopened when you type `FFT` as the plugin name |
| `__Plugins__/FFT/` + rename-in-place swap | Standalone `_b.bat` and PluginBuilder hot-reload both deploy `FFT.dll` + `libfftw3f-*.dll` there; TD loads from this path |
| `dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT` | Cross-repo configure + build + ctest used as a smoke test, in the project's own `build/ci` tree (2.2.1: it used to reconfigure the project's real `build/` and leave `PLUGIN_DIR=__deploy__` in its cache) |
| Hot-reload parameter refresh (2.1.1) | Discovered while iterating on FFT's `Planner = Patient` menu entry — TD keeps stale par objects across re-init, so Ext now unloads, recreates the loader, reloads, and restores values |

Plugin_FFT's own docs (README, CHANGELOG, optimization analysis, roadmap) describe the *DSP* side;
this repository describes the *build/hot-reload* side. When you change a CMake function, a manifest
key, or the loader lifecycle, run both `python dev/ci.py --all` and
`python dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT`.

## Contributing

Issues and pull requests are welcome — please run `python dev/ci.py --all` (and, if you touched the
CMake module or extension lifecycle, the Plugin_FFT cross-repo build) before opening one.
