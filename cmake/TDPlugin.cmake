# ===========================================================================================
#  TDPlugin.cmake — shared CMake module for TouchDesigner C++ plugins built with PluginBuilder
# ===========================================================================================
#
#  A generated plugin project only needs:
#
#      cmake_minimum_required(VERSION 3.21)
#      project(MyPlugin LANGUAGES CXX)
#      include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)
#      td_add_plugin(MyPlugin FAMILY CHOP)
#
#  Everything else (output layout, SDK include path, MSVC hardening, optimisation flags,
#  compile_commands.json, post-build copy to __Plugins__/, runtime-DLL deployment, optional
#  dependencies, headless tests/benchmarks) lives here, so fixes reach every project on the
#  next configure instead of being frozen into generated text.
#
#  Variables understood (all optional, -D or environment):
#      PLUGIN_BUILDER_DIR      root of PluginBuilder_V2 (the caller normally sets it before include())
#      TD_SDK_INCLUDE_DIR      override the SDK header directory (default ${PLUGIN_BUILDER_DIR}/include)
#      TD_SAMPLES_DIR          <TouchDesigner>/Samples/CPlusPlus (used to locate bundled 3rdParty libs)
#      PLUGIN_DIR              where the finished plugin is deployed (default ../../__Plugins__/<name>)
#      PLUGINBUILDER_BUILD     (env) set by PluginBuilder; when set, PluginBuilder itself copies the DLL
#                              and the CMake POST_BUILD deploy step is skipped.
#      TD_PLUGIN_WARNINGS      OFF|ON  (ON = /W4 /permissive-)   default OFF
#      TD_PLUGIN_ASAN          OFF|ON  (Debug only, /fsanitize=address) default OFF
#
#      Functions:
#      td_add_plugin(<name> FAMILY <CHOP|TOP|DAT|SOP|POP> [SOURCES_DIR <dir>] [FEATURES <cuda|python|opencv>...])
#      td_plugin_optimize(<name> [REALTIME] [AVX2] [AVX512] [FAST_MATH] [LTO])
#      td_plugin_add_runtime_dll(<name> <dll-path>...)
#      td_plugin_use_fftw3(<name> [ROOT <dir>] [PRECISION f|d] [VERSION <tag>])
#      td_plugin_use_cuda(<name>)
#      td_plugin_use_python(<name>)
#      td_plugin_use_opencv(<name> [COMPONENTS ...])
#      td_plugin_add_test(<name> <test-target> SOURCES <files>... [LINK <libs>...])
#      td_plugin_add_bench(<name> <bench-target> SOURCES <files>... [LINK <libs>...] [GATE <baseline.json> [GATE_ARGS ...]])
#      TD_PLUGIN_CXX_STANDARD  17|20 (default 17)
#
#  Consumers: every generated project (CMakeBlocks.BODY includes this file), dev/ci.py
#  scaffolds, and the sibling repo ../Plugin_FFT (FFTW3 DYNAMIC contract, headless tests).
#  Toolchain assumed: CMake >= 3.21 + Ninja + MSVC (vcvarsall captured by
#  PluginBuilderCore.capture_vc_env). SDK headers are API 10 (include/sdk_versions.json).
# ===========================================================================================

include_guard(GLOBAL)
cmake_minimum_required(VERSION 3.21)

# ---------------------------------------------------------------------------------------------
# Resolve PLUGIN_BUILDER_DIR (caller -D > env > location of this file)
# Priority matches generated CMakeLists (CMakeBlocks.BODY): an explicit -D wins, then
# the PLUGIN_BUILDER_DIR environment variable (exported by PluginBuilder builds), then
# the directory of this file (works when the repo layout is intact).
# ---------------------------------------------------------------------------------------------
if(NOT PLUGIN_BUILDER_DIR)
    if(DEFINED ENV{PLUGIN_BUILDER_DIR})
        set(PLUGIN_BUILDER_DIR "$ENV{PLUGIN_BUILDER_DIR}")
    else()
        get_filename_component(PLUGIN_BUILDER_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
    endif()
endif()
file(TO_CMAKE_PATH "${PLUGIN_BUILDER_DIR}" PLUGIN_BUILDER_DIR)
set(PLUGIN_BUILDER_DIR "${PLUGIN_BUILDER_DIR}" CACHE PATH "Path to the PluginBuilder directory")

if(NOT TD_SDK_INCLUDE_DIR)
    set(TD_SDK_INCLUDE_DIR "${PLUGIN_BUILDER_DIR}/include")
endif()
set(TD_SDK_INCLUDE_DIR "${TD_SDK_INCLUDE_DIR}" CACHE PATH "TouchDesigner C++ SDK header directory")

if(NOT TD_SAMPLES_DIR AND DEFINED ENV{TD_SAMPLES_DIR})
    set(TD_SAMPLES_DIR "$ENV{TD_SAMPLES_DIR}")
endif()
if(NOT TD_SAMPLES_DIR AND WIN32)
    # TouchDesigner 2025+ installs into versioned folders (TouchDesigner.2025.33230); the unversioned
    # "TouchDesigner" folder of older installers is accepted too. The highest build wins. Mirrors
    # PluginBuilderCore.find_td_installs so a standalone build finds the same samples tree as the COMP.
    set(_td_pf "$ENV{ProgramFiles}")
    if(NOT _td_pf)
        set(_td_pf "C:/Program Files")
    endif()
    file(TO_CMAKE_PATH "${_td_pf}" _td_pf)
    file(GLOB _td_installs LIST_DIRECTORIES true "${_td_pf}/Derivative/TouchDesigner*")
    set(_td_best "")
    set(_td_best_key "")
    foreach(_d IN LISTS _td_installs)
        if(NOT EXISTS "${_d}/Samples/CPlusPlus")
            continue()
        endif()
        get_filename_component(_n "${_d}" NAME)
        set(_key "0000.000000")
        if(_n MATCHES "([0-9][0-9][0-9][0-9])\.([0-9]+)$")
            set(_b "000000${CMAKE_MATCH_2}")
            string(LENGTH "${_b}" _bl)
            math(EXPR _bs "${_bl} - 6")
            string(SUBSTRING "${_b}" ${_bs} 6 _b)
            set(_key "${CMAKE_MATCH_1}.${_b}")
        endif()
        if(_td_best STREQUAL "" OR _key STRGREATER _td_best_key)
            set(_td_best "${_d}")
            set(_td_best_key "${_key}")
        endif()
    endforeach()
    if(_td_best)
        set(TD_SAMPLES_DIR "${_td_best}/Samples/CPlusPlus")
    endif()
endif()
set(TD_SAMPLES_DIR "${TD_SAMPLES_DIR}" CACHE PATH "TouchDesigner Samples/CPlusPlus directory (for bundled 3rdParty)")

option(TD_PLUGIN_WARNINGS "Enable strict warnings (/W4 /permissive-) for plugin targets" OFF)
set(TD_PLUGIN_CXX_STANDARD 17 CACHE STRING "C++ standard for plugin targets (17 or 20)")
option(TD_PLUGIN_ASAN "Enable AddressSanitizer for Debug builds of plugin targets" OFF)

# ---------------------------------------------------------------------------------------------
# Global project defaults (safe to apply once per configure)
# ---------------------------------------------------------------------------------------------
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Choose the type of build." FORCE)
    set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS "Debug" "Release" "RelWithDebInfo")
