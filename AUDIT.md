# Repomix Architecture & Audit Report — PluginBuilder_V2

> **Repository:** PluginBuilder_V2  
> **Target System:** TouchDesigner 2023+ / Windows x64 / MSVC Ninja CMake  
> **Report Style:** Architectural Audit, Comprehensive Codebase Review & Improvement Plan  

---

## 1. Directory Structure & File Inventory

```text
PluginBuilder_V2/
├── 3rdParty/
│   ├── OpenCV/                       # Bundled OpenCV 4.8 headers & x64 import libraries
│   └── Python/                       # Bundled Python 3.11 C API headers & x64 import libraries
├── dev/
│   ├── deploy.py                     # Deployment and release packaging script
│   ├── dev.toe                       # Development test bed TOE file
│   ├── dev.160.toe / dev.161.toe     # Versioned backup development test beds
│   └── settings_template.ini         # Template file for toolchain environment paths
├── include/                          # TouchDesigner C++ SDK interface headers
│   ├── CHOP_CPlusPlusBase.h
│   ├── CPlusPlus_Common.h
│   ├── DAT_CPlusPlusBase.h
│   ├── SOP_CPlusPlusBase.h
│   └── TOP_CPlusPlusBase.h
├── source/                           # Core Extension logic & CMake generation
│   ├── CMakeBlocks.py                # Modular CMake template definitions
│   ├── CMakePresets.json             # Visual Studio / Ninja build presets
│   ├── launch.vs.json                # MSVC Debugger launch configuration
│   └── PluginBuilderExt.py           # Master TouchDesigner Extension orchestrator (Python)
├── templates/                        # C++ Operator Templates (CHOP, TOP, DAT, SOP)
│   ├── BasicCHOP/                    # Standard CHOP operator template (contains source/ & unused CMakelists.txt)
│   ├── BasicDAT/                     # Standard DAT operator template
│   ├── CHOPWithPythonClass/          # CHOP template exposing custom Python bindings
│   ├── CPUMemoryTOP/                 # CPU-side texture memory TOP template (includes FrameQueue.h/.cpp)
│   ├── CudaTOP/                      # GPU CUDA kernel TOP operator template (includes kernel.cu)
│   └── SimpleShapesSOP/              # 3D geometry generator SOP template
├── .gitattributes
├── .gitignore
├── AUDIT.md                          # Master Repomix Architecture & Audit Report
├── CHANGELOG.md                      # Versioning history log
├── LICENSE                           # MIT License
├── PluginBuilder.tox                 # Packaged TouchDesigner COMP component
├── README.md                         # Project documentation & usage guide
├── SetSettings.6.toe                 # Settings TOE snapshot
└── SetSettings.toe                   # Active settings configuration TOE file
```

---

## 2. Component & File Summaries

### Root Level
- **[PluginBuilder.tox](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/PluginBuilder.tox)**: Pre-packaged TouchDesigner COMP containing the `PluginBuilderExt` extension, file watchers (`folder_bin`, `folder_source`), UI controls, and operator loader pipeline.
- **[SetSettings.toe](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/SetSettings.toe)**: Interactive TOE used to write user environment settings (`PluginBuilderDir`, `NinjaDir`, `VCVarsall`) to `%APPDATA%/IntentDev/PluginBuilder/settings.ini`.
- **[README.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/README.md)**: User setup guide, toolchain prerequisites (VS, CMake, Ninja, CUDA), template usage, and Visual Studio debugger attachment workflow.
- **[CHANGELOG.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/CHANGELOG.md)**: Release notes documenting dynamic parameter mirroring, subprocess lifecycle hardening, and telemetry log suppression.
- **[LICENSE](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/LICENSE)**: Standard MIT open-source license.
- **[.gitignore](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/.gitignore)**: Filters out temporary build artifacts, compiled DLLs, and Python bytecode caches (`__pycache__/`).

### Source (`source/`)
- **[PluginBuilderExt.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/PluginBuilderExt.py)**: Master Python extension orchestrator (`PluginBuilderExt`). Responsibilities:
  - Scaffolding new C++ plugin projects from template directories.
  - Maintaining a persistent `cmd.exe` background subprocess pre-initialized with MSVC `vcvarsall.bat x64` and Ninja.
  - Executing asynchronous CMake configure (`cmake -B build -G Ninja`) and compilation (`ninja -C build`).
  - Hot-reloading built DLLs into TouchDesigner `cplusplus` operators with file lock retries and debounced copy triggers.
  - Dynamic parameter mirroring onto COMP 'Custom' parameter page tab with vector binding support.
