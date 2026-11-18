# Changelog — PluginBuilder_V2

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

> **Sibling project:** [`../Plugin_FFT`](../Plugin_FFT) is the production consumer of this builder
> (FFT analysis CHOP). Cross-repo verification:
> `python dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT`. Several fixes below (notably
> 2.1.1's parameter-definition refresh and the `td_plugin_use_fftw3` `VERSION`/`DYNAMIC` flags)
> were driven by needs discovered while building and hot-reloading that plugin.

---

## [2.2.2] - 2026-11-19

### Changed
- **`PluginBuilder_V2.tox` is the distributable.** It was re-exported from a live V2 session (2.2.1); the 2024
  upstream `PluginBuilder.tox` is removed from the tree (it is in git history) so nobody installs the stale one.
  `dev/deploy.py` now writes `PluginBuilder_V2.tox`; the README points to it.

### Fixed
- **Cancel no longer misses a job the worker has just dequeued.** `cancel_pending()` decided from what was still
  in the queue; a job taken by the worker but not yet started escaped and ran. It now cancels everything in
  `_pending` under the lock, and a shutdown sentinel found in the queue is put back.
- **Re-baking keeps the baked node's wires and position.** Bake for Performance destroyed an existing
  `<name>_baked` and created an unwired one at the default position, silently disconnecting a show from the
  plugin. Wires in and out are now restored (the connector calls `_recreate_loader_node` uses) and the log says
  how many.

### Added
- **Licensing:** your copyright line in `LICENSE` (MIT) for the V2 work, and `THIRD_PARTY_NOTICES.md` — what the
  MIT license does *not* cover: `include/` and the Derivative-sample templates (Derivative's Shared Use License),
  `3rdParty/opencv` (OpenCV 4.8.0, Apache-2.0) and `3rdParty/Python` (CPython 3.11, PSF License).

### Tests
- 53 unit tests (+2: the dequeued-job cancel, and a re-bake that must keep position and both wires, against a
  wired fake network).

---

## [2.2.1] - 2026-11-18

Release-prep audit of both repositories: `../RELEASE_AUDIT_2026-11-18.md`.

### Fixed
- **A failed hot reload no longer counts as loaded.** `_do_copy_plugin` recorded the new DLL's hash after
  `_reload_loader_fresh` even when that rolled back to the previous DLL, so the next copy of the very same DLL was
  skipped as "unchanged" although it had never loaded. `_reload_loader_fresh` now returns whether the new DLL
  loaded, and the hash is recorded only then.
- **Cancel Build kills the whole build.** `BuildRunner.kill_current()` killed only the direct child; cmake / ninja /
  cl.exe kept running (holding `.obj` / `.pdb` locks) until the runner shut down, and the orphans kept the output
  pipe open, so the cancelled job could not even report done. It now terminates the job object, i.e. the
  running build's whole process tree.
- **`WindowsJobObject` declares its kernel32 signatures** (a private `ctypes.WinDLL`, so no other Python in the
  TouchDesigner process is affected). HANDLEs were passed and returned as 32-bit C ints.
- **`dev/ci.py --project` no longer reconfigures the project's own `build/`.** It passed
  `-DPLUGIN_DIR=<project>/__deploy__`, which stayed in that build tree's CMake cache: every later standalone build
  of the project then deployed to `__deploy__` instead of `__Plugins__/<name>`, and TouchDesigner kept loading a stale
  DLL. Existing projects are now built in `<project>/build/ci`. Its `TD_SAMPLES_DIR` default is the newest
  installed TouchDesigner (`find_td_installs`) instead of an unversioned path a 2025 install does not have.
- **Bake for Performance writes a project-relative DLL path**, like `plugin_loader`. The absolute path broke the
  baked node as soon as the `.toe` moved to another folder or machine.

### Changed
- **`vswhere` runs only when it is needed.** Toolchain discovery ran it (two child processes, ~250 ms measured) on
  TouchDesigner's main thread at every extension init, even when settings.ini named every tool. It now runs only
  when a tool is missing from settings.ini / PATH, and at most once.

### Tests
- 51 unit tests (46 + 5): Cancel kills a grandchild process (Windows; the test fails against the 2.2.0 behaviour),
  `vswhere` is skipped / run once, and three Ext reload tests (success → True, a rolled-back load → False with the
  previous DLL restored and loaded, no loader → False). A pre-existing unclosed-file `ResourceWarning` in the
  RealtimeCHOP scaffold test is fixed; the suite is clean under `-W error::ResourceWarning`.
- `dev/ci.py --all`: every non-CUDA template builds with 0 warnings on MSVC 14.52.37025; RealtimeCHOP's ctest passes.
  `dev/ci.py --project ../Plugin_FFT/PluginProjects/FFT`: build (0 warnings) + ctest pass.

### Still open
- `PluginBuilder.tox` (2024-04-20) and `dev/dev.toe` (2024-07-08) still need a re-export from a live session
  (`dev/deploy.py`). This is the release blocker for distributing the `.tox`.
- The live enable-state mirror (2.2.0) is still not confirmed in TouchDesigner.

---

## [2.2.0] - 2026-11-08

### Added
- **Live enable-state mirror.** A plugin greys parameters out with `setParameterEnableStates` (C++ API
  Common 3) on the hidden `plugin_loader`. The COMP's mirrored parameters copied `enable` / `readOnly` only
  during a full `sync_custom_parameters` (init, reload, Refreshcustompars), so a plugin's greying rules never
  showed on the COMP while you edited it. Now an edit of any mirrored parameter queues **one** deferred pass
  (`_sync_enable_states`, `run(..., delayFrames=2)`, coalesced) that copies just those two attributes, loader
  → mirror. Nothing runs per frame when idle; a pass that finds nothing changed writes nothing. Plugin_FFT
  2.13's Multi-FFT ("Window Sampling is taken over") is the first rule that depends on it.

### Recorded here for the first time (shipped in `24b8ff5`, 2026-09-23, previously in no CHANGELOG entry)
- Off-thread staging and install: rename-in-place deploy, a size+mtime fast path for files older than 2 s,
  a runtime-DLL manifest, and fresh-loader reload with a parameter snapshot/restore.
- **ParamGen** (`source/ParamGen.py`, `dev/gen_parameters.py`): `parameters.json` → `Parameters.h/.cpp`;
  Generate Parameters and Bake for Performance pulses.
- **RealtimeCHOP template** (core header, tests, bench) and a NOTES.md per template; the templates build
  warning-free.
- `cmake/TDPlugin.cmake`: `td_plugin_optimize REALTIME`, `td_plugin_add_bench GATE` (ctest label `perf`),
  `cmake/td_runtime/td_cpu_guard.h`; CUDA enabled at directory scope, MSVC flags scoped to CXX.
- `include/`: SDK headers synced to TouchDesigner 2025.33230 (Common API 3, `BitMaskFlags.h`).
- `dev/ci.py` runs each project's ctest (the `perf` label is excluded).

### Tests
- 46 unit tests (45 + `test_enable_states_follow_the_loader_after_an_edit`: one queued pass per burst of
  edits, enable/readOnly copied, 0 writes in the steady state).

### Still open
- **Not yet confirmed in TouchDesigner** that the CPlusPlus CHOP host applies `setParameterEnableStates` to
  the loader's custom parameters at a point the 2-frame delay catches. If it does not, the delay is the knob
  (`ENABLE_SYNC_DELAY_FRAMES`); the fallback is evaluating a ParamGen spec's `enable_if` rules in Python.
- `PluginBuilder.tox` (2024-04-20) and `dev/dev.toe` (2024-07-08) still need a re-export from a live session.

---

## [docs] - 2026-09-22 — documentation pass (shipped in `24b8ff5`; no version bump)

### Documentation
- **Docs refresh pass.** README and AUDIT brought up to date with the code as of 2.1.4:
  test count corrected **23 → 29** (the 2.1.3 suite), AUDIT dated and annotated with the
  2.1.1–2.1.4 delta, source-map table added to README, and a *Relationship to Plugin_FFT*
  section added to both README and a new AUDIT §6 documenting the cross-repo contract
  (CMake module, manifest, `__Plugins__/` layout, `ci.py --project`, hot-reload feedback loop).
- **Code comments.** Extensive explanatory comments added throughout `source/`, `dev/`,
  `tests/`, `cmake/` and `templates/` (no logic changes).

---

## [2.1.4] - 2026-09-14

### Fixed
- **Header parameters excluded from parameter binding and persistence.** `Header` / `Header2` styles are
  now treated identically to `Pulse` in `_bind_components` (no `bindExpr`, no `ParMode.BIND`), and in
  `is_persisted_par` / `snapshot_par` (headers are visual labels with no value to mirror). Previously the
  sync silently attempted to bind headers, relying on the swallowed `try/except`. (§6.1C)

## [2.1.3] - 2026-09-13

### Fixed
- **Build-queue guard always-True bug.** `any(True for _ in [0])` — which always passed — replaced with
  a real early return; stale-plugin_loader_fresh tolerance, DLL-map failure rollback, re-resolve/
  defensive fallback and per-wire failure logging all hardened. `dedupe_consecutive` added for MSVC
  repeat-diagnostics during batch builds.
- 6 new pure tests for par persistence + dedupe → 29/29 green.

## [2.1.2] - 2026-09-12

### Fixed
- **Hot-reload hardening in `PluginBuilderExt`.** Built-in parameters now persisted across a fresh-node
  reload; stale-`plugin_loader_fresh` tolerance; DLL-map failure rollback with re-resolve/defensive
  fallback; per-wire failure logging.

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
