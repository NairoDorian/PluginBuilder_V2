"""
CMakeBlocks.py — CMakeLists.txt generator for PluginBuilder projects.

Generated projects are deliberately tiny: all shared logic lives in
``PluginBuilder_V2/cmake/TDPlugin.cmake`` (included at configure time), so fixes and
new features reach every existing project on its next configure instead of being
frozen into per-project text.

Placeholders:
  __PLUGIN_TYPE__        : 'CHOP', 'TOP', 'DAT', 'SOP' or 'POP'
  __PLUGIN_NAME__        : plugin / CMake target name
  __PLUGIN_BUILDER_DIR__ : absolute PluginBuilder directory (fallback only — the live value is
                           passed as -DPLUGIN_BUILDER_DIR by PluginBuilder, or taken from the
                           PLUGIN_BUILDER_DIR environment variable)
  __FEATURES__           : space separated td_add_plugin FEATURES (cuda / python / opencv), may be empty
  __EXTRA__              : extra per-template lines (e.g. td_plugin_optimize)
"""

# Header comment (kept for backward compatibility: PluginBuilder reads plugin_type from it;
# plugin.json is the primary manifest for new projects).
HEADER = "# {'plugin_type': __PLUGIN_TYPE__}\n"

BODY = '''cmake_minimum_required(VERSION 3.21)
project(__PLUGIN_NAME__ LANGUAGES CXX)

# ---------------------------------------------------------------------------
# PluginBuilder shared module. PLUGIN_BUILDER_DIR is normally passed by
# PluginBuilder (-D) or set in the environment; the fallback below is the
# location recorded when this project was created.
# ---------------------------------------------------------------------------
if(NOT PLUGIN_BUILDER_DIR AND NOT DEFINED ENV{PLUGIN_BUILDER_DIR})
    set(PLUGIN_BUILDER_DIR __PLUGIN_BUILDER_DIR__)
endif()
if(NOT PLUGIN_BUILDER_DIR)
    set(PLUGIN_BUILDER_DIR "$ENV{PLUGIN_BUILDER_DIR}")
endif()
if(NOT EXISTS "${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake")
    message(FATAL_ERROR "PluginBuilder not found at '${PLUGIN_BUILDER_DIR}'. Pass -DPLUGIN_BUILDER_DIR=<path to PluginBuilder_V2>.")
endif()
include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)

# ---------------------------------------------------------------------------
# The plugin. Add dependencies below, e.g.
#   td_plugin_use_fftw3(__PLUGIN_NAME__)
#   td_plugin_use_opencv(__PLUGIN_NAME__)
#   td_plugin_optimize(__PLUGIN_NAME__ AVX2 FAST_MATH)
#   td_plugin_add_test(__PLUGIN_NAME__ __PLUGIN_NAME___tests SOURCES tests/test_main.cpp)
# ---------------------------------------------------------------------------
td_add_plugin(__PLUGIN_NAME__ FAMILY __PLUGIN_TYPE_BARE__ __FEATURES_KW__)
__EXTRA__'''

FEATURE_EXTRAS = {
    'cuda': '',
    'python': '',
    'opencv': '',
}


def assemble(plugin_name, plugin_type, plugin_builder_dir, features=(), extra_lines=()):
    """Return the CMakeLists.txt text for a new plugin project."""
    features = [f for f in features if f]
    features_kw = f"FEATURES {' '.join(features)}" if features else ''
    extra = ''.join(line.rstrip('\n') + '\n' for line in extra_lines)
    text = HEADER + BODY
    text = text.replace('__PLUGIN_TYPE_BARE__', plugin_type)
    text = text.replace('__PLUGIN_TYPE__', f"'{plugin_type}'")
    text = text.replace('__PLUGIN_NAME__', plugin_name)
    text = text.replace('__PLUGIN_BUILDER_DIR__', f'"{plugin_builder_dir}"')
    text = text.replace('__FEATURES_KW__', features_kw)
    text = text.replace('__EXTRA__', extra)
    # tidy the td_add_plugin line when no features
    text = text.replace(' )', ')')
    return text


# ---------------------------------------------------------------------------
# Backward-compatible module-level strings (older code assembled these directly).
# ---------------------------------------------------------------------------
start_block = HEADER
project_block = ''
cuda_project_block = ''
core_block = BODY
cuda_block = ''
python_block = ''
