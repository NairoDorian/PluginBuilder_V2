# Changelog — PluginBuilder_V2

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [2.1.1] - 2026-09-10

### Fixed
- **Hot reload now refreshes parameter definitions.** TouchDesigner keeps the parameter objects of a C++ OP across
  a plugin re-init: `setupParameters()` adds new parameters, but an existing one keeps its stored definition, so a
  changed menu entry / label / range only appeared after restarting TouchDesigner (seen with the FFT plugin's new
  `FFT Planner = Patient` entry). Every reload (automatic after a build, and `Force Reload Plugin`) now unloads the
  plugin first, recreates the `plugin_loader` node (same name, position and wires) when TouchDesigner kept the
  parameter objects, loads the new DLL, and restores every custom parameter value / expression / binding (menu
  values by entry name). The log reports added / removed parameters and changed menus.

## [2.1.0] - 2026-08-26

### SDK
- **C++ API 10 headers** (CHOP 10 / TOP 12 / SOP 4 / DAT 4 / POP 1, Common 2) synced from TouchDesigner
  2025.33070 `Samples/CPlusPlus` by the new `dev/sync_templates.py`; `include/sdk_versions.json` records what is
  vendored and the extension warns at init when the running TouchDesigner ships different versions.
  (2.0.0 claimed a "2026.20000 SDK sync" but still carried the API-9 headers.)
- All templates regenerated from the installed samples with `setAPIVersion()`, `opHelpURL` and metadata placeholders;
  **new POP templates** `SimpleShapesPOP` and `CudaPOP`. Templates are described by `templates/<Name>/template.json`
  and discovered at runtime (the template menu is populated from them).

### Build system
- **`cmake/TDPlugin.cmake`** shared module (`td_add_plugin`, `td_plugin_optimize`, `td_plugin_use_fftw3/cuda/python/opencv`,
  `td_plugin_add_runtime_dll`, `td_plugin_add_test`, `td_plugin_add_bench`) + `cmake/TDDeploy.cmake`
  (rename-in-place deploy for standalone builds). Generated `CMakeLists.txt` is 15 lines and includes the module,
  so fixes reach existing projects on their next configure.
- `-DPLUGIN_BUILDER_DIR` / `-DPLUGIN_DIR` passed by PluginBuilder are honoured (2.0.0 baked absolute paths with
  `CACHE … FORCE`, silently ignoring both). `PLUGIN_BUILDER_DIR` can also come from the environment.
- `compile_commands.json` exported on every configure; `/Zi` only for Debug/RelWithDebInfo, `/INCREMENTAL:NO` for
  Release; `_USE_MATH_DEFINES`, `/utf-8`, `/Zc:__cplusplus`, optional `/W4 /permissive-` (`TD_PLUGIN_WARNINGS`)
  and `/fsanitize=address` (`TD_PLUGIN_ASAN`); CUDA architectures 75…120; explicit `python3xx.lib` link.
- `plugin.json` manifest per project (family, optype, template, features, API versions); the legacy
  `# {'plugin_type': …}` header is still written and read.

### Extension (`source/PluginBuilderExt.py`, `source/PluginBuilderCore.py`)
- **Build runner with exit codes**: `vcvarsall` is captured once into an environment, cmake/ninja/ctest run as
  individual jobs inside a Windows job object (children die with the runner), output is drained on the main thread,
  and every job reports success/failure and duration. Compiler/linker/CMake/Ninja diagnostics are parsed into
  `builder/build_errors` and surfaced as COMP errors; new **Status** page (`Build Status`, `Last Build`,
  `Loaded DLL`, `Cancel Build`, `Clean Build Dir`, `Force Reload Plugin`, `Run Tests`).
- **Hot reload without an unload gap**: the built DLL is hashed (no reload when unchanged), the loaded DLL is
  renamed to `.old` and the new one copied in (Windows allows renaming a mapped DLL), then the loader re-inits.
  Runtime DLLs staged next to the plugin (e.g. `libfftw3f-3.dll`) are deployed the same way. `file_locked()`
  now tests for write access (a mapped DLL is readable, so the old check never detected locks).
- **Parameter mirroring** keeps the plugin's parameter **pages** (`MirrorPages` option), groups vector
  components through `tupletName`, copies menus before values, uses a signature to skip rebuilds when nothing
  changed, and re-binds cheaply otherwise.
- **Graceful configuration**: missing/invalid `settings.ini` no longer kills the extension; only
  `PluginBuilderDir` is required — `vcvarsall`, `ninja` and `cmake` are discovered via `vswhere`, `PATH` and the
  Visual Studio-bundled tools. Lazy runner start; no CMake configure at every init (inputs are hashed).
- Plugin name validation and opType sanitisation (`FFT_v2` → `Fftv2`) with a built-in operator collision check;
  `TDProjectName` handles dotted names; `Compile On Update` is respected by `OnSourceUpdate`; repeated saves
  during a build coalesce into one rebuild; `.vscode/` generated per project.
- POP loader support (`cplusplusPOP`) when the running TouchDesigner has it.
- The extension loads `PluginBuilderCore` / `CMakeBlocks` from `PluginBuilderDir/source` on disk, so a stale
  DAT inside the `.tox` can no longer shadow the current code.

### Tooling
- `tests/test_core.py` — 23 headless unit tests (naming, settings, templates, manifests, diagnostics, runner,
  hot-swap, SDK versions, VS Code rendering).
- `dev/ci.py` — scaffolds every template, configures + builds the non-CUDA ones and optional existing projects
  (`--project`), usable in GitHub Actions `windows-latest`.
- `dev/sync_templates.py` — regenerates `include/` and `templates/` from an installed TouchDesigner.
- `dev/deploy.py` verifies the code DATs are file-linked and strips `__pycache__` before exporting the `.tox`.

### Docs
- README rewritten; AUDIT.md replaced by a current-state audit; settings template documents the optional keys.

---

## [2.0.0] - 2026-07-24

- Dynamic parameter mirroring onto the COMP's `Custom` page with BIND expressions and vector support.
- Targeted loader re-init instead of recursive COMP cooking; persistent `cmd.exe` + `vcvarsall` subprocess with
  auto-restart; debounced DLL copy with file-lock retries; `ast.literal_eval` for the CMake header comment;
  `CONFIGURE_DEPENDS` globbing; tagged logging.