endif()

if(NOT DEFINED CMAKE_CXX_STANDARD)
    set(CMAKE_CXX_STANDARD ${TD_PLUGIN_CXX_STANDARD})
endif()

# ctest only generates CTestTestfile.cmake for directories where enable_testing() ran at directory
# scope; calling it inside td_plugin_add_test() (a function) had no effect, so every consumer needed its
# own top-level enable_testing(). include() runs in the caller's scope, so enabling it here fixes that.
enable_testing()
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Debug information:
#   Debug / RelWithDebInfo -> Edit-and-Continue PDB (Visual Studio hot reload)
#   Release                -> plain PDB (/Zi) + /DEBUG at link, with /OPT:REF /OPT:ICF kept, so the binary
#                             that ships is byte-for-byte the optimised one AND a profiler (VTune, WPA,
#                             Superluminal) can symbolise it. The PDB stays in build/bin; only DLLs deploy.
# CUDA-free projects only (nvcc does not understand the /ZI abstraction). Language-guarded with CXX.
if(POLICY CMP0141)
    cmake_policy(SET CMP0141 NEW)
    set(CMAKE_MSVC_DEBUG_INFORMATION_FORMAT
        "$<$<CONFIG:Debug,RelWithDebInfo>:$<IF:$<CXX_COMPILER_ID:MSVC>,EditAndContinue,ProgramDatabase>>$<$<CONFIG:Release>:ProgramDatabase>")
endif()

# Output layout expected by PluginBuilder:  build/bin/<Config>/<name>.dll
foreach(_cfg Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER "${_cfg}" _CFG)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${_CFG} "${CMAKE_BINARY_DIR}/bin/${_cfg}")
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_${_CFG} "${CMAKE_BINARY_DIR}/lib/${_cfg}")
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_${_CFG} "${CMAKE_BINARY_DIR}/lib/${_cfg}")
endforeach()
if(CMAKE_BUILD_TYPE)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/${CMAKE_BUILD_TYPE}")
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/${CMAKE_BUILD_TYPE}")
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/${CMAKE_BUILD_TYPE}")
endif()

if(EXISTS "${TD_SDK_INCLUDE_DIR}/sdk_versions.json")
    file(READ "${TD_SDK_INCLUDE_DIR}/sdk_versions.json" _td_sdk_json)
    string(REGEX REPLACE "[\n\r ]+" " " _td_sdk_json "${_td_sdk_json}")
    message(STATUS "TD SDK headers: ${TD_SDK_INCLUDE_DIR}  ${_td_sdk_json}")
else()
    message(STATUS "TD SDK headers: ${TD_SDK_INCLUDE_DIR}")
endif()

# ---------------------------------------------------------------------------------------------
# Helper: locate a 3rdParty package in the plugin, PluginBuilder or TouchDesigner samples tree
# ---------------------------------------------------------------------------------------------
# Order: the plugin's own copy, then the running TouchDesigner's samples tree (its libraries match the
# DLLs TouchDesigner actually ships, e.g. OpenCV 4.11 / Python 3.11.x), then PluginBuilder's vendored
# copy as a last resort for machines without the samples.
function(_td_find_3rdparty out_var subdir)
    set(_candidates
        "${CMAKE_CURRENT_SOURCE_DIR}/3rdParty/${subdir}"
        "${TD_SAMPLES_DIR}/3rdParty/${subdir}"
        "${PLUGIN_BUILDER_DIR}/3rdParty/${subdir}")
    foreach(_c IN LISTS _candidates)
        if(EXISTS "${_c}")
            set(${out_var} "${_c}" PARENT_SCOPE)
            return()
        endif()
    endforeach()
    set(${out_var} "" PARENT_SCOPE)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_add_plugin — the one call every generated project makes.