- **[CMakeBlocks.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/CMakeBlocks.py)**: Reusable Python string blocks assembled by `PluginBuilderExt` to generate `CMakeLists.txt` files for different operator types (Basic C++, CUDA, Python, OpenCV).
- **[CMakePresets.json](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/CMakePresets.json)**: Standard CMake preset definitions for Ninja/MSVC builds (`windows-base`, `debug`, `release`, `relwithdebuginfo`).
- **[launch.vs.json](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/launch.vs.json)**: Configuration for attaching Visual Studio C++ debugger directly to TouchDesigner (`touchdesigner.exe`).

### Templates (`templates/`)
- **`BasicCHOP/`**: C++ CHOP template demonstrating custom channels, numeric parameters, and time slice processing. *Note: Contains an obsolete `CMakelists.txt` file.*
- **`BasicDAT/`**: C++ DAT template for string/table data generation and manipulation.
- **`CHOPWithPythonClass/`**: Advanced CHOP template showing how to expose C++ methods directly to Python inside TouchDesigner using Python C API.
- **`CPUMemoryTOP/`**: CPU texture processing TOP template supporting RGBA memory buffers, frame synchronization, and thread-safe frame queueing (`FrameQueue.h/.cpp`).
- **`CudaTOP/`**: CUDA GPU kernel TOP template for parallel compute shaders (`kernel.cu`).
- **`SimpleShapesSOP/`**: C++ 3D geometry generator template demonstrating mesh, vertex, and normal generation.

### SDK Headers (`include/`)
- Abstract base classes and data definitions provided by Derivative for custom C++ operators: `CHOP_CPlusPlusBase.h`, `TOP_CPlusPlusBase.h`, `DAT_CPlusPlusBase.h`, `SOP_CPlusPlusBase.h`, `CPlusPlus_Common.h`.

### Bundled Toolchains & Libraries (`3rdParty/`)
- **`Python/`**: Python 3.11 C API headers and x64 import libraries for plugins interacting with embedded Python.
- **`OpenCV/`**: OpenCV 4.8 computer vision headers and import libraries for vision-based TOP/DAT operators.

---

## 3. Detailed Technical Findings & Code Audit

1. **Multi-Vector Tuple Parameter Binding Bug (`_sync_single_parameter`)**:
   - In `PluginBuilderExt.py` (lines 835–864), when appending multi-tuple parameters (`appendFloat`, `appendInt`, `appendXYZ`, `appendUV`, `appendRGB`, `appendRGBA`) with `size > 1`, TouchDesigner returns a tuple of parameter objects (e.g. `(par0, par1, par2)`).
   - The current code extracts index `[0]` (`new_p = page.appendXYZ(par_name, label=label)[0]`).
   - Consequently, `_copy_par_attributes` and `bindExpr` are applied **only to the first component** (`par0`), leaving subsequent vector components (`par1`, `par2`) unbound and without default/min/max values.

2. **Unsafe Header Parsing via `eval()` (`onPluginname`)**:
   - In `onPluginname` (line 1048), `eval(first_line[2:])` is used to parse the header comment in `CMakeLists.txt` (e.g., `# {'plugin_type': 'CHOP'}`).
   - Using Python's `eval()` on raw file contents poses potential code execution risks and breaks if the comment contains unquoted strings or formatting variations. Safe parsing with `ast.literal_eval`, `json`, or regex is strongly recommended.

3. **Menu Parameter Synchronization Edge Case (`_copy_par_attributes`)**:
   - When updating `menuNames` and `menuLabels` on dynamic `StrMenu`/`IntMenu` parameters, setting `dst_p.menuNames` before `dst_p.menuLabels` can fail or raise size-mismatch errors if the new menu length differs from defaults. Assigning both atomically or setting labels first prevents runtime exceptions.

4. **File Lock Exception Handling (`file_locked`)**:
   - `file_locked()` (lines 754–761) catches `PermissionError`. On Windows, locked files during Ninja compilation or background antivirus scanning may throw generic `OSError` or `IOError` (e.g. `[WinError 32]`). Expanding exception handling to `(PermissionError, OSError)` improves lock detection resilience.

