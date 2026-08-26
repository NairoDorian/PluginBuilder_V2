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
#  Functions:
#      td_add_plugin(<name> FAMILY <CHOP|TOP|DAT|SOP|POP> [SOURCES_DIR <dir>] [FEATURES <cuda|python|opencv>...])
#      td_plugin_optimize(<name> [AVX2] [AVX512] [FAST_MATH] [LTO])
#      td_plugin_add_runtime_dll(<name> <dll-path>...)
#      td_plugin_use_fftw3(<name> [ROOT <dir>] [PRECISION f|d])
#      td_plugin_use_cuda(<name>)
#      td_plugin_use_python(<name>)
#      td_plugin_use_opencv(<name> [COMPONENTS ...])
#      td_plugin_add_test(<name> <test-target> SOURCES <files>... [LINK <libs>...])
#      td_plugin_add_bench(<name> <bench-target> SOURCES <files>... [LINK <libs>...])
# ===========================================================================================

include_guard(GLOBAL)
cmake_minimum_required(VERSION 3.21)

# ---------------------------------------------------------------------------------------------
# Resolve PLUGIN_BUILDER_DIR (caller -D > env > location of this file)
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
if(NOT TD_SAMPLES_DIR AND EXISTS "C:/Program Files/Derivative/TouchDesigner/Samples/CPlusPlus")
    set(TD_SAMPLES_DIR "C:/Program Files/Derivative/TouchDesigner/Samples/CPlusPlus")
endif()
set(TD_SAMPLES_DIR "${TD_SAMPLES_DIR}" CACHE PATH "TouchDesigner Samples/CPlusPlus directory (for bundled 3rdParty)")

option(TD_PLUGIN_WARNINGS "Enable strict warnings (/W4 /permissive-) for plugin targets" OFF)
option(TD_PLUGIN_ASAN "Enable AddressSanitizer for Debug builds of plugin targets" OFF)

# ---------------------------------------------------------------------------------------------
# Global project defaults (safe to apply once per configure)
# ---------------------------------------------------------------------------------------------
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Choose the type of build." FORCE)
    set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS "Debug" "Release" "RelWithDebInfo")
endif()

if(NOT DEFINED CMAKE_CXX_STANDARD)
    set(CMAKE_CXX_STANDARD 17)