# Globs source/ (CONFIGURE_DEPENDS so new files re-glob on build), creates a SHARED
# library with no "lib" prefix, wires SDK includes + MSVC hardening, applies FEATURES,
# and either deploys here (standalone: TDDeploy.cmake POST_BUILD) or leaves deploy to
# PluginBuilder when PLUGINBUILDER_BUILD=1 is set in the environment.
# FAMILY is mandatory and must be one of CHOP|TOP|DAT|SOP|POP.
# ---------------------------------------------------------------------------------------------
function(td_add_plugin name)
    set(_opts "")
    set(_one FAMILY SOURCES_DIR)
    set(_multi FEATURES)
    cmake_parse_arguments(TDP "${_opts}" "${_one}" "${_multi}" ${ARGN})

    if(NOT TDP_FAMILY)
        message(FATAL_ERROR "td_add_plugin(${name}): FAMILY <CHOP|TOP|DAT|SOP|POP> is required")
    endif()
    if(NOT TDP_SOURCES_DIR)
        set(TDP_SOURCES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/source")
    endif()
    get_filename_component(TDP_SOURCES_DIR "${TDP_SOURCES_DIR}" ABSOLUTE)

    # CUDA must be enabled at DIRECTORY scope: enable_language() inside this function left the CUDA
    # toolchain variables (CMAKE_CUDA_COMPILE_OBJECT, ...) in the function scope, so the configure failed
    # with "Missing variable is: CMAKE_CUDA_COMPILE_OBJECT". Generated projects declare
    # project(<name> LANGUAGES CXX CUDA) when the template has the cuda feature (CMakeBlocks.assemble).
    if("cuda" IN_LIST TDP_FEATURES)
        get_property(_enabled GLOBAL PROPERTY ENABLED_LANGUAGES)
        if(NOT "CUDA" IN_LIST _enabled)
            message(FATAL_ERROR "td_add_plugin(${name} ... FEATURES cuda): enable CUDA at directory scope first, "
                                "e.g. project(${name} LANGUAGES CXX CUDA) or enable_language(CUDA) before td_add_plugin().")
        endif()
    endif()

    file(GLOB_RECURSE _sources CONFIGURE_DEPENDS
        "${TDP_SOURCES_DIR}/*.cpp" "${TDP_SOURCES_DIR}/*.c" "${TDP_SOURCES_DIR}/*.cc"
        "${TDP_SOURCES_DIR}/*.h"   "${TDP_SOURCES_DIR}/*.hpp"
        "${TDP_SOURCES_DIR}/*.cu"  "${TDP_SOURCES_DIR}/*.cuh")
    if(NOT _sources)
        message(FATAL_ERROR "td_add_plugin(${name}): no sources found under ${TDP_SOURCES_DIR}")
    endif()
    list(SORT _sources)
    foreach(_s IN LISTS _sources)
        file(RELATIVE_PATH _rel "${CMAKE_CURRENT_SOURCE_DIR}" "${_s}")
        message(STATUS "${name} source: ${_rel}")
    endforeach()

    add_library(${name} SHARED ${_sources})
    set_target_properties(${name} PROPERTIES
        PREFIX ""
        OUTPUT_NAME "${name}"
        TD_PLUGIN_FAMILY "${TDP_FAMILY}"
        TD_PLUGIN_SOURCES_DIR "${TDP_SOURCES_DIR}")

    target_include_directories(${name} PRIVATE "${TDP_SOURCES_DIR}" "${TD_SDK_INCLUDE_DIR}")
    target_compile_definitions(${name} PRIVATE
        TD_PLUGIN_NAME="${name}"
        TD_PLUGIN_FAMILY_${TDP_FAMILY}=1)

    if(WIN32)
        # NOMINMAX is defined by CPlusPlus_Common.h itself (API 10), so it is not repeated here.
        target_compile_definitions(${name} PRIVATE
            WIN32 _WINDOWS _USRDLL
            _USE_MATH_DEFINES        # M_PI & friends from <cmath>
            WIN32_LEAN_AND_MEAN)
    endif()

    if(MSVC)
        # MSVC flags are scoped to C++ sources: passed raw to nvcc for a .cu file they break the compile
        # ("nvcc fatal: A single input file is required ..." for /W3).
        # (/MP is deliberately absent: Ninja already builds translation units in parallel)
        target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/utf-8 /Zc:__cplusplus>)
        if(TD_PLUGIN_WARNINGS)
            target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/W4 /permissive->)
        else()
            target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/W3>)
        endif()
        # Debug info format comes from CMAKE_MSVC_DEBUG_INFORMATION_FORMAT above; the linker must be told
        # to emit the PDB for Release too. /OPT:REF /OPT:ICF are restated because /DEBUG would otherwise
        # switch them off.
        target_link_options(${name} PRIVATE
            $<$<CONFIG:Debug>:/INCREMENTAL>
            $<$<CONFIG:Release,RelWithDebInfo>:/INCREMENTAL:NO>
            $<$<CONFIG:Release>:/DEBUG /OPT:REF /OPT:ICF>)
        if(TD_PLUGIN_ASAN)
            target_compile_options(${name} PRIVATE $<$<AND:$<CONFIG:Debug>,$<COMPILE_LANGUAGE:CXX>>:/fsanitize=address>)
        endif()
    endif()

    if("cuda" IN_LIST TDP_FEATURES)
        td_plugin_use_cuda(${name})
    endif()
    if("python" IN_LIST TDP_FEATURES)
        td_plugin_use_python(${name})
    endif()
    if("opencv" IN_LIST TDP_FEATURES)
        td_plugin_use_opencv(${name})
    endif()

    # Deployment directory (__Plugins__/<name> next to the .toe, two levels above PluginProjects/<name>)
    # Callers may override with -DPLUGIN_DIR=... (dev/ci.py uses a throwaway __deploy__).
    if(NOT PLUGIN_DIR)
        set(PLUGIN_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../__Plugins__/${name}")
    endif()
    get_filename_component(PLUGIN_DIR "${PLUGIN_DIR}" ABSOLUTE)
    set_target_properties(${name} PROPERTIES TD_PLUGIN_DEPLOY_DIR "${PLUGIN_DIR}")

    # PLUGINBUILDER_BUILD is exported by PluginBuilderExt._env_factory / dev/ci.py so the
    # extension owns the copy-to-__Plugins__ step (safe_replace_file + loader reload) and
    # this module does not double-deploy mid-build.
    if(DEFINED ENV{PLUGINBUILDER_BUILD})
        message(STATUS "PluginBuilder is building ${name} (deploy handled by PluginBuilder)")
    else()
        # Standalone (Visual Studio / command line) build: deploy with the rename-in-place trick so a
        # DLL currently loaded by TouchDesigner can still be replaced (Windows allows renaming loaded DLLs).
        add_custom_command(TARGET ${name} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E make_directory "${PLUGIN_DIR}"
            COMMAND ${CMAKE_COMMAND} -DSRC=$<TARGET_FILE:${name}> -DDST_DIR=${PLUGIN_DIR}
                    -P "${PLUGIN_BUILDER_DIR}/cmake/TDDeploy.cmake"
            COMMENT "Deploying ${name} to ${PLUGIN_DIR}"
            VERBATIM)
    endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_optimize — release-only MSVC/GCC optimisation switches.
# AVX2/AVX512 emit /arch:* (MSVC) or -mavx2 -mfma (GCC); FAST_MATH = /fp:fast | -ffast-math;
# LTO = /GL + /LTCG on MSVC. CONFIG:Release,RelWithDebInfo always get /O2-style opts and
# /OPT:REF /OPT:ICF. Headless tests/benches inherit LINK_OPTIONS via _td_add_console.
# ---------------------------------------------------------------------------------------------
function(td_plugin_optimize name)
    cmake_parse_arguments(TDO "AVX2;AVX512;FAST_MATH;LTO;REALTIME" "" "" ${ARGN})
    if(TDO_REALTIME)
        # The real-time preset: AVX2 + fast math + LTO, plus TD_PLUGIN_REALTIME=1 so code can assert
        # it. Pair it with the runtime CPU guard (td_cpu_guard.h, on the include path whenever AVX2 or
        # AVX512 is requested) so the plugin refuses to process on a CPU without AVX2/FMA instead of
        # faulting TouchDesigner with an illegal instruction.
        set(TDO_AVX2 TRUE)
        set(TDO_FAST_MATH TRUE)
        set(TDO_LTO TRUE)
        target_compile_definitions(${name} PRIVATE TD_PLUGIN_REALTIME=1)
    endif()
    if(TDO_AVX2 OR TDO_AVX512)
        target_include_directories(${name} PRIVATE "${PLUGIN_BUILDER_DIR}/cmake/td_runtime")
    endif()
    if(MSVC)
        target_compile_options(${name} PRIVATE $<$<AND:$<CONFIG:Release,RelWithDebInfo>,$<COMPILE_LANGUAGE:CXX>>:/O2 /Oi /Ot /Gy>)
        if(TDO_AVX512)
            target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/arch:AVX512>)
        elseif(TDO_AVX2)
            target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/arch:AVX2>)
        endif()
        if(TDO_FAST_MATH)
            target_compile_options(${name} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/fp:fast>)
        endif()
        if(TDO_LTO)
            target_compile_options(${name} PRIVATE $<$<AND:$<CONFIG:Release>,$<COMPILE_LANGUAGE:CXX>>:/GL>)
            target_link_options(${name} PRIVATE $<$<CONFIG:Release>:/LTCG>)
        endif()
        target_link_options(${name} PRIVATE $<$<CONFIG:Release,RelWithDebInfo>:/OPT:REF /OPT:ICF>)
    else()
        target_compile_options(${name} PRIVATE $<$<CONFIG:Release,RelWithDebInfo>:-O3>)
        if(TDO_AVX2)
            target_compile_options(${name} PRIVATE -mavx2 -mfma)
        endif()
        if(TDO_FAST_MATH)
            target_compile_options(${name} PRIVATE -ffast-math)
        endif()
    endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_add_runtime_dll — copy runtime DLLs next to the built plugin (and deploy them)