5. **Redundant Template File in `templates/BasicCHOP/CMakelists.txt`**:
   - `templates/BasicCHOP/CMakelists.txt` is an un-templated leftover file. `PluginBuilderExt.py` generates `CMakeLists.txt` dynamically using `CMakeBlocks.py`. Having this file in `templates/BasicCHOP` can cause confusion during manual inspection.

6. **CMake Source File Globbing (`source/CMakeBlocks.py`)**:
   - `CMakeBlocks.py` uses `file(GLOB_RECURSE PROJ_SOURCE_FILES ...)` to collect source files. While functional for small projects, adding a new C++ header/source file does not automatically trigger CMake re-configure unless CMakeLists.txt is modified. Adding `CONFIGURE_DEPENDS` or explicit change notifications guarantees seamless new file pick-up.

---

## 5. Pass 2 Audit Findings: Template Inconsistencies & Refactoring Opportunities

1. **Template Discrepancy in [templates/BasicDAT/source/BasicDAT.cpp](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/templates/BasicDAT/source/BasicDAT.cpp)**:
   - `FillDATPluginInfo()` in `BasicDAT.cpp` contained hardcoded strings (`"Customdat"`, `"Custom DAT"`, `"CDT"`, `"Author Name"`, `"email@email.com"`) instead of template substitution placeholders (`#__OP_TYPE__#`, `#__OP_LABEL__#`, `#__OP_ICON__#`, `#__OP_AUTHOR__#`, `#__OP_EMAIL__#`).
   - Fixing `BasicDAT.cpp` ensures metadata replacement works identically across all operator templates (CHOP, TOP, DAT, SOP).

2. **Parameter Filtering Robustness in [source/PluginBuilderExt.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/PluginBuilderExt.py)**:
   - In `sync_custom_parameters()`, custom parameter filtering relied exclusively on `p.name[0].isupper()`. Adding `getattr(p, 'isCustom', False)` ensures custom parameters are correctly recognized regardless of casing.

3. **Dead Code Cleanup in [source/PluginBuilderExt.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/PluginBuilderExt.py)**:
   - Line 500 contains a redundant self-assignment (`self.loader_op = self.loader_op`). Cleaning up dead statements improves codebase maintainability.

4. **Deployment Script Cleanup in [dev/deploy.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/dev/deploy.py)**:
   - Refactor redundant `op('PluginBuilder')` call to use the initialized `PluginBuilderComp` variable.

5. **Path Placeholder Alignment in [dev/settings_template.ini](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/dev/settings_template.ini)**:
   - Replace hardcoded `D:/TD/PluginBuilder` with `${USER_PATH}/PluginBuilder` placeholder for better portability across user environments.

6. **CUDA Runtime Reference Alignment in [source/CMakeBlocks.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/CMakeBlocks.py)**:
   - Update CUDA runtime comment reference from `cudart64_110.dll` to `cudart64_118.dll` matching TouchDesigner 2023+ CUDA 11.8 specification.

---

## 6. Comprehensive Pass 2 Improvement Plan

> [!NOTE]  
> **Status:** Pass 2 Plan created and audited. **No code changes have been executed yet.**

### Phase 1: Template Standardization & Metadata Substitution Fix
- Update `FillDATPluginInfo()` in `templates/BasicDAT/source/BasicDAT.cpp` to use standard metadata placeholders (`#__OP_TYPE__#`, `#__OP_LABEL__#`, `#__OP_ICON__#`, `#__OP_AUTHOR__#`, `#__OP_EMAIL__#`).

### Phase 2: Extension Logic & Custom Parameter Inspection
- Update `sync_custom_parameters()` in `source/PluginBuilderExt.py` to check `getattr(p, 'isCustom', False)` alongside casing filters.
- Remove dead code statement (`self.loader_op = self.loader_op`) in `create_plugin_loader()`.

### Phase 3: Developer Tooling & Settings Cleanup
- Refactor `dev/deploy.py` to use `PluginBuilderComp.EnableCreatePars()`.
- Update `dev/settings_template.ini` default path to `${USER_PATH}/PluginBuilder`.
- Update CUDA runtime comments in `source/CMakeBlocks.py`.

### Development (`dev/`)
- **`dev.toe`**: Interactive development sandbox file.
- **`deploy.py`**: Helper script to package and export `PluginBuilder.tox`.
- **`settings_template.ini`**: Initial configuration template for environment setup.
