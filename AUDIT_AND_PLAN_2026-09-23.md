# PluginBuilder_V2 — Audit, Current State & Progression Plan (2026-09-23)

> **What this is.** A full-read review of PluginBuilder_V2: HEAD `738601f` (v2.1.4 plus two loader
> fixes) and an uncommitted working tree of 32 files. It ends in a phased plan. The lens is the one
> this toolchain exists for: **shipping real-time TouchDesigner plugins**, and keeping TouchDesigner
> itself real-time while developing them. No project code was changed.
>
> **How it was produced.**
> - Read in full: `source/*.py`, `cmake/*.cmake`, `dev/*.py`, `tests/test_core.py`, README, AUDIT,
>   CHANGELOG.
> - A second reviewer read all 20 template sources, the SDK headers and `3rdParty/`, and diffed them
>   against the installed official samples.
> - Every Python file was AST-compared between HEAD and the working tree.
> - The 29 unit tests were run.
> - The hot-reload hashing cost was measured on the real `Plugin_FFT` build output.
>
> **Labels:** **verified** (read or reproduced), **measured** (a number from today), **inferred**.
> Severity: 🔴 broken · 🟠 real-time or dev-loop cost · 🟡 hygiene / latent · ⚪ note.

---

## 0. TL;DR

- 🔴 **The uncommitted working copy of `source/PluginBuilderExt.py` does not compile**
  (`IndentationError`, line 1330). The same "comments only" pass also folded the module-level
  `_par_mode()` into dead code after a `return`, which turns every hot-reload snapshot/restore into a
  `NameError`.
  - The CHANGELOG `[Unreleased]` entry says "no logic changes".
  - The 29 tests cannot see this because they only import `PluginBuilderCore`.
  - **Do not commit or re-init the extension from this working tree before fixing it (§2.1 B1–B2).**
- 🔴 **A committed bug (since `5a88fc3`, 2.1.4):** a mis-indented `for … else` in
  `sync_custom_parameters` binds and relabels only the **last** mirrored parameter on the rebuild
  path. It is masked because the next sync's fast path re-binds everything.
- 🟠 **Every hot reload hashes about 550 MB on TouchDesigner's main thread.** `_runtime_dlls_in_bin()`
  picks up every DLL in `build/bin/Release`, including the 18 oneMKL/FFTW runtime DLLs, and
  `safe_replace_file` SHA-256s source and destination. That measured **0.86 s** (warm cache), about
  50 dropped frames at 60 fps, on every rebuild. `install_plugin` does the same with copies.
- 🟠 **The SDK is stale against the installed TouchDesigner.**
  - The machine now has **TD 2025.33230**: Common API **3**, a new `BitMaskFlags.h`, and a new
    `setParameterEnableStates` virtual. `include/` is Common API 2, from 33070.
  - The default path `C:/Program Files/Derivative/TouchDesigner` no longer exists (installs are
    versioned folders). That breaks `sync_templates.py`, `TDPlugin.cmake`'s `TD_SAMPLES_DIR`
    fallback, and the `sdk_versions.json` record.
- 🟡 **`PluginBuilder.tox` and `dev/dev.toe` were last committed on 2024-07-08,** before V2. The
  distributable artefact has never been re-exported since the V2 rewrite.
- 🟡 **Templates are faithful copies of Derivative's samples, including their real bugs** (a SOP
  texcoord overrun, a POP index-buffer create-info bug, a CPUMemoryTOP data race). They are poor
  real-time starting points. The +393 comment lines added to them are wiped by the next
  `sync_templates.py`, and they put the `#__OP_*__#` tokens inside comments, which makes the
  placeholder test vacuous.

The plan (§4): **Phase 0** repairs the tree and the truth (1 day). **Phase 1** gets heavy work off
TD's main thread (hash, copy, print) and resyncs the SDK. **Phase 2** makes the builder produce
real-time-first plugins (an RT template, a parameter generator, perf scaffolding, a production
"bake"). **Phase 3** is CI and distribution.

---

## 1. Current state snapshot