# Also stages into TD_PLUGIN_DEPLOY_DIR on standalone builds so third-party DLLs land in
# __Plugins__/<name>/ beside the plugin (PluginBuilder builds stage via safe_replace_file instead).
# ---------------------------------------------------------------------------------------------
function(td_plugin_add_runtime_dll name)
    foreach(_dll IN LISTS ARGN)
        if(NOT EXISTS "${_dll}")
            message(WARNING "td_plugin_add_runtime_dll(${name}): ${_dll} does not exist")
            continue()
        endif()
        # Record the intended runtime set: PluginBuilder stages exactly these next to the plugin
        # (build/<name>_runtime_dlls.txt) instead of sweeping every *.dll it finds in bin/.
        get_filename_component(_dll_name "${_dll}" NAME)
        set_property(TARGET ${name} APPEND PROPERTY TD_PLUGIN_RUNTIME_DLLS "${_dll_name}")
        get_target_property(_all_rt ${name} TD_PLUGIN_RUNTIME_DLLS)
        string(REPLACE ";" "\n" _rt_text "${_all_rt}")
        file(WRITE "${CMAKE_BINARY_DIR}/${name}_runtime_dlls.txt" "${_rt_text}\n")
        add_custom_command(TARGET ${name} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_dll}" "$<TARGET_FILE_DIR:${name}>"
            COMMENT "Staging runtime DLL ${_dll}"
            VERBATIM)
        if(NOT DEFINED ENV{PLUGINBUILDER_BUILD})
            get_target_property(_deploy ${name} TD_PLUGIN_DEPLOY_DIR)
            add_custom_command(TARGET ${name} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_dll}" "${_deploy}"
                VERBATIM)
        endif()
    endforeach()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_fftw3 — vendored prebuilt FFTW3 (Windows dll64 layout)
