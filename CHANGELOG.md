# Changelog — PluginBuilder_V2

All notable changes to the `PluginBuilder_V2` framework are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-07-24

### 🚀 Major Highlights

- **Universal Dynamic Parameter Mirroring Engine**: Intelligently inspects and reflects custom parameters from dynamic C++ operator DLLs directly onto the parent COMP's **'Custom'** parameter page tab with automatic binding and vector support.
- **Subprocess & Extension Lifecycle Hardening**: Replaces destructive parent COMP recursive cooking with target operator re-initialization, eliminating extension instance destruction and maintaining persistent background build toolchains.
- **Zero-Latency MSVC Compilation Environment**: Manages a persistent `cmd.exe` background process pre-loaded with `vcvarsall.bat x64` and Ninja, avoiding compiler setup overhead on every build.
- **Repomix Architectural Reference ([AUDIT.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/AUDIT.md))**: Complete architectural audit report detailing repository topology, component mapping, and file specifications.

---

### 🌟 Added

#### Core Engine & Extension (`source/PluginBuilderExt.py`)
- **Universal Parameter Binding Engine (`_sync_single_parameter`)**:
  - Dynamically detects and mirrors C++ plugin parameters (`Float`, `Int`, `Toggle`, `Menu`, `Str`, `Pulse`, `Header`) onto TouchDesigner COMP parameter pages.
  - Full vector parameter dimension support for `XYZ`, `UV`, `RGB`, and `RGBA` tuples.
  - Dynamic `ParMode.BIND` expression creation for two-way parameter state synchronization.
  - String-set parameter name deduplication preventing single-float numeric parameter dropping caused by `td.Par.__eq__` evaluation edge cases.
- **Pulse Parameter Dispatcher (`OnParPulse`)**:
  - Event routing mechanism for non-bindable pulse parameters (e.g. `Reset`).
  - Automatically triggers `pulsePressed()` hooks in the underlying C++ DLL instance.
- **Subprocess Recovery & Guard System**:
  - Automatic process state inspection (`poll()`) inside `SendCommand()` with auto-restart fallback when the build process terminates unexpectedly.
  - Clean resource cleanup in `close_subprocess()` terminating background tasks and joining daemon stdout reader threads to prevent zombie process file locks on DLLs.

#### Telemetry & Telemetry Deduplication
- Tagged logging framework (`_log`) categorization (`[Init]`, `[Create]`, `[Build]`, `[Compile]`, `[Install]`, `[ParamSync]`, `[Subprocess]`).
- State-change log suppression algorithm for parameter synchronization, suppressing redundant textport messages unless active parameter sets change.

#### Documentation & Environment Configuration
- **[AUDIT.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/AUDIT.md)**: Full Repomix architectural documentation outlining system diagrams, directory layouts, and file responsibilities.
- **[CHANGELOG.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/CHANGELOG.md)**: Standardized versioning history.
- **`SetSettings.toe` / `SetSettings.6.toe`**: TouchDesigner setting files pre-configured for local development path setup.

---

### 🔧 Changed

- **Plugin Reload Pipeline (`_do_copy_plugin`)**:
  - Replaced parent COMP recursive cooking (`ownerComp.cook(recurse=True)`) with targeted loader operator re-initialization (`reinitpulse.pulse()` and `loader_op.cook(force=True)`).
  - Preserves active Python extension instances and prevents active background build processes from being killed during hot-reloading.
- **Repository Exclusions ([.gitignore](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/.gitignore))**: Added `__pycache__/` and `*.pyc` rules to prevent tracking Python bytecode cache files.

---

### 🐛 Fixed

- **Eliminated Duplicate `[ParamSync]` Telemetry Log Output**: Removed premature `sync_custom_parameters()` calls in `compile_plugin()` and `build_plugin()`, guaranteeing parameter sync fires strictly once after the C++ DLL is copied and reloaded.

---

### 🛡️ Technical Specifications & Compatibility Matrix

| Feature | TouchDesigner Compatibility | Toolchain | Operating System |
|---|---|---|---|
| **C++ Operator Hot-Reload** | TouchDesigner 2023+ | MSVC x64 / Ninja / CMake 3.21+ | Windows 10/11 x64 |
| **Dynamic Parameter Sync** | TouchDesigner 2023+ (td.Par / ParMode.BIND) | Python 3.11 Embedded | Windows 10/11 x64 |