| Item | State | Evidence |
|---|---|---|
| Version | 2.1.4 (CHANGELOG); `[Unreleased]` docs pass | `CHANGELOG.md` |
| Git | `main` = `origin/main` @ `738601f`; 32 modified, 0 untracked | `git status` |
| Working tree | Ext: **does not compile** plus `_par_mode` swallowed plus a duplicate line; Core: `CREATE_NO_WINDOW` moved (benign); CMakeBlocks / ci / deploy / sync / tests: docstring-only; templates: 20 files comment-only | AST diff; second reviewer's stripped-hash diff |
| Tests | `python -m unittest discover -s tests`: **29 passed** (Core only) | measured |
| SDK in `include/` | CHOP 10 / TOP 12 / SOP 4 / DAT 4 / POP 1 / **Common 2** (from TD 33070) | `include/sdk_versions.json` |
| Installed TD | **2025.33230** (Common **3**) in `C:\Program Files\Derivative\TouchDesigner.2025.33230\`; also 2022.32120 | verified on disk |
| Distributable | `PluginBuilder.tox` (14 KB) last committed **2024-07-08** (upstream era) | `git log -- PluginBuilder.tox` |
| Consumer | `../Plugin_FFT` (CHOP, FFTW3 DYNAMIC, LTO, tests + bench through `td_plugin_add_test/bench`) | FFT `CMakeLists.txt` |
| Repo weight | 975 tracked files under `3rdParty/` (Python 3.11.1 headers + 66 libs; OpenCV 4.8.0 headers + 63 libs), ≈ 37 MB of blobs | second reviewer |

### 1.1 Architecture (as built)

```
settings.ini ─ parse_settings ─ discover_toolchain (ini > vswhere > PATH > VS-bundled)
Create ─ validate/sanitize ─ CMakeBlocks.assemble (15-line CMakeLists incl. TDPlugin.cmake)
       ─ plugin.json + .vscode + launch.vs.json + template sources (#__OP_*__# rendered)
       ─ loader chain  in1 → plugin_loader (cplusplusXXX, viewer=True) → out1
BuildRunner (background thread, captured vcvars env, Windows Job Object)
       ─ [configure] cmake -G Ninja   ─ [build] ninja   → parse_diagnostics → Status page / build_errors DAT
Main thread (run() loop, 1 frame busy / 15 frames idle)
       ─ _poll: print every output line ─ on_done ─ _schedule_copy (debounced)
       ─ _do_copy_plugin: lock check → SHA-256 → safe_replace_file (rename-in-place) for DLL + ALL runtime DLLs
       ─ _reload_loader_fresh: snapshot → unload → (recreate node) → load → reinit → restore → _ensure_loader_live
       ─ sync_custom_parameters: mirror loader pars onto the COMP with BIND expressions
```

The split is right: **all blocking work that is *process* work (vcvars, cmake, ninja) is off the main
thread.** What is still on the main thread is file work (hash, copy), textport printing and the loader
reload. The first two are the problem (§2.2).

---

## 2. Findings

### 2.1 🔴 Broken

**B1 — The working-tree `PluginBuilderExt.py` is a syntax error.** Verified with `py_compile`.
- `OnParPulse` gained a stray, over-indented copy of its dispatch line (`PluginBuilderExt.py:1328-1331`):

  ```python
  def OnParPulse(self, par):
          self.on_par_pulse_map[par.name]()      # ← inserted, 12-space indent
      if par.name in self.on_par_pulse_map:      # ← IndentationError here
  ```
- `README` and `deploy.py` state that the COMP's extension DAT is file-linked to `source/*.py`. If
  that holds for the `.tox` in use, the **next extension re-init or `.toe` open fails to create the
  extension**, and the whole builder (create, build, hot reload, parameter mirror) goes dead.
- Whether the `.tox`'s DAT is actually file-linked cannot be read from the compressed `.tox`
  headlessly. Assume yes.

**B2 — `_par_mode()` no longer exists at module scope.** Verified by AST diff against HEAD.
- The docstring pass turned the module-level `def _par_mode():` (HEAD line 80) into an indented
  `@staticmethod def _par_mode():` that now sits *inside* `_import_support_module`, after its
  `return`, at `PluginBuilderExt.py:96-110`. Its body is unreachable.
- `_snapshot_loader_values` (`:1489`) and `_restore_loader_values` (`:1515`) call the bare name
  `_par_mode()`. Once B1 is fixed, **every hot reload raises `NameError` at step 0**, which means no
  parameter persistence across reloads, the headline 2.1.1–2.1.3 feature.
- Also: a duplicate `samples = self.TDSamplesDir` at `:509-510` (benign).
- Everything else in the Ext diff is docstring-only (AST-verified).

**B3 — `sync_custom_parameters` has a `for … else` that should be an `if … else`.** Verified in HEAD
since `5a88fc3`.

```python
for (tname, …) in entries:
    …
    if existing is None:
        existing = self._create_mirror_par(…)
        if existing is None:
            continue
else:                                   # ← aligned with `for`, not with `if`
    try: existing.label = label …
self._bind_components(comps, existing, …)   # ← runs ONCE, after the loop, for the last entry
```

On the rebuild path (a first sync, or any signature change) only the last parameter gets its BIND
and label. The others are created unbound (UI changes do not reach the loader) until a later
fast-path sync re-binds them. Existing parameters never get their labels refreshed.
(`PluginBuilderExt.py:1271-1289`)

**B4 — The CHANGELOG claims what did not happen.** `[Unreleased]` says the comment pass made "no logic
changes". B1–B2 are logic changes, and a tree that does not compile. `AUDIT.md` says `dev/ci.py --all`
passes; it can, because nothing in CI compiles or imports the Ext.

### 2.2 🟠 Real-time and dev-loop cost on TouchDesigner's main thread

The builder runs *inside* the show file, so any main-thread stall is a dropped frame for everything
else in the project.

**R-B1 — Every hot reload hashes all runtime DLLs (measured 0.86 s for 550 MB).**
- `_runtime_dlls_in_bin()` returns every `*.dll` in `build/bin/<Config>/` except the plugin
  (`PluginBuilderExt.py:1418-1426`).
- For Plugin_FFT that is `libfftw3f` plus **17 oneMKL DLLs, 550 MB**. They are present in
  `build/bin/Release` today.
- `_do_copy_plugin` runs `safe_replace_file` on each (`:1751-1754`), and that SHA-256s the source
  *and* the destination before deciding "unchanged" (`PluginBuilderCore.py:573-576`).
- Measured with the real Core function on the real files: **0.86 s** warm cache. That is roughly 52
  dropped frames at 60 fps per rebuild, and worse with a cold cache.
- `install_plugin` walks `__Plugins__/<name>` the same way *and copies* (`:1024-1037`).

*Fix, in order of payoff:*
- Fast path on `(size, mtime_ns)` equality before hashing.
- Remember hashes in a per-project `deploy_manifest.json`.
- Run hashing and copying as a BuildRunner job (off-thread), leaving only the loader reload on the
  main thread.
- Have `td_plugin_add_runtime_dll` record the intended runtime set, so stray DLLs in `bin/` are not
  swept up.

**R-B2 — Build output is printed line by line on the main thread.**
- `_poll()` drains up to 10 000 events per frame and does `print(line)` for each (`:1877-1880`,
  `PluginBuilderCore.py:1078-1093`).
- A full configure plus build emits hundreds of lines. Textport printing is not free in TD (inferred:
  several frames of hitching during a build).

*Fix:* join a frame's lines into one `print`, cap it (e.g. 200 lines per frame, remainder summarised),
and print the full log only in `LogLevel=debug`. The `build_errors` DAT is already the structured
channel.

**R-B3 — `viewer = True` on the loader is load-bearing, but it is also a per-frame viewer render**
(`:708`, `:1591`, `:1608`).
- For a 16 384-sample CHOP the network-editor viewer redraws every frame.
- It was chosen as the only kick-start for `cplusplusCHOP` (cookOnStart does not apply). That is
  correct as a kick-start; whether it must stay *on* after the first cook is **not established**.
  `cookEveryFrame=true` in the plugin may be enough once it has cooked.

*Experiment (S):* set the viewer, cook once, clear it, and watch `execute_count` plus the Performance
Monitor. Keep the flag only if cooking stops.

**R-B4 — The dev-loader architecture is not a production architecture.**
- In a show, the plugin runs inside the PluginBuilder COMP: mirrored parameters with BIND
  expressions, parameter-execute callbacks forwarding every change (`OnParValueChange` also writes
  `loader_par.val` directly, *on top of* the bind, `:1317-1326`), a `run()` poll loop, and folder
  DATs watching the source tree.
- None of that is needed at run time.

*Add a **"Bake for performance"** pulse:* export a plain `cplusplusCHOP` / Custom-OP instance with
constant parameter values (binds resolved), no callbacks and no folder watchers. Alternatively,
generate a `.tox` next to `__Plugins__/<name>`.

**R-B5 — The runner poll never stops.** While the runner is alive, `_poll` re-arms every 15 frames
forever (`:1890-1892`). It is small, but it is Python on the main thread in a show. Stop it when idle
and restart it on `submit`.

### 2.3 🟠 SDK and toolchain drift

**S1 — Common API 3 is out; `include/` is Common 2.** Verified.
- The installed 33230 samples have `OP_CommonAPIVersion = 3` and include `BitMaskFlags.h`.
- `setParameterEnableStates(const OP_Inputs*, OP_ParEnableState*, void*)` replaces
  `reservedFunc9`. The SDK says it "supersedes the usage of `inputs->enablePar()`" and "may be called
  anytime, not just during cooking", which is exactly what real-time plugins want (no `enablePar` in
  `execute`).
- Plugins built on Common 2 still load today (FFT runs), but new API is unreachable, and the
  extension's `_check_sdk_versions` should be warning on every init (Common 2 against 3).

**S2 — `dev/sync_templates.py` cannot perform that sync.**
- The default `--td` path does not exist (`:39`).
- `HEADER_FILES` has no entry for `BitMaskFlags.h` (`:81-88`), so the synced `CPlusPlus_Common.h`
  would `#include` a file that is not in `include/`.
- It does not record the TD build number.
- `sdk_versions.json` stores a path that never existed in that form.

**S3 — `TDPlugin.cmake` hard-codes the unversioned TD path** as the `TD_SAMPLES_DIR` fallback
(`cmake/TDPlugin.cmake:71-73`).
- Inside TD the extension passes `-DTD_SAMPLES_DIR=app.samplesFolder`, so builds from the COMP are
  fine.
- Standalone builds (Plugin_FFT's `_c.bat`, VS "Open Folder", CI) silently lose the samples'
  `3rdParty` fallback.

*Fix:* glob `C:/Program Files/Derivative/TouchDesigner*` and pick the highest build; allow an env or
`settings.ini` override.

**S4 — The vendored 3rdParty is stale against the installed TD.**
- `3rdParty/Python` is 3.11.1; TD ships 3.11.15.
- `3rdParty/opencv` is 4.8.0 (`*480.lib`); TD ships **4.11.0** (`*4110.lib` + `opencv_*4110.dll`).
- `_td_find_3rdparty` prefers the PluginBuilder copy over TD's (`TDPlugin.cmake:125-129`), and
  `td_plugin_use_opencv` links **all 63** libs (`:592-595`).
- A plugin using `FEATURES opencv` would link 4.8 import libraries against a TD that ships only 4.11
  DLLs. The inference is that it would fail to load; this was not built.
- There are no PSF or Apache license texts for either.

**S5 — CMake-module details** (all verified):
- `td_plugin_add_test` calls `enable_testing()` inside a function (`:659-663`), which has no effect at
  the top level. Plugin_FFT had to work around it (`Plugin_FFT/.../CMakeLists.txt:36`). *Fix:* call
  `enable_testing()` at module scope (an `include()` runs in the caller's scope).
- `td_plugin_use_fftw3` **writes into the source tree at configure time.** It rewrites the `.def` and
  regenerates the `.lib` with `lib.exe` (`:428-459`), which is why Plugin_FFT's
  `libfftw3f-3.3.11-avx2.lib` is permanently "modified" (timestamp bytes only). *Fix:* generate into
  `${CMAKE_BINARY_DIR}/td_deps/` and link from there.
- **Release has no PDB** (`/Zi` only for Debug/RelWithDebInfo, `:208`), and RelWithDebInfo does not
  get `/GL` (`:271`). There is no configuration that is "the shipped binary plus symbols", which is
  what VTune, WPA or Superluminal need. *Fix:* add `/Zi` + `/DEBUG /OPT:REF /OPT:ICF` to Release, or
  a `Profile` config equal to Release plus a PDB.
- `/MP` is a no-op under Ninja. The C++ standard is pinned at 17; C++20 would allow `std::span`,
  `std::atomic::wait/notify` and designated initialisers for parameter structs. Make it opt-in.
- The CUDA arch list includes `120` (Blackwell), which needs CUDA ≥ 12.8. With an older 12.x the
  configure fails. Filter by toolkit version.
- `td_plugin_use_python` takes `list(GET … 0)` of an alphabetical glob. It works today
  (`python311.lib` sorts before `python311_d.lib`), but the comment says "newest". Make it explicit.

### 2.4 🟡 Templates (the second reviewer's findings; faithful copies of Derivative's samples)

The code is identical to the 33230 samples except for the `opIcon` placeholder, and BasicCHOP lacks
the new `setParameterEnableStates` example. So these are **Derivative's** issues, inherited by every
generated plugin.

| Template | Real-time or correctness issues a user inherits |
|---|---|
| All | `getPar*` by name every cook; `enablePar` inside `execute`; Info DAT uses `sprintf` into 4 KB stack buffers per call; comments say "cook every frame" where the code sets `false` |
| BasicCHOP | `% numSamples` per sample (divides by zero with a 0-sample input); `Shape` never re-enabled after input disconnect |
| BasicDAT | `setTableSize` per channel (O(n) resizes); function-static `tempBuffer` shared across instances; writes column 1 when `Cols=1` |
| CHOPWithPythonClass | Builds a tuple, takes the GIL and calls into Python **every cook**; leaks on `PyErr_Occurred` |
| CPUMemoryTOP / FrameQueue | 1 MiB RGBA32F allocate+fill per cook on the main thread; `myStep` data race; producer paced by a fixed `Sleep` (assumes 60 fps); `createOutputBuffer` under the queue lock; oldest-frame (not latest) consumption adds a frame of latency; no buffer reuse |
| CudaTOP | `cudaDeviceSynchronize` in Debug; launch errors unchecked in Release; always renders a 1280×720 aux output; blocking stream |
| CudaPOP | No `outputBufferCandidate` (likely a copy per attribute per cook); per-cook vectors and strings; `beginCUDAOperations` result unchecked |
| SimpleShapesPOP | **Index buffers created with the attribute create-info** (`:344,486,777,914`); GPU→CPU readback stall every cook with an input |
| SimpleShapesSOP | **Out-of-bounds texcoord read/write** with GPU Direct (`:608-610` against a 24-element array); `addTriangle` on non-triangles; per-point virtual calls instead of bulk setters; uninitialised custom attributes |

**The uncommitted template comments (+393 lines) should not be committed as they are.**
- They are wiped by the next `sync_templates.py` (it `rmtree`s `templates/<name>/source`).
- They contain the `#__OP_*__#` tokens, so `test_templates_have_all_placeholders` (a substring check,
  `tests/test_core.py:116-124`) now passes even if a real token disappears, and `render_source`
  garbles the comment prose in generated plugins.
- They copy process notes ("Only comments were added here…") into every user's plugin.
- Several are factually wrong, e.g. "resamples" and "120 Hz waveform" in BasicCHOP, and "no CUDA work
  here" in constructors that call `cudaStreamCreate`.

### 2.5 🟡 Testing and CI gaps

- **The Ext has zero automated coverage**, not even a compile. That is how B1–B3 exist.
- `ci.py` builds BasicCHOP (or all non-CUDA templates with `--all`), but never compiles or imports the
  Ext, and never builds a CUDA or POP template.
- There is no test of `_do_copy_plugin` / `_reload_loader_fresh` ordering, of the mirror rebuild path
  (B3), or of `safe_replace_file`'s `.oldN` fallback when `.old` is locked. Note that `keep_old=False`
  only removes `dst + '.old'`, not the `.oldN` it may have used (`PluginBuilderCore.py:579-603`).
- Line counts claimed in docs ("29 tests") are maintained by hand in four places: README, AUDIT,
  Core docstring, ci.py docstring.

### 2.6 🟡 Repository hygiene

- Tracked duplicates:
  - `SetSettings.6.toe` is byte-identical to `SetSettings.toe`.
  - `dev/dev.161.toe` is byte-identical to `dev/dev.toe`.
  - `dev/dev.160.toe` is an older variant.
  - The ignore rule `*.[0-9]*.toe` cannot untrack them; that needs `git rm --cached`.
- `.gitattributes` is only `* text=auto`. Mark `*.toe *.tox *.lib *.dll` as `binary`.
- `source/__pycache__` holds cpython-39/311/314 caches (ignored, but present on disk).
- `source/CMakePresets.json`: all presets share one `binaryDir`, and there are no build presets.
  `source/launch.vs.json` has three configurations with the same name (shipped into every generated
  project).
- License layering is undocumented: MIT (root), Derivative Shared Use (`templates/`, `include/`), PSF
  (Python), Apache-2.0 (OpenCV). Add a `THIRD_PARTY.md`.

---

## 3. What is good (keep it)

- **The Core/Ext split with a headless test suite.** All the pure logic (naming, settings, toolchain
  discovery, manifests, diagnostics, runner, hot-swap, persistence classification) is testable
  without TD.
- **BuildRunner:** a background thread, a captured vcvars environment once per session, a Windows Job
  Object (no orphaned `cl.exe`), exit codes plus parsed diagnostics, and a coalesced build queue.
- **The rename-in-place hot swap, hash-gated**, with rollback to the previous DLL on a failed map,
  and a fresh-node reload so changed parameter *definitions* appear without restarting TD.
- **A shared CMake module:** generated projects are 15 lines, so fixes reach every project on the next
  configure. The `td_plugin_use_fftw3 VERSION … DYNAMIC` contract is careful work (the import-lib
  module-name check, `FFTW_DLL`, DYNAMIC for runtime backends). Headless test and bench targets that
  inherit LTO flags are the right idea.
- **Honest documentation of corrected history** (`AUDIT.md` §5).

---

## 4. The plan

Acceptance criteria are checks or numbers. Effort: S ≤ ½ day · M ≤ 2 days · L > 2 days.

### Phase 0 — Repair the tree and the truth (≈ 1 day, first)

| # | Item | Effort | Accept when |
|---|---|---|---|
| 0.1 | **B1+B2:** restore `def _par_mode()` at module scope (as in HEAD); remove the stray line in `OnParPulse`; drop the duplicate `samples =` | S | `py_compile` of every `source/*.py` passes; AST diff against HEAD shows docstring-only changes |
| 0.2 | **B3:** re-indent the `else` to the `if`, and move `_bind_components` into the loop body | S | New test (see 0.4): a rebuild with 3 parameters leaves 3 bound parameters |
| 0.3 | **Template comments:** do not commit them as they are. Strip `#__OP_*__#` tokens and process notes from comments, fix the factual errors, and move the explanations into `templates/<name>/NOTES.md` (survives the sync). Tighten the placeholder test to match `setString("#__OP_…__#")` | S | `sync_templates.py --dry-run` against HEAD shows no template-source diff; the test fails if a real token is removed |
| 0.4 | **Ext smoke tests in CI:** (a) `py_compile` every `.py`; (b) a `tests/test_ext.py` with a minimal fake `td` module (op/run/project/ParMode/cplusplusCHOP stubs) that imports `PluginBuilderExt` and drives `sync_custom_parameters` (rebuild + fast path) and `_snapshot_loader_values` / `_restore_loader_values` on fake parameters | M | B1, B2 and B3 each make CI red when reintroduced |
| 0.5 | Fix the CHANGELOG `[Unreleased]` statement; single-source the test count (README and AUDIT say "see `ci.py` output") | S | No hand-maintained count remains |
| 0.6 | Re-export `PluginBuilder.tox` via `dev/dev.toe` + `deploy.py` after 0.1–0.2; record in the CHANGELOG which DATs are file-linked | S | `git log -1 -- PluginBuilder.tox` is after 0.1; a fresh drag-in in TD 33230 creates, builds and hot-reloads BasicCHOP |
| 0.7 | Hygiene: `git rm --cached` the numbered `.toe` duplicates; `.gitattributes` binary rules; unique `launch.vs.json` names | S | `git ls-files '*.[0-9]*.toe'` is empty |

### Phase 1 — Off the main thread, and SDK current (≈ 3–4 days)

| # | Item | Effort | Accept when |
|---|---|---|---|
| 1.1 | **R-B1:** `(size, mtime_ns)` fast path before hashing; per-project `deploy_manifest.json` of hashes; a runtime-DLL allow-list from CMake (`td_plugin_add_runtime_dll` writes `build/td_runtime_dlls.txt`) instead of "every DLL in `bin/`" | M | Plugin_FFT hot reload with the MKL DLLs present: main-thread file work < 5 ms (today 860 ms), measured with a `time.perf_counter` log line |
| 1.2 | Move DLL staging (hash + copy) into a BuildRunner job; the main thread only runs `_reload_loader_fresh` in `on_done` | M | No main-thread file I/O on reload other than the loader cook |
| 1.3 | **`install_plugin` off-thread** with a progress status; skip files by `(size, mtime)` | S | Installing FFT with MKL does not freeze TD |
| 1.4 | **R-B2:** batched textport output (one `print` per frame, ≤ 200 lines, rest summarised; full log in `build/last_build.log`) | S | A full rebuild of Plugin_FFT causes no frame over 20 ms in the Performance Monitor (today unmeasured; take a baseline first) |
| 1.5 | **R-B5:** stop the idle poll; re-arm on `submit` | S | No `run()` activity while idle |
| 1.6 | **S1–S3: SDK resync to 33230.** `sync_templates.py`: discover the newest `TouchDesigner*` install; add `BitMaskFlags.h`; write `{td_build, source, versions}` into `sdk_versions.json`; `TDPlugin.cmake` globs versioned installs for `TD_SAMPLES_DIR` | M | `include/` Common 3; `_check_sdk_versions` silent in 33230; `ci.py --all` + `--project ../Plugin_FFT/...` green; Plugin_FFT loads in TD 33230 |
| 1.7 | **S5:** `enable_testing()` at module scope (then remove FFT's workaround); FFTW `.def`/`.lib` generated in the binary dir; a `Profile` build type (Release + PDB) | S | Plugin_FFT tree stays clean after configure; `ctest` works without the workaround; the Profile PDB loads in VTune/WPA |
| 1.8 | **R-B3 experiment:** can the loader viewer be cleared after the first cook? | S | A documented answer, with `execute_count` evidence |

### Phase 2 — Make the builder produce real-time-first plugins (≈ 1–2 weeks)

This is where PluginBuilder can become more than a build button. The FFT project already contains the
patterns; lift them into the builder so every new plugin starts there.

| # | Item | Effort | Why / accept when |
|---|---|---|---|
| 2.1 | **`RealtimeCHOP` template** (not a sample copy): a POD parameter snapshot read once in `getOutputInfo`; `setParameterEnableStates` (Common 3) instead of `enablePar`; no allocation in `execute`; an optional worker thread with latest-wins triple buffers both ways; FTZ/DAZ guard; cached Info CHOP/DAT strings (format only on change); `cook_time_us` / `dsp_time_us` / `hold_frames` Info channels; a TD-free `Core.h` + `tests/` + `bench/` wired through `td_plugin_add_test/bench` | L | A generated plugin passes an allocation gate (0 allocations over 1000 cooks) and ships with a bench whose `--cook` p99 is reported |
| 2.2 | **Parameter generator:** `parameters.json` → `Parameters.h/.cpp` (the Plugin_FFT layout: constexpr names, enum-class menus with `static_assert`ed tables, one `eval()` into a `Values` struct, `setParameterEnableStates` from `enable_if` rules). Closes AUDIT §4 "parameter round-trip" | L | FFT's `Parameters.h/.cpp` can be regenerated from JSON with a clean diff (or a documented one) |
| 2.3 | **Fix and harden the sample templates** (keep them as "official samples", but correct): the SOP texcoord OOB, the POP index-buffer create-info, the CPUMemoryTOP `myStep` race + latest-frame queue + no alloc under lock + buffer reuse, CUDA error checks + non-blocking stream, BasicCHOP divide-by-zero. Keep the patches as a `templates/patches/*.diff` applied by `sync_templates.py`, so fixes survive resyncs | M | `ci.py --all` builds them; the second reviewer's list is closed item by item |
| 2.4 | **"Bake for performance" (R-B4):** a pulse that emits a production node/tox (constant parameters, no binds, no callbacks, no folder watchers), plus optional `Install` into Documents/Derivative/Plugins as a Custom OP (where `cookOnStart` works) | M | A show `.toe` containing the baked node has no PluginBuilder Python running per frame (Performance Monitor) |
| 2.5 | **Perf scaffolding in `TDPlugin.cmake`:** `td_plugin_add_bench(... GATE baseline.json)` → a ctest `perf` label that fails on regression; the `Profile` config (1.7) used by default for benches | M | Plugin_FFT's `fft_bench --gate` (FFT plan 1.6) runs through this, not ad hoc |
| 2.6 | **`td_plugin_optimize` presets:** `REALTIME` = `/O2 /Oi /Ot /Gy /arch:AVX2 /fp:fast /GL` + a generated runtime CPU check (`cpuSupportsAVX2`, lifted from FFT's `DSPModules.h` into `cmake/td_runtime/cpu_guard.h`) so AVX2 plugins can never fault on an old CPU | S | A template compiled with `REALTIME` refuses to process (with an error string) on a non-AVX2 VM instead of crashing TD |

### Phase 3 — CI, distribution, cross-platform (as needed)

| # | Item | Effort |
|---|---|---|
| 3.1 | GitHub Actions (windows-latest): `unittest` + Ext smoke + `ci.py --all` + build Plugin_FFT (checkout the sibling repo) + `fft_tests` (non-perf labels) | M |
| 3.2 | Slim `3rdParty/`: default to TD's own `Samples/CPlusPlus/3rdParty`; if vendored, pin to TD's versions (OpenCV 4.11, Python 3.11.x), link only requested components, and add the license texts | M |
| 3.3 | macOS: port the sample CMake bundle rules into `TDPlugin.cmake` (AUDIT §4 open item) | L |
| 3.4 | Template gallery metadata (`template.json`: `realtime: true`, `threads: worker`, `gpu: cuda`) shown in the COMP menu | S |

---

## 5. Cross-repo contract items that Plugin_FFT's plan depends on

| Needed by Plugin_FFT | Delivered by V2 item |
|---|---|
| Configure must not dirty `3rdParty/fftw3/lib` | 1.7 |
| `ctest` without the local `enable_testing()` workaround | 1.7 |
| A `Profile` build (Release + PDB) for VTune/WPA/Superluminal on the real binary | 1.7 |
| A perf-gate hook (`td_plugin_add_bench ... GATE`) | 2.5 |
| Common API 3 / `setParameterEnableStates` (grey out the EQ / dB / ballistics sub-parameters without touching `execute`) | 1.6 |
| Hot reload must not stall TD for ~1 s when the MKL runtime sits in `bin/` | 1.1–1.2 |
| Hot reload must keep parameter values (broken by B2 in the working tree) | 0.1 |

---

## Appendix — Evidence log (2026-09-23)

- `py_compile source/PluginBuilderExt.py` gives: `IndentationError: unindent does not match any outer indentation level (line 1330)`.
- AST diff HEAD → working tree, with the `OnParPulse` stray line removed so it parses. The only code
  deltas are:
  - `_par_mode` moved into the dead tail of `_import_support_module`;
  - a duplicated `samples = self.TDSamplesDir`.

  Core: `CREATE_NO_WINDOW` moved earlier (benign). CMakeBlocks, ci, deploy, sync_templates and
  test_core: identical modulo docstrings.
- `python -m unittest discover -s tests`: `Ran 29 tests … OK`.
- Hashing, using the real `PluginBuilderCore.file_sha256` over `Plugin_FFT/PluginProjects/FFT/build/bin/Release/*.dll`
  (18 files, 550 MB, src + dst as `safe_replace_file` does): **0.86 s**.
- Installed TouchDesigner: `C:\Program Files\Derivative\TouchDesigner.2025.33230\` (and
  `TouchDesigner.2022.32120`). Samples: `OP_CommonAPIVersion = 3`, `BitMaskFlags.h` present;
  `CHOP_CPlusPlusBase.h` differs from `include/` by `setParameterEnableStates` replacing `reservedFunc9`.
- `git log -1 -- PluginBuilder.tox dev/dev.toe` gives `65a56b2 2024-07-08`.