#
#   td_plugin_use_fftw3(<name> [ROOT <dir>] [PRECISION f|d] [VERSION <tag>])
#
# Versioned layout (preferred):
#   <ROOT>/include/fftw3-<tag>.h            the upstream header, unmodified
#   <ROOT>/include/fftw3.h                  forwarder to it, so #include <fftw3.h> keeps working
#   <ROOT>/lib/libfftw3f-<tag>.def          export list, generated from the built DLL
#   <ROOT>/lib/libfftw3f-<tag>.lib          MSVC import library (regenerated from the .def if absent)
#   <ROOT>/bin|lib/libfftw3f-<tag>.dll      the runtime DLL deployed next to the plugin
#   <ROOT>/VERSION                          provenance: upstream version, source URL, hashes, SIMD set
#
# Legacy layout, still supported: libfftw3f-3.{def,lib,dll} + include/fftw3.h.
#
# The version tag exists because a vendored FFTW is a binary artifact whose performance is a property
# of *that file*, not of the code calling it: 3.3.5 with SSE2 only and 3.3.11 with AVX2 codelets are
# different machines behind an identical API. Putting the tag in every artifact name (DLL, import
# library, .def, .exp, header) means the running DLL can be identified from the plugin folder alone,
# with no build log to hand, and it makes two versions safely co-installable.
#
# Two consumer details that are easy to get wrong and are handled here:
#   * FFTW_DLL must be defined for MSVC consumers. fftw3.h is explicit about this ("for Windows
#     compilers, you should add ... #define FFTW_DLL"); without it, data symbols (fftwf_version,
#     fftwf_cc, fftwf_codelet_optim) bind to the import library's jump thunk rather than to the data
#     in the DLL, so reading fftwf_version as a C string yields machine code, not a version string.
#     Under MinGW/libtool this is done for you, which is why it is often forgotten.
#   * the import library's embedded module name must equal the runtime DLL's file name. A plain
#     file rename does not achieve that, so the .def is regenerated with the correct LIBRARY line
#     and lib.exe is re-run whenever the .def is newer than the .lib.
#
#   DYNAMIC (option, off by default):
#   Skip linking the import library; the target resolves fftwf_* through LoadLibrary/GetProcAddress at
#   run time instead. Required whenever more than one FFT library may be selected at run time, because
#   FFTW3 and Intel oneMKL's FFTW3 interface export the SAME fftwf_* names - linking one of them makes
#   the choice at link time and the other one unreachable (or, worse, silently resolves to whichever
#   the loader found first). The header directory and the runtime DLL copy are still set up, and
#   TD_PLUGIN_FFTW3_DYNAMIC=1 is defined, so the code can assert it is not calling a linked symbol.
#   Probes and benchmarks that only ever want one implementation should not pass DYNAMIC.
#
#   Known consumer: ../Plugin_FFT pins VERSION 3.3.11-avx2 DYNAMIC (AVX2 codelets; performance is a
#   property of that exact vendored binary — see the layout notes above).
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_fftw3 name)
    cmake_parse_arguments(TDF "DYNAMIC" "ROOT;PRECISION;VERSION" "" ${ARGN})
    if(NOT TDF_PRECISION)
        set(TDF_PRECISION f)
    endif()
    if(TDF_PRECISION STREQUAL "f")
        set(_stem libfftw3f)
    elseif(TDF_PRECISION STREQUAL "d")
        set(_stem libfftw3)
    else()
        message(FATAL_ERROR "td_plugin_use_fftw3: PRECISION must be f or d")
    endif()
    if(NOT TDF_ROOT)
        _td_find_3rdparty(TDF_ROOT fftw3)
    endif()
    if(NOT TDF_ROOT)
        message(FATAL_ERROR "td_plugin_use_fftw3(${name}): fftw3 not found (looked in 3rdParty/fftw3 of plugin, PluginBuilder and TD samples)")
    endif()

    # ---- resolve the version tag -------------------------------------------------------------
    if(TDF_VERSION)
        set(_tag "${TDF_VERSION}")
    else()
        file(GLOB _versioned_libs "${TDF_ROOT}/lib/${_stem}-*.lib")
        list(LENGTH _versioned_libs _nv)
        if(_nv EQUAL 1)
            get_filename_component(_one "${_versioned_libs}" NAME_WE)
            string(REPLACE "${_stem}-" "" _tag "${_one}")
        elseif(_nv GREATER 1)
            # Deliberately not "pick the highest": version strings do not sort lexicographically
            # (3.3.6 > 3.3.11 as text) and silently linking the wrong FFTW is exactly the kind of
            # mistake the version tag exists to prevent.
            message(FATAL_ERROR
                "td_plugin_use_fftw3(${name}): ${_nv} versioned FFTW libraries under ${TDF_ROOT}/lib "
                "(${_versioned_libs}); pass VERSION <tag> to choose one")
        else()
            set(_tag "")   # legacy unversioned layout
        endif()
    endif()
    if(_tag)
        set(_lib "${_stem}-${_tag}")
    else()
        set(_lib "${_stem}-3")
    endif()

    # ---- header ------------------------------------------------------------------------------
    if(_tag AND EXISTS "${TDF_ROOT}/include/fftw3-${_tag}.h")
        set(FFTW3_INCLUDE_HINT "fftw3-${_tag}.h")
    else()
        set(FFTW3_INCLUDE_HINT "fftw3.h")
    endif()
    # Resolved by direct path rather than find_path/find_library, because those cache their answer:
    # after a version bump on disk, a cached FFTW3_LIBRARY still names the previous version's import
    # library and the configure either links the wrong FFTW or fails with "missing and no known rule
    # to make it". The layout is fixed by this function's contract, so stating the path is both
    # simpler and immune to that. The find_* calls remain only as a fallback for legacy trees.
    set(_hdr_path "${TDF_ROOT}/include/${FFTW3_INCLUDE_HINT}")
    if(EXISTS "${_hdr_path}")
        set(FFTW3_INCLUDE_DIR "${TDF_ROOT}/include")
    else()
        find_path(FFTW3_INCLUDE_DIR NAMES fftw3.h PATHS "${TDF_ROOT}/include" "${TDF_ROOT}" NO_DEFAULT_PATH)
        unset(FFTW3_INCLUDE_DIR CACHE)
        find_path(FFTW3_INCLUDE_DIR NAMES fftw3.h PATHS "${TDF_ROOT}/include" "${TDF_ROOT}" NO_DEFAULT_PATH)
    endif()

    # ---- import library ----------------------------------------------------------------------
    # The .def's LIBRARY line is what the linker bakes into the import library, and the loader
    # follows *that* name, not the .lib's file name. Renaming libfftw3f-3.{dll,lib} to a versioned
    # name without editing the line inside the .def produces an import library that still demands
    # libfftw3f-3.dll at load time. The two names are required to agree, so it is enforced here.
    #
    # Nothing is ever written into the vendored tree: a configure used to rewrite the .def and re-run
    # lib.exe in 3rdParty/ whenever the .def was not older than the .lib (IS_NEWER_THAN is also true on
    # equal timestamps, i.e. after every git checkout), which left the vendored .lib permanently
    # "modified" in git. Now the vendored .lib is used as-is when its .def agrees with the DLL name, and
    # otherwise a corrected .def + .lib are generated under ${CMAKE_BINARY_DIR}/td_deps/.
    set(_def "${TDF_ROOT}/lib/${_lib}.def")
    set(_imp "${TDF_ROOT}/lib/${_lib}.lib")
    set(_def_ok TRUE)
    if(EXISTS "${_def}")
        file(STRINGS "${_def}" _def_lib_line REGEX "^LIBRARY")
        if(_def_lib_line AND NOT _def_lib_line STREQUAL "LIBRARY ${_lib}.dll")
            set(_def_ok FALSE)
        endif()
    endif()
    if(MSVC AND EXISTS "${_def}" AND (NOT EXISTS "${_imp}" OR NOT _def_ok))
        set(_gen_dir "${CMAKE_BINARY_DIR}/td_deps/fftw3")
        file(MAKE_DIRECTORY "${_gen_dir}")
        file(READ "${_def}" _def_text)
        if(_def_lib_line)
            string(REPLACE "${_def_lib_line}" "LIBRARY ${_lib}.dll" _def_text "${_def_text}")
        endif()
        file(WRITE "${_gen_dir}/${_lib}.def" "${_def_text}")
        find_program(MSVC_LIB_EXE lib.exe)
        if(MSVC_LIB_EXE)
            message(STATUS "td_plugin_use_fftw3(${name}): generating ${_gen_dir}/${_lib}.lib (vendored .lib missing or naming a different DLL)")
            execute_process(COMMAND "${MSVC_LIB_EXE}" /nologo /machine:x64 "/def:${_gen_dir}/${_lib}.def" "/out:${_gen_dir}/${_lib}.lib"
                            WORKING_DIRECTORY "${_gen_dir}" RESULT_VARIABLE _r OUTPUT_QUIET)
            if(_r EQUAL 0)
                set(_imp "${_gen_dir}/${_lib}.lib")
            else()
                message(WARNING "td_plugin_use_fftw3(${name}): lib.exe failed to generate ${_lib}.lib (exit ${_r})")
            endif()
        else()
            message(WARNING "td_plugin_use_fftw3(${name}): lib.exe not found, cannot generate ${_lib}.lib from ${_lib}.def")
        endif()
    endif()

    if(EXISTS "${_imp}")
        set(FFTW3_LIBRARY "${_imp}")
    else()
        unset(FFTW3_LIBRARY CACHE)
        find_library(FFTW3_LIBRARY NAMES ${_lib} PATHS "${TDF_ROOT}/lib" "${TDF_ROOT}" NO_DEFAULT_PATH)
    endif()

    set(FFTW3_DLL "")
    foreach(_d "${TDF_ROOT}/bin/${_lib}.dll" "${TDF_ROOT}/lib/${_lib}.dll" "${TDF_ROOT}/${_lib}.dll")
        if(EXISTS "${_d}")
            set(FFTW3_DLL "${_d}")
            break()
        endif()
    endforeach()

    if(NOT FFTW3_INCLUDE_DIR OR (NOT FFTW3_LIBRARY AND NOT TDF_DYNAMIC))
        if(TDF_DYNAMIC)
            message(FATAL_ERROR "td_plugin_use_fftw3(${name}): ${FFTW3_INCLUDE_HINT} not found under ${TDF_ROOT}")
        endif()
        message(FATAL_ERROR "td_plugin_use_fftw3(${name}): ${FFTW3_INCLUDE_HINT} or ${_lib}.lib not found under ${TDF_ROOT}")
    endif()

    # ---- provenance, from the VERSION file if the vendor kept one ----------------------------
    set(_fftw3_tag "${_lib}")
    if(EXISTS "${TDF_ROOT}/VERSION")
        file(STRINGS "${TDF_ROOT}/VERSION" _ver_lines REGEX "^FFTW_VENDORED_(UPSTREAM_VERSION|BUILD_TAG|SIMD|THREADS)=")
        foreach(_l IN LISTS _ver_lines)
            string(REPLACE "=" " = " _l "${_l}")
            message(STATUS "  fftw3 ${_l}")
        endforeach()
        set(_ver_upstream "")
        foreach(_l IN LISTS _ver_lines)
            if(_l MATCHES "^FFTW_VENDORED_UPSTREAM_VERSION=([0-9.]+)")
                set(_ver_upstream "${CMAKE_MATCH_1}")
            endif()
        endforeach()
        if(_ver_upstream)
            set(_fftw3_tag "${_ver_upstream}")
        endif()
    endif()

    if(NOT FFTW3_DLL)
        if(TDF_DYNAMIC)
            message(WARNING "td_plugin_use_fftw3(${name}) DYNAMIC: no ${_lib}.dll under ${TDF_ROOT}/bin or /lib. "
                            "The plugin resolves its FFT library at run time and will fall back to whatever "
                            "else it can find, so this build will run but not with the vendored FFTW.")
        else()
            message(WARNING "td_plugin_use_fftw3(${name}): no ${_lib}.dll under ${TDF_ROOT}/bin or /lib. "
                            "The plugin will import it at load time and fail with 'module not found' "
                            "unless it is already on the DLL search path.")
        endif()
    endif()
    message(STATUS "  dll:      ${FFTW3_DLL}")
    message(STATUS "  header:   ${FFTW3_INCLUDE_DIR}/${FFTW3_INCLUDE_HINT}")
    target_include_directories(${name} PRIVATE "${FFTW3_INCLUDE_DIR}")
    target_compile_definitions(${name} PRIVATE TD_PLUGIN_HAS_FFTW3=1)
    if(TDF_DYNAMIC)
        # No target_link_libraries here on purpose: the plugin picks its FFT library at run time.
        target_compile_definitions(${name} PRIVATE TD_PLUGIN_FFTW3_DYNAMIC=1)
        message(STATUS "  link:     (dynamic - fftwf_* resolved at run time via LoadLibrary)")
    else()
        target_link_libraries(${name} PRIVATE "${FFTW3_LIBRARY}")
        message(STATUS "  link:     ${FFTW3_LIBRARY}")
        if(MSVC)
            # See the note above: without this, fftwf_version and friends read as garbage on MSVC.
            target_compile_definitions(${name} PRIVATE FFTW_DLL=1)
        endif()
    endif()
    set_property(TARGET ${name} PROPERTY TD_PLUGIN_FFTW3_VERSION "${_fftw3_tag}")
    # Recorded even for DYNAMIC targets, which deliberately do not link it: the headless test and bench
    # executables are single-backend by construction (they exist to measure one implementation), so
    # _td_add_console() links this import library for them. See the DYNAMIC note above.
    set_property(TARGET ${name} PROPERTY TD_PLUGIN_FFTW3_LINK "${FFTW3_LIBRARY}")
    if(FFTW3_DLL)
        td_plugin_add_runtime_dll(${name} "${FFTW3_DLL}")
    endif()
    set(FFTW3_INCLUDE_DIR "${FFTW3_INCLUDE_DIR}" PARENT_SCOPE)
    set(FFTW3_LIBRARY "${FFTW3_LIBRARY}" PARENT_SCOPE)
    set(FFTW3_DLL "${FFTW3_DLL}" PARENT_SCOPE)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_cuda — TouchDesigner 2025 samples ship against CUDA 12.x
