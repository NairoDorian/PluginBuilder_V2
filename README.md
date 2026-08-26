# PluginBuilder V2

PluginBuilder is a development tool for building, hot-reloading and installing C++ custom operators
(CHOP / TOP / DAT / SOP / **POP**) for TouchDesigner, from inside TouchDesigner. It drives CMake + Ninja
through a persistent MSVC environment, recompiles on every source save (usually well under a second),
swaps the new DLL into the running `.toe` without an unload gap, mirrors the plugin's custom parameters
(pages preserved) onto the PluginBuilder COMP, and reports build results (status, timings, parsed compiler
errors) on the COMP itself.

V2 compiles against the **C++ API 10** headers shipped with TouchDesigner 2025.30000+ (`include/`,
synced from the installed `Samples/CPlusPlus` by `dev/sync_templates.py`) and warns when the running
TouchDesigner ships a different SDK version.

## Requirements

- **TouchDesigner** 2025.30000 or newer (commercial / pro / educational license — CPlusPlus operators).
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
   the loader re-inits. Custom parameters are mirrored onto the COMP with their original pages and BIND expressions.
6. **Status** page: `Build Status`, `Last Build`, `Loaded DLL`, `Cancel Build`, `Clean Build Dir`,
   `Force Reload Plugin`, `Run Tests (ctest)`. Compiler diagnostics are parsed into `builder/build_errors`
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
python -m unittest discover -s tests -v      # 23 headless tests for source/PluginBuilderCore.py
python dev/ci.py --all                       # + scaffold every template and build the non-CUDA ones
python dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT
python dev/sync_templates.py                 # resync SDK headers + templates from the installed TouchDesigner
```
`source/PluginBuilderCore.py` holds all TouchDesigner-independent logic (naming, settings, toolchain
discovery, templates, manifests, diagnostics parsing, the build runner, hot-swap file replacement);
`source/PluginBuilderExt.py` is the thin TouchDesigner adapter. The extension loads both from disk under
`PluginBuilderDir/source`, so the `.tox` always runs the current code. `dev/deploy.py` (run inside `dev/dev.toe`)
re-exports `PluginBuilder.tox`.

## Contributing

Issues and pull requests are welcome — please run `python dev/ci.py --all` before opening one.