endif()
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Edit-and-Continue friendly PDB format for Debug/RelWithDebInfo (Visual Studio hot reload)
if(POLICY CMP0141)
    cmake_policy(SET CMP0141 NEW)
    set(CMAKE_MSVC_DEBUG_INFORMATION_FORMAT
        "$<IF:$<AND:$<C_COMPILER_ID:MSVC>,$<CXX_COMPILER_ID:MSVC>>,$<$<CONFIG:Debug,RelWithDebInfo>:EditAndContinue>,$<$<CONFIG:Debug,RelWithDebInfo>:ProgramDatabase>>")
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
function(_td_find_3rdparty out_var subdir)
    set(_candidates
        "${CMAKE_CURRENT_SOURCE_DIR}/3rdParty/${subdir}"
        "${PLUGIN_BUILDER_DIR}/3rdParty/${subdir}"
        "${TD_SAMPLES_DIR}/3rdParty/${subdir}")
    foreach(_c IN LISTS _candidates)
        if(EXISTS "${_c}")
            set(${out_var} "${_c}" PARENT_SCOPE)
            return()
        endif()
    endforeach()
    set(${out_var} "" PARENT_SCOPE)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_add_plugin
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

    set(_langs CXX)
    if("cuda" IN_LIST TDP_FEATURES)
        enable_language(CUDA)
        list(APPEND _langs CUDA)
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
        target_compile_options(${name} PRIVATE /MP /utf-8 /Zc:__cplusplus)
        if(TD_PLUGIN_WARNINGS)
            target_compile_options(${name} PRIVATE /W4 /permissive-)
        else()
            target_compile_options(${name} PRIVATE /W3)
        endif()
        # Debug info: Debug/RelWithDebInfo carry a PDB; Release stays lean (no /Zi, no /INCREMENTAL).
        target_compile_options(${name} PRIVATE $<$<CONFIG:Debug,RelWithDebInfo>:/Zi>)
        target_link_options(${name} PRIVATE
            $<$<CONFIG:Debug>:/INCREMENTAL>
            $<$<CONFIG:Release,RelWithDebInfo>:/INCREMENTAL:NO>)
        if(TD_PLUGIN_ASAN)
            target_compile_options(${name} PRIVATE $<$<CONFIG:Debug>:/fsanitize=address>)
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
    if(NOT PLUGIN_DIR)
        set(PLUGIN_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../__Plugins__/${name}")
    endif()
    get_filename_component(PLUGIN_DIR "${PLUGIN_DIR}" ABSOLUTE)
    set_target_properties(${name} PROPERTIES TD_PLUGIN_DEPLOY_DIR "${PLUGIN_DIR}")

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
# td_plugin_optimize
# ---------------------------------------------------------------------------------------------
function(td_plugin_optimize name)
    cmake_parse_arguments(TDO "AVX2;AVX512;FAST_MATH;LTO" "" "" ${ARGN})
    if(MSVC)
        target_compile_options(${name} PRIVATE $<$<CONFIG:Release,RelWithDebInfo>:/O2 /Oi /Ot /Gy>)
        if(TDO_AVX512)
            target_compile_options(${name} PRIVATE /arch:AVX512)
        elseif(TDO_AVX2)
            target_compile_options(${name} PRIVATE /arch:AVX2)
        endif()
        if(TDO_FAST_MATH)
            target_compile_options(${name} PRIVATE /fp:fast)
        endif()
        if(TDO_LTO)
            target_compile_options(${name} PRIVATE $<$<CONFIG:Release>:/GL>)
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
# ---------------------------------------------------------------------------------------------
function(td_plugin_add_runtime_dll name)
    foreach(_dll IN LISTS ARGN)
        if(NOT EXISTS "${_dll}")
            message(WARNING "td_plugin_add_runtime_dll(${name}): ${_dll} does not exist")
            continue()
        endif()
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
#   expects <ROOT>/include/fftw3.h, <ROOT>/lib/libfftw3f-3.{def,lib}, <ROOT>/bin|lib/libfftw3f-3.dll
#   generates the MSVC import library from the .def when missing.
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_fftw3 name)
    cmake_parse_arguments(TDF "" "ROOT;PRECISION" "" ${ARGN})
    if(NOT TDF_PRECISION)
        set(TDF_PRECISION f)
    endif()
    if(TDF_PRECISION STREQUAL "f")
        set(_lib libfftw3f-3)
    elseif(TDF_PRECISION STREQUAL "d")
        set(_lib libfftw3-3)
    else()
        message(FATAL_ERROR "td_plugin_use_fftw3: PRECISION must be f or d")
    endif()
    if(NOT TDF_ROOT)
        _td_find_3rdparty(TDF_ROOT fftw3)
    endif()
    if(NOT TDF_ROOT)
        message(FATAL_ERROR "td_plugin_use_fftw3(${name}): fftw3 not found (looked in 3rdParty/fftw3 of plugin, PluginBuilder and TD samples)")
    endif()

    find_path(FFTW3_INCLUDE_DIR NAMES fftw3.h PATHS "${TDF_ROOT}/include" "${TDF_ROOT}" NO_DEFAULT_PATH)
    if(MSVC AND EXISTS "${TDF_ROOT}/lib/${_lib}.def" AND NOT EXISTS "${TDF_ROOT}/lib/${_lib}.lib")
        find_program(MSVC_LIB_EXE lib.exe)
        if(MSVC_LIB_EXE)
            message(STATUS "Generating ${_lib}.lib from ${_lib}.def")
            execute_process(COMMAND "${MSVC_LIB_EXE}" /nologo /machine:x64 "/def:${TDF_ROOT}/lib/${_lib}.def" "/out:${TDF_ROOT}/lib/${_lib}.lib"
                            WORKING_DIRECTORY "${TDF_ROOT}/lib" RESULT_VARIABLE _r)
            if(NOT _r EQUAL 0)
                message(WARNING "lib.exe failed to generate ${_lib}.lib (exit ${_r})")
            endif()
        endif()
    endif()
    find_library(FFTW3_LIBRARY NAMES ${_lib} PATHS "${TDF_ROOT}/lib" "${TDF_ROOT}" NO_DEFAULT_PATH)
    find_file(FFTW3_DLL NAMES ${_lib}.dll PATHS "${TDF_ROOT}/bin" "${TDF_ROOT}/lib" "${TDF_ROOT}" NO_DEFAULT_PATH)

    if(NOT FFTW3_INCLUDE_DIR OR NOT FFTW3_LIBRARY)
        message(FATAL_ERROR "td_plugin_use_fftw3(${name}): fftw3.h or ${_lib}.lib not found under ${TDF_ROOT}")
    endif()
    message(STATUS "FFTW3: ${FFTW3_LIBRARY}  (dll: ${FFTW3_DLL})")
    target_include_directories(${name} PRIVATE "${FFTW3_INCLUDE_DIR}")
    target_link_libraries(${name} PRIVATE "${FFTW3_LIBRARY}")
    target_compile_definitions(${name} PRIVATE TD_PLUGIN_HAS_FFTW3=1)
    if(FFTW3_DLL)
        td_plugin_add_runtime_dll(${name} "${FFTW3_DLL}")
    endif()
    set(FFTW3_INCLUDE_DIR "${FFTW3_INCLUDE_DIR}" PARENT_SCOPE)
    set(FFTW3_LIBRARY "${FFTW3_LIBRARY}" PARENT_SCOPE)
    set(FFTW3_DLL "${FFTW3_DLL}" PARENT_SCOPE)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_cuda — TouchDesigner 2025 samples ship against CUDA 12.x
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_cuda name)
    find_package(CUDAToolkit REQUIRED)
    if(NOT CMAKE_CUDA_ARCHITECTURES)
        # Turing .. Blackwell; use "native" for a dev-only build of the current GPU
        set(CMAKE_CUDA_ARCHITECTURES 75 80 86 89 90 120 PARENT_SCOPE)
        set_property(TARGET ${name} PROPERTY CUDA_ARCHITECTURES 75 80 86 89 90 120)
    endif()
    message(STATUS "CUDA Toolkit ${CUDAToolkit_VERSION}: ${CUDAToolkit_INCLUDE_DIRS}")
    target_include_directories(${name} PRIVATE ${CUDAToolkit_INCLUDE_DIRS})
    target_link_libraries(${name} PRIVATE CUDA::cudart)
    set_target_properties(${name} PROPERTIES CUDA_SEPARABLE_COMPILATION OFF)
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_use_python — embedded CPython headers/import lib (vendored or from TD samples)
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_python name)
    _td_find_3rdparty(_py Python)
    if(NOT _py)
        message(FATAL_ERROR "td_plugin_use_python(${name}): 3rdParty/Python not found")
    endif()
    target_include_directories(${name} PRIVATE "${_py}/Include" "${_py}/Include/PC")
    file(GLOB _pylibs "${_py}/lib/x64/python3*.lib")
    list(FILTER _pylibs EXCLUDE REGEX "python3\\.lib$")
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
# ---------------------------------------------------------------------------------------------
function(td_plugin_use_opencv name)
    cmake_parse_arguments(TDC "" "" "COMPONENTS" ${ARGN})
    _td_find_3rdparty(_cv opencv)
    if(NOT _cv)
        message(FATAL_ERROR "td_plugin_use_opencv(${name}): 3rdParty/opencv not found")
    endif()
    target_include_directories(${name} PRIVATE "${_cv}/include")
    file(GLOB _cvlibs "${_cv}/lib/Win64/*.lib" "${_cv}/lib/*.lib")
    list(FILTER _cvlibs EXCLUDE REGEX "d\\.lib$")
    target_link_libraries(${name} PRIVATE ${_cvlibs})
    file(GLOB _cvdlls "${_cv}/bin/*.dll" "${_cv}/lib/Win64/*.dll")
    if(_cvdlls)
        td_plugin_add_runtime_dll(${name} ${_cvdlls})
    endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# td_plugin_add_test / td_plugin_add_bench — headless executables that reuse the plugin sources
#   (no TouchDesigner needed). The plugin's include dirs / definitions are inherited.
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
    if(TDT_LINK)
        target_link_libraries(${target} PRIVATE ${TDT_LINK})
    endif()
    set_target_properties(${target} PROPERTIES FOLDER "${kind}")
endfunction()

function(td_plugin_add_test name target)
    enable_testing()
    _td_add_console(${name} ${target} test ${ARGN})
    add_test(NAME ${target} COMMAND ${target} WORKING_DIRECTORY "$<TARGET_FILE_DIR:${target}>")
endfunction()

function(td_plugin_add_bench name target)
    _td_add_console(${name} ${target} bench ${ARGN})
endfunction()