# Arch list covers Turing..Blackwell; override with -DCMAKE_CUDA_ARCHITECTURES=native for a
# local GPU-only build. Requires CUDA enabled at directory scope (project(... LANGUAGES CXX CUDA)) and FEATURES cuda.
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_cuda name)
    find_package(CUDAToolkit REQUIRED)
    if(NOT CMAKE_CUDA_ARCHITECTURES)
        # Turing .. Hopper; Blackwell (120) only when the toolkit knows it (CUDA >= 12.8), otherwise
        # nvcc rejects the whole configure. Use "native" for a dev-only build of the current GPU.
        set(_archs 75 80 86 89 90)
        if(CUDAToolkit_VERSION VERSION_GREATER_EQUAL 12.8)
            list(APPEND _archs 120)
        endif()
        set(CMAKE_CUDA_ARCHITECTURES ${_archs} PARENT_SCOPE)
        set_property(TARGET ${name} PROPERTY CUDA_ARCHITECTURES ${_archs})
    endif()
    message(STATUS "CUDA Toolkit ${CUDAToolkit_VERSION}: ${CUDAToolkit_INCLUDE_DIRS}")
    target_include_directories(${name} PRIVATE ${CUDAToolkit_INCLUDE_DIRS})
    target_link_libraries(${name} PRIVATE CUDA::cudart)
    set_target_properties(${name} PROPERTIES CUDA_SEPARABLE_COMPILATION OFF)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_python — embedded CPython headers/import lib (vendored or from TD samples)
