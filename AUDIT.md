# Repomix Architecture Report — PluginBuilder_V2

> **Repository:** PluginBuilder_V2  
> **Target System:** TouchDesigner 2023+ / Windows x64 / MSVC Ninja CMake  
> **Report Style:** Repomix Architectural Summary (No full code dumps, concise file descriptions)

---

## 1. Directory Structure

```text
PluginBuilder_V2/
├── 3rdParty/
│   ├── OpenCV/                       # Bundled OpenCV 4.8 headers & x64 import libraries
│   └── Python/                       # Bundled Python 3.11 C API headers & x64 import libraries
├── dev/
│   ├── deploy.py                     # Deployment and release packaging script
│   ├── dev.toe                        # Development test bed file for extension debugging
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
│   ├── BasicCHOP/                    # Standard CHOP operator template
│   ├── BasicDAT/                     # Standard DAT operator template
│   ├── CHOPWithPythonClass/          # CHOP template exposing custom Python bindings
│   ├── CPUMemoryTOP/                 # CPU-side texture memory TOP template
│   ├── CudaTOP/                      # GPU CUDA kernel TOP operator template
│   └── SimpleShapesSOP/              # 3D geometry generator SOP template
├── .gitattributes
├── .gitignore
├── AUDIT.md                          # Master Repomix Architecture Report
├── LICENSE                           # MIT License
├── PluginBuilder.tox                 # Packaged TouchDesigner component
├── README.md                         # Project documentation & usage guide
├── SetSettings.6.toe                 # Environment settings configuration TOE
└── SetSettings.toe                   # Active settings TOE file
```

---

## 2. Component & File Summaries

### Root Level
- **[PluginBuilder.tox](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/PluginBuilder.tox)**: Pre-packaged TouchDesigner COMP containing the `PluginBuilderExt` extension, file watchers, UI controls, and loader pipeline.
- **[SetSettings.toe](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/SetSettings.toe)**: Configures global paths for MSVC `vcvarsall.bat`, CMake, and Ninja binaries.
- **[README.md](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/README.md)**: User guide covering setup, template creation, hot-reloading, and workflow steps.
- **[LICENSE](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/LICENSE)**: Standard MIT open-source license.
- **[.gitignore](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/.gitignore)**: Filters out temporary build artifacts, compiled DLLs, and Python bytecode caches (`__pycache__/`).

### Source (`source/`)
- **[PluginBuilderExt.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/PluginBuilderExt.py)**: Master Python extension class (`PluginBuilderExt`). Manages:
  - Scaffolding new C++ plugin projects from template directories.
  - Persistent `cmd.exe` background subprocess pre-initialized with MSVC x64 build environment.
  - Asynchronous CMake configure (`cmake -B build -G Ninja`) and compilation (`ninja -C build`).
  - Hot-reloading built DLLs into TouchDesigner `cplusplus` operators.
  - Dynamic parameter mirroring onto COMP 'Custom' parameter tab with vector binding support.
- **[CMakeBlocks.py](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/CMakeBlocks.py)**: Reusable Python string blocks assembled by `PluginBuilderExt` to generate CMakeLists.txt files for different operator types (C++, CUDA, Python, OpenCV).
- **[CMakePresets.json](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/CMakePresets.json)**: Standard CMake preset definitions for Ninja/MSVC builds.
- **[launch.vs.json](file:///c:/Users/Z/Downloads/PROJECTS/TD_PROJECTS/PluginBuilder/PluginBuilder_V2/source/launch.vs.json)**: Configuration for attaching Visual Studio C++ debugger directly to TouchDesigner (`touchdesigner.exe`).

### Templates (`templates/`)
- **`BasicCHOP/`**: C++ CHOP template demonstrating custom channels, numeric parameters, and time slice processing.
- **`BasicDAT/`**: C++ DAT template for string/table data generation and manipulation.
- **`CHOPWithPythonClass/`**: Advanced CHOP template showing how to expose C++ methods directly to Python inside TouchDesigner.
- **`CPUMemoryTOP/`**: CPU texture processing TOP template supporting RGBA memory buffers and frame synchronization.
- **`CudaTOP/`**: CUDA GPU kernel TOP template for parallel compute shaders.
- **`SimpleShapesSOP/`**: C++ 3D geometry generator template demonstrating mesh, vertex, and normal generation.

### TouchDesigner SDK Headers (`include/`)
- **`CPlusPlus_Common.h`**: Common data structures, parameter interface definitions, and version macros for TouchDesigner custom operators.
- **`CHOP_CPlusPlusBase.h`**: Abstract base class interface (`CHOP_CPlusPlusBase`) for custom CHOP plugins.
- **`TOP_CPlusPlusBase.h`**: Abstract base class interface (`TOP_CPlusPlusBase`) for custom TOP plugins.
- **`DAT_CPlusPlusBase.h`**: Abstract base class interface (`DAT_CPlusPlusBase`) for custom DAT plugins.
- **`SOP_CPlusPlusBase.h`**: Abstract base class interface (`SOP_CPlusPlusBase`) for custom SOP plugins.

### Bundled Toolchains & Libraries (`3rdParty/`)
- **`Python/`**: Python 3.11 development headers and x64 import libraries for plugins interacting with embedded Python.
- **`OpenCV/`**: OpenCV 4.8 computer vision headers and import libraries for vision-based TOP/DAT operators.

### Development (`dev/`)
- **`dev.toe`**: Interactive development sandbox file.
- **`deploy.py`**: Helper script to package and export `PluginBuilder.tox`.
- **`settings_template.ini`**: Initial configuration template for environment setup.