# Used by the CHOPWithPythonClass template (FEATURES python). Picks the newest python3*.lib
# under 3rdParty/Python/lib/x64 (skips the bare python3.lib stub).
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_python name)
    _td_find_3rdparty(_py Python)
    if(NOT _py)
        message(FATAL_ERROR "td_plugin_use_python(${name}): 3rdParty/Python not found")
    endif()
    target_include_directories(${name} PRIVATE "${_py}/Include" "${_py}/Include/PC")
    # python3XY.lib only (not the python3.lib stable-ABI stub, not *_d debug libs); highest minor wins.
    file(GLOB _pylibs "${_py}/lib/x64/python3*.lib")
    list(FILTER _pylibs INCLUDE REGEX "python3[0-9]+\\.lib$")
    list(SORT _pylibs COMPARE NATURAL ORDER DESCENDING)
    if(_pylibs)
        list(GET _pylibs 0 _pylib)
        target_link_libraries(${name} PRIVATE "${_pylib}")
        message(STATUS "Python import library: ${_pylib}")
    else()
        target_link_directories(${name} PRIVATE "${_py}/lib/x64")
    endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_opencv — vendored OpenCV (headers + import libs), as shipped with TD samples
# Links non-debug Win64 import libs; runtime DLLs are staged via td_plugin_add_runtime_dll.
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_opencv name)
    cmake_parse_arguments(TDC "" "" "COMPONENTS" ${ARGN})
    _td_find_3rdparty(_cv opencv)
    if(NOT _cv)
        message(FATAL_ERROR "td_plugin_use_opencv(${name}): 3rdParty/opencv not found")
    endif()
    target_include_directories(${name} PRIVATE "${_cv}/include")
    file(GLOB _cvlibs "${_cv}/lib/Win64/*.lib" "${_cv}/lib/*.lib")
    list(FILTER _cvlibs EXCLUDE REGEX "[0-9]d\\.lib$")   # debug import libs end in <version>d.lib
    if(TDC_COMPONENTS)
        # Link only what was asked for (e.g. COMPONENTS core imgproc): fewer import libraries, fewer
        # DLL dependencies for the loader to resolve when TouchDesigner maps the plugin.
        set(_wanted "")
        foreach(_c IN LISTS TDC_COMPONENTS)
            foreach(_l IN LISTS _cvlibs)
                get_filename_component(_ln "${_l}" NAME_WE)
                if(_ln MATCHES "^opencv_${_c}[0-9]*$" OR _ln MATCHES "^opencv_world[0-9]*$")
                    list(APPEND _wanted "${_l}")
                endif()
            endforeach()
        endforeach()
        list(REMOVE_DUPLICATES _wanted)
        set(_cvlibs ${_wanted})
    endif()
    target_link_libraries(${name} PRIVATE ${_cvlibs})
    file(GLOB _cvdlls "${_cv}/bin/*.dll" "${_cv}/lib/Win64/*.dll")
    if(_cvdlls)
        td_plugin_add_runtime_dll(${name} ${_cvdlls})
    endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_add_test / td_plugin_add_bench — headless executables that reuse the plugin sources
#   (no TouchDesigner needed). The plugin's include dirs / definitions are inherited.
#   Inherit compile AND link options so an LTO plugin is measured with /GL+/LTCG, not silently
#   degraded. Strip TD_PLUGIN_FFTW3_DYNAMIC from the exe defs: these tools link the import lib
#   directly (single-backend by construction). Run via PluginBuilderExt.run_tests (ctest).
# ---------------------------------------------------------------------------------------------
function(_td_add_console name target kind)
    cmake_parse_arguments(TDT "" "" "SOURCES;LINK" ${ARGN})
    if(NOT TDT_SOURCES)
        message(FATAL_ERROR "td_plugin_add_${kind}(${name} ${target}): SOURCES required")
    endif()
    add_executable(${target} ${TDT_SOURCES})
    get_target_property(_src ${name} TD_PLUGIN_SOURCES_DIR)
    target_include_directories(${target} PRIVATE "${_src}" "${TD_SDK_INCLUDE_DIR}")
    get_target_property(_defs ${name} COMPILE_DEFINITIONS)
    if(_defs)
        # A DYNAMIC plugin defines TD_PLUGIN_FFTW3_DYNAMIC ("fftwf_* comes from LoadLibrary, not the
        # linker"). These executables link FFTW3 below, so inheriting that marker would be a lie.
        list(REMOVE_ITEM _defs TD_PLUGIN_FFTW3_DYNAMIC)
        target_compile_definitions(${target} PRIVATE ${_defs})
    endif()
    get_target_property(_opts ${name} COMPILE_OPTIONS)
    if(_opts)
        target_compile_options(${target} PRIVATE ${_opts})
    endif()
    get_target_property(_incs ${name} INCLUDE_DIRECTORIES)
    if(_incs)
        target_include_directories(${target} PRIVATE ${_incs})
    endif()
    get_target_property(_libs ${name} LINK_LIBRARIES)
    if(_libs)
        target_link_libraries(${target} PRIVATE ${_libs})
    endif()
    # FFTW3 (if the plugin uses it) is linked here even when the plugin itself is DYNAMIC. A headless
    # tool measures or probes exactly one implementation, and the direct-symbol form is what lets the
    # probes read fftwf_version without a backend table of their own.
    get_target_property(_fftw3lib ${name} TD_PLUGIN_FFTW3_LINK)
    if(_fftw3lib AND EXISTS "${_fftw3lib}")
        target_link_libraries(${target} PRIVATE "${_fftw3lib}")
        if(MSVC)
            target_compile_definitions(${target} PRIVATE FFTW_DLL=1)
        endif()
    endif()
    # Link options matter as much as compile options here: /GL without /LTCG degrades to a warning
    # and silently drops the whole-program optimisation, so a bench built from an LTO plugin would
    # otherwise measure something the plugin is not.
    get_target_property(_lopts ${name} LINK_OPTIONS)
    if(_lopts)
        target_link_options(${target} PRIVATE ${_lopts})
    endif()
    if(TDT_LINK)
        target_link_libraries(${target} PRIVATE ${TDT_LINK})
    endif()
    set_target_properties(${target} PROPERTIES FOLDER "${kind}")
endfunction()

function(td_plugin_add_test name target)
    _td_add_console(${name} ${target} test ${ARGN})
    add_test(NAME ${target} COMMAND ${target} WORKING_DIRECTORY "$<TARGET_FILE_DIR:${target}>")
endfunction()

# td_plugin_add_bench(<name> <target> SOURCES ... [LINK ...] [GATE <baseline.json> [GATE_ARGS ...]])
# GATE registers a ctest named <target>_gate with the label "perf": it runs
#   <target> --gate <baseline.json> [GATE_ARGS...]
# and the bench decides pass/fail against the baseline (the bench owns the metric and the tolerance).
# Run with `ctest -L perf`; exclude with `ctest -LE perf` (timing is machine-dependent).
function(td_plugin_add_bench name target)
    cmake_parse_arguments(TDB "" "GATE" "GATE_ARGS" ${ARGN})
    _td_add_console(${name} ${target} bench ${TDB_UNPARSED_ARGUMENTS})
    if(TDB_GATE)
        get_filename_component(_gate "${TDB_GATE}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME ${target}_gate COMMAND ${target} --gate "${_gate}" ${TDB_GATE_ARGS}
                 WORKING_DIRECTORY "$<TARGET_FILE_DIR:${target}>")
        set_tests_properties(${target}_gate PROPERTIES LABELS perf RUN_SERIAL TRUE)
    endif()
endfunction()
