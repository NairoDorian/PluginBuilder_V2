"""
PluginBuilderCore.py — TouchDesigner-independent logic used by PluginBuilderExt.

Everything in this module is importable and testable with a plain Python interpreter
(``python -m unittest discover tests``). It must NOT import ``td`` or touch TouchDesigner
globals (``op``, ``run``, ``project`` ...). PluginBuilderExt is the thin adapter between
TouchDesigner and this module.

Contents
--------
- naming / validation      validate_plugin_name, sanitize_op_type, make_op_icon, td_project_name
- settings                 parse_settings, expand_user_path, discover_toolchain, capture_vc_env
- templates                load_templates, render_source, template_replacements
- manifests                read_plugin_manifest, write_plugin_manifest, plugin_type_from_cmake_header
- diagnostics              parse_diagnostics (MSVC / linker / CMake / Ninja)
- build execution          BuildJob, BuildRunner (background worker + main-thread poll), WindowsJobObject
- files                    file_sha256, safe_replace_file (rename-in-place hot swap)
- sdk                      read_sdk_versions, installed_sdk_versions
- parameters               par_signature, BUILTIN_LOADER_PARS, is_persisted_par,
                            snapshot_par, restore_action, parameter_definition_diff
- IDE                      render_vscode_files

Contracts (stable, depended on by Plugin_FFT and tests/test_core.py)
-------------------------------------------------------------------
* PLUGIN_BUILDER_DIR / td_add_plugin / td_plugin_use_fftw3 live in cmake/TDPlugin.cmake;
  generated CMakeLists.txt only includes them (see CMakeBlocks.assemble).
* plugin.json is the primary manifest (family/name/features); the legacy
  "# {'plugin_type': '...'}" CMakeLists first-line header is still parsed for old
  projects (plugin_type_from_cmake_header, read_plugin_manifest).
* Deploy: built DLL -> __Plugins__/<name>/ via rename-in-place (safe_replace_file;
  the standalone path is cmake/TDDeploy.cmake) so a DLL already mapped by
  TouchDesigner can still be replaced (Windows allows renaming loaded DLLs).
* Builds run under PLUGINBUILDER_BUILD=1 (set by PluginBuilderExt._env_factory);
  when that env is set, TDPlugin.cmake skips its own POST_BUILD deploy.
* Sibling repo ../Plugin_FFT is a consumer of this SDK contract (FFTW3 3.3.11-avx2
  DYNAMIC layout, API-10 headers). It is referenced only for context — never modified here.

Known facts: API 10 headers; toolchain = CMake + Ninja + MSVC; 29 unit tests in
tests/test_core.py must stay green; version v2.1.4. 2.1.1 added hot-reload
parameter-definition refresh (_reload_loader_fresh, discovered via Plugin_FFT's
Planner=Patient menu changing at runtime).
"""

import configparser
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

# Plugin families supported end-to-end (template -> CMake FAMILY -> cplusplus loader op).
# POP is guarded at the TD-adapter layer: older TouchDesigner builds have no cplusplusPOP
# (see PluginBuilderExt.loader_op_map).
FAMILIES = ('CHOP', 'TOP', 'DAT', 'SOP', 'POP')

# Fallback when a template/ has no template.json (pre-manifest layout kept for
# backward compatibility; load_templates prefers template.json when present).
_LEGACY_TEMPLATE_MAP = {
    'BasicCHOP':           {'family': 'CHOP', 'features': []},
    'CHOPWithPythonClass': {'family': 'CHOP', 'features': ['python']},
    'CPUMemoryTOP':        {'family': 'TOP',  'features': []},
    'CudaTOP':             {'family': 'TOP',  'features': ['cuda']},
    'BasicDAT':            {'family': 'DAT',  'features': []},
    'SimpleShapesSOP':     {'family': 'SOP',  'features': []},
    'SimpleShapesPOP':     {'family': 'POP',  'features': []},
    'CudaPOP':             {'family': 'POP',  'features': ['cuda']},
}

# =================================================================================================
#  Naming
# =================================================================================================

_NAME_RX = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')


def validate_plugin_name(name):
    """Return (ok, message). A plugin name is used as CMake target, C++ class name, file name and DLL name.

    Must be a valid CMake target / filesystem identifier: start with a letter, then
    letters/digits/'_' only. Reserved words that would collide with generated
    CMake targets or folder names (build, source, test, ...) are rejected —
    a project dir PluginProjects/<name> plus build/ and source/ subfolders is
    created under that name (create_plugin).
    """
    if not name:
        return False, 'Plugin name is empty.'
    if not _NAME_RX.match(name):
        return False, ("Plugin name must start with a letter and contain only letters, digits or '_' "
                       f"(got '{name}').")
    if name.lower() in ('build', 'source', 'test', 'tests', 'cmake', 'all', 'install', 'clean'):
        return False, f"'{name}' clashes with a reserved build target name."
    return True, ''


def sanitize_op_type(name, fallback='Customop'):
    """TouchDesigner opType rule: first char A-Z, remaining chars a-z0-9 only.

    opType becomes the C++ class-facing type string written into
    Fill*PluginInfo via the #__OP_TYPE__# placeholder (template_replacements).
    Pure punctuation/digits -> fallback. Example: 'FFT_v2' -> 'Fftv2'.
    """
    alnum = re.sub(r'[^A-Za-z0-9]', '', name or '')
    if not alnum:
        return fallback
    alnum = alnum.lower()
    # opType must start with a letter
    alnum = re.sub(r'^[0-9]+', '', alnum) or fallback.lower()
    return alnum[0].upper() + alnum[1:]


def make_op_icon(name):
    """3-letter node icon derived from the plugin name (always exactly 3 chars, A-Z0-9).

    Empty/non-alnum -> 'CST'; short names are right-padded with 'X' (e.g. 'a' -> 'AXX').
    Written into Fill*PluginInfo via #__OP_ICON__# (template_replacements).
    """
    alnum = re.sub(r'[^A-Za-z0-9]', '', name or '').upper()
    return (alnum[:3] or 'CST').ljust(3, 'X')


def td_project_name(project_name):
    """Strip .toe and TD's autosave numeric suffix: 'Plugin_FFT.94.toe' -> 'Plugin_FFT',
    'my.project.toe' -> 'my.project', 'demo' -> 'demo'.

    Used for launch.vs.json / .vscode paths that must name the .toe next to the
    project (PluginBuilderExt._write_launch_vs_json, _write_vscode_files).
    """
    base = project_name or ''
    if base.lower().endswith('.toe'):
        base = base[:-4]
    while True:
        m = re.match(r'^(.*)\.\d+$', base)
        if not m:
            break
        base = m.group(1)
    return base


# =================================================================================================
#  Settings & toolchain
# =================================================================================================

def expand_user_path(value, user_home):
    """Expand ${USER_PATH} / %USERPROFILE% and shell vars, then normalize.

    settings.ini lives under %APPDATA%/IntentDev/PluginBuilder/ and commonly
    writes ${USER_PATH}/... so the same file works across machines.
    """
    if value is None:
        return ''
    value = value.strip().strip('"')
    value = value.replace('${USER_PATH}', user_home).replace('%USERPROFILE%', user_home)
    return os.path.normpath(os.path.expandvars(value)) if value else ''


def parse_settings(text, user_home):
    """Parse settings.ini text into a dict. Never raises; problems are returned in ['errors'].

    Sections: [Paths] (PluginBuilderDir, NinjaDir, VCVarsall, CMakeDir, SdkIncludeDir),
    [PluginInfo] (Author/Email/HelpURL -> #__OP_*__# placeholders), [Options]
    (LogLevel, Arch, ParallelJobs, MirrorPages). Presence of a [DevMode] section
    sets result['dev_mode'] (keeps Create pars enabled after create_plugin).
    Called from PluginBuilderExt.__init__ and dev/ci.py:load_settings.
    """
    result = {
        'paths': {'PluginBuilderDir': '', 'NinjaDir': '', 'VCVarsall': '', 'CMakeDir': '', 'SdkIncludeDir': ''},
        'plugininfo': {'Author': 'Author Name', 'Email': 'email@email.com', 'HelpURL': ''},
        'options': {'LogLevel': 'info', 'Arch': 'x64', 'ParallelJobs': '', 'MirrorPages': 'true'},
        'dev_mode': False,
        'errors': [],
    }
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read_string(text or '')
    except configparser.Error as e:
        result['errors'].append(f'settings.ini parse error: {e}')
        return result
    for section, keys in (('Paths', result['paths']), ('PluginInfo', result['plugininfo']), ('Options', result['options'])):
        if cfg.has_section(section):
            for k in list(keys.keys()):
                for candidate in (k, k.lower()):
                    if cfg.has_option(section, candidate):
                        keys[k] = cfg.get(section, candidate)
                        break
    for k in ('PluginBuilderDir', 'NinjaDir', 'VCVarsall', 'CMakeDir', 'SdkIncludeDir'):
        result['paths'][k] = expand_user_path(result['paths'][k], user_home)
    result['dev_mode'] = cfg.has_section('DevMode')
    return result


def _vswhere_path():
    for env in ('ProgramFiles(x86)', 'ProgramFiles'):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, 'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
            if os.path.exists(p):
                return p
    return None


# Hidden console window so vswhere/child tools don't flash over the TD UI
# (same flag used by BuildRunner / capture_vc_env).
CREATE_NO_WINDOW = 0x08000000


def _run_quiet(args, timeout=20):
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa: BLE001
        return -1, '', str(e)


def find_visual_studio():
    """Return (installationPath, displayName) of the newest VS with the C++ x64 toolset, or (None, None).

    Uses vswhere.exe (ships with VS Installer). Pre-release installs are included
    so preview VS builds work. Consumed by discover_toolchain.
    """
    vswhere = _vswhere_path()
    if not vswhere:
        return None, None
    code, out, _ = _run_quiet([vswhere, '-latest', '-products', '*', '-prerelease',
                               '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                               '-property', 'installationPath'])
    path = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else None
    if not path:
        return None, None
    _, name, _ = _run_quiet([vswhere, '-latest', '-products', '*', '-prerelease', '-property', 'displayName'])
    return path, (name.strip() or 'Visual Studio')


def discover_toolchain(settings_paths, extra_search_dirs=()):
    """Resolve vcvarsall / ninja / cmake. settings.ini values win; otherwise vswhere + PATH + VS bundled tools.

    Returns info = {'vcvarsall','ninja_dir','cmake_dir','vs_path','sources','errors'} where
    'sources' maps each tool to where it was found ('settings.ini' | 'vswhere (...)' |
    'PATH' | 'Visual Studio bundled' | 'search dir') for the [Init] log.
    Called from PluginBuilderExt.check_paths and dev/ci.py:main.
    """
    info = {'vcvarsall': '', 'ninja_dir': '', 'cmake_dir': '', 'vs_path': '', 'sources': {}, 'errors': []}
    # 2.2.1: vswhere is run only when something actually needs the Visual Studio location. It is two
    # child processes (~250 ms measured) and this runs on TouchDesigner's main thread at every extension
    # init, so a settings.ini that names its tools (or tools on PATH) now costs no vswhere at all.
    _vs = {}

    def vs():
        if 'path' not in _vs:
            _vs['path'], _vs['name'] = find_visual_studio()
            info['vs_path'] = _vs['path'] or ''
        return _vs['path'], _vs['name']

    # vcvarsall
    cand = settings_paths.get('VCVarsall', '')
    if cand and os.path.exists(cand):
        info['vcvarsall'], info['sources']['vcvarsall'] = cand, 'settings.ini'
    else:
        vs_path, vs_name = vs()
        if vs_path and os.path.exists(os.path.join(vs_path, 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')):
            info['vcvarsall'] = os.path.join(vs_path, 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')
            info['sources']['vcvarsall'] = f'vswhere ({vs_name})'
        else:
            info['errors'].append('vcvarsall.bat not found (install Visual Studio C++ tools or set [Paths] VCVarsall).')

    # ninja
    cand = settings_paths.get('NinjaDir', '')
    which_ninja = shutil.which('ninja')
    if cand and os.path.exists(os.path.join(cand, 'ninja.exe')):
        info['ninja_dir'], info['sources']['ninja'] = cand, 'settings.ini'
    elif which_ninja:
        info['ninja_dir'], info['sources']['ninja'] = os.path.dirname(which_ninja), 'PATH'
    else:
        vs_path, _ = vs()
        bundled_ninja = os.path.join(vs_path, 'Common7', 'IDE', 'CommonExtensions', 'Microsoft', 'CMake', 'Ninja') if vs_path else ''
        if bundled_ninja and os.path.exists(os.path.join(bundled_ninja, 'ninja.exe')):
            info['ninja_dir'], info['sources']['ninja'] = bundled_ninja, 'Visual Studio bundled'
        else:
            for d in extra_search_dirs:
                if os.path.exists(os.path.join(d, 'ninja.exe')):
                    info['ninja_dir'], info['sources']['ninja'] = d, 'search dir'
                    break
    if not info['ninja_dir']:
        info['errors'].append('ninja.exe not found (install Ninja, add it to PATH, or set [Paths] NinjaDir).')

    # cmake (optional: vcvarsall usually adds the VS-bundled CMake to PATH)
    cand = settings_paths.get('CMakeDir', '')
    which_cmake = shutil.which('cmake')
    if cand and os.path.exists(os.path.join(cand, 'cmake.exe')):
        info['cmake_dir'], info['sources']['cmake'] = cand, 'settings.ini'
    elif which_cmake:
        info['cmake_dir'], info['sources']['cmake'] = os.path.dirname(which_cmake), 'PATH'
    else:
        vs_path, _ = vs()
        bundled_cmake = os.path.join(vs_path, 'Common7', 'IDE', 'CommonExtensions', 'Microsoft', 'CMake', 'CMake', 'bin') if vs_path else ''
        if bundled_cmake and os.path.exists(os.path.join(bundled_cmake, 'cmake.exe')):
            info['cmake_dir'], info['sources']['cmake'] = bundled_cmake, 'Visual Studio bundled'
    return info


def capture_vc_env(vcvarsall, arch='x64', extra_path_dirs=(), extra_env=None, timeout=120):
    """Run vcvarsall once and return the resulting environment dict (or raise RuntimeError).

    Shell: `cmd /s /c ""<vcvarsall>" x64 >nul 2>&1 && set"` — stdout is parsed as KEY=VALUE.
    Extra PATH entries (ninja_dir, cmake_dir) are prepended; extra_env is applied last so
    callers can force PLUGINBUILDER_BUILD=1 / PLUGIN_BUILDER_DIR / TD_SAMPLES_DIR
    (see PluginBuilderExt._env_factory, dev/ci.py:main). Raises RuntimeError if the MSVC
    environment did not initialise (no VCToolsInstallDir/VCINSTALLDIR in the dump).
    """
    if not vcvarsall or not os.path.exists(vcvarsall):
        raise RuntimeError(f'vcvarsall.bat not found: {vcvarsall}')
    cmdline = f'cmd.exe /s /c ""{vcvarsall}" {arch} >nul 2>&1 && set"'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        r = subprocess.run(cmdline, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f'vcvarsall failed to run: {e}') from e
    env = {}
    for line in r.stdout.splitlines():
        if '=' in line and not line.startswith('='):
            k, v = line.split('=', 1)
            env[k] = v
    if r.returncode != 0 or 'VCToolsInstallDir' not in env and 'VCINSTALLDIR' not in env:
        raise RuntimeError(f'vcvarsall did not initialise the MSVC environment (exit {r.returncode}). '
                           f'Output: {(r.stdout or r.stderr)[-400:]}')
    path_parts = [d for d in extra_path_dirs if d] + [env.get('PATH', env.get('Path', ''))]
    env['PATH'] = ';'.join(p for p in path_parts if p)
    env.pop('Path', None)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return env


# =================================================================================================
#  Templates
# =================================================================================================

def load_templates(templates_dir):
    """Discover templates: templates/<Name>/template.json (preferred) or legacy folder layout.

    Each entry needs a source/ subfolder and family in FAMILIES (from template.json,
    else _LEGACY_TEMPLATE_MAP). Metadata keys: name, family, replace (class/file stem
    to rename), main_source, features (cuda/python/opencv), description, api_version,
    sources (list of files under source/). Consumed by PluginBuilderExt._populate_template_menu
    and create_plugin; tests/test_core.py:TemplateTests asserts BasicCHOP is present and
    every main_source contains all #__OP_*__# placeholders plus setAPIVersion( (API 10).
    """
    templates = {}
    if not templates_dir or not os.path.isdir(templates_dir):
        return templates
    for name in sorted(os.listdir(templates_dir)):
        tdir = os.path.join(templates_dir, name)
        if not os.path.isdir(os.path.join(tdir, 'source')):
            continue
        meta = {'name': name, 'family': None, 'replace': name, 'main_source': f'{name}.cpp',
                'features': [], 'description': '', 'api_version': None, 'sources': []}
        jpath = os.path.join(tdir, 'template.json')
        if os.path.exists(jpath):
            try:
                with open(jpath, 'r', encoding='utf-8') as f:
                    meta.update(json.load(f))
            except Exception as e:  # noqa: BLE001
                meta['error'] = f'template.json: {e}'
        if not meta.get('family'):
            legacy = _LEGACY_TEMPLATE_MAP.get(name)
            if legacy:
                meta['family'] = legacy['family']
                meta['features'] = legacy['features']
        if meta.get('family') not in FAMILIES:
            continue
        meta['dir'] = tdir
        meta['sources'] = sorted(os.listdir(os.path.join(tdir, 'source')))
        templates[name] = meta
    return templates


def template_replacements(plugin_name, op_type, op_label, op_icon, author, email, help_url=''):
    """Placeholder -> value map substituted into template sources by render_source.

    Placeholders are the #...# tokens written by dev/sync_templates.py:templatize
    (META_RULES). op_type/op_icon come from sanitize_op_type / make_op_icon;
    author/email/help_url come from settings.ini [PluginInfo].
    """
    return {
        '#__OP_TYPE__#': op_type,
        '#__OP_LABEL__#': op_label,
        '#__OP_ICON__#': op_icon,
        '#__OP_AUTHOR__#': author,
        '#__OP_EMAIL__#': email,
        '#__OP_HELPURL__#': help_url or '',
        '#__PLUGIN_NAME__#': plugin_name,
    }


def scaffold_template_files(meta, project_dir, plugin_name, replacements):
    """Materialise a template's files into project_dir. Returns the list of written relative paths.

    * templates/<T>/source/*  -> <project>/source/   (file names renamed like the class)
    * templates/<T>/project/** -> <project>/**       (optional: tests/, bench/, parameters.json, ...)
    * template.json "generate_parameters": true      -> source/Parameters.h/.cpp from parameters.json
      (ParamGen.generate_files), so a template can ship its parameters as a spec
    Every text file goes through render_source (class rename + #__OP_*__# placeholders). Shared by
    PluginBuilderExt.create_plugin and dev/ci.py:scaffold so the two cannot drift apart.
    """
    replace = meta.get('replace', meta['name'])
    written = []
    tdir = meta['dir']
    for sub, dest_sub in (('source', 'source'), ('project', '')):
        src_root = os.path.join(tdir, sub)
        if not os.path.isdir(src_root):
            continue
        for root, _dirs, files in os.walk(src_root):
            for fn in sorted(files):
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, src_root)
                rel_out = os.path.join(dest_sub, os.path.dirname(rel), fn.replace(replace, plugin_name)) if dest_sub \
                    else os.path.join(os.path.dirname(rel), fn.replace(replace, plugin_name))
                dst = os.path.join(project_dir, rel_out)
                os.makedirs(os.path.dirname(dst) or project_dir, exist_ok=True)
                with open(src, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(render_source(text, replace, plugin_name, replacements))
                written.append(rel_out.replace('\\', '/'))
    if meta.get('generate_parameters'):
        spec = os.path.join(project_dir, 'parameters.json')
        if os.path.exists(spec):
            import ParamGen  # noqa: E402  (source/ is on the path wherever Core is)
            ParamGen.generate_files(spec, os.path.join(project_dir, 'source'))
            written += ['source/Parameters.h', 'source/Parameters.cpp']
    return written


def template_cmake_lines(meta, plugin_name):
    """Extra CMakeLists lines a template asks for (template.json "cmake": [...]), with the name filled in."""
    return [ln.replace('__PLUGIN_NAME__', plugin_name) for ln in meta.get('cmake', [])]


def render_source(text, replace_name, plugin_name, replacements=None):
    """Rename the template class/file identifiers and substitute metadata placeholders.

    Order matters: replace_name (e.g. 'BasicCHOP') and its UPPER form are swapped for
    plugin_name first, then #__OP_*__# / #__PLUGIN_NAME__# tokens are filled in.
    Called from create_plugin and dev/ci.py:scaffold.
    """
    text = text.replace(replace_name, plugin_name)
    text = text.replace(replace_name.upper(), plugin_name.upper())
    for k, v in (replacements or {}).items():
        text = text.replace(k, v)
    return text


# =================================================================================================
#  Manifests
# =================================================================================================

# Primary per-project manifest (written by write_plugin_manifest from create_plugin;
# read back by read_plugin_manifest when loading an existing PluginProject).
MANIFEST_NAME = 'plugin.json'


def plugin_type_from_cmake_header(first_line):
    """Parse the legacy '# {'plugin_type': 'CHOP'}' header. Returns family or None.

    First line of generated CMakeLists.txt (CMakeBlocks.HEADER). ast.literal_eval only —
    never exec — so a malicious/odd header cannot run code (test_header_parse covers this).
    """
    import ast
    if not first_line or not first_line.startswith('#'):
        return None
    try:
        info = ast.literal_eval(first_line[1:].strip())
    except Exception:  # noqa: BLE001
        return None
    if isinstance(info, dict):
        fam = info.get('plugin_type')
        return fam if fam in FAMILIES else None
    return None


def read_plugin_manifest(project_dir):
    """Return manifest dict for a project (plugin.json, else derived from CMakeLists header). None if not a project.

    plugin.json wins when family is valid; otherwise the legacy CMakeLists first-line
    header is parsed and a {'legacy': True} stub is returned. None means "not a
    PluginProject" (used by onPluginname to decide create-vs-load).
    """
    jpath = os.path.join(project_dir, MANIFEST_NAME)
    if os.path.exists(jpath):
        try:
            with open(jpath, 'r', encoding='utf-8') as f:
                m = json.load(f)
            if m.get('family') in FAMILIES:
                m.setdefault('name', os.path.basename(project_dir))
                return m
        except Exception:  # noqa: BLE001
            pass
    cml = os.path.join(project_dir, 'CMakeLists.txt')
    if os.path.exists(cml):
        with open(cml, 'r', encoding='utf-8', errors='ignore') as f:
            fam = plugin_type_from_cmake_header(f.readline())
        if fam:
            return {'name': os.path.basename(project_dir), 'family': fam, 'legacy': True}
    return None


def write_plugin_manifest(project_dir, manifest):
    with open(os.path.join(project_dir, MANIFEST_NAME), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')


# =================================================================================================
#  Diagnostics parsing
# =================================================================================================

_RX_MSVC = re.compile(r'^(?P<file>.+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*(?P<sev>fatal error|error|warning)\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<msg>.*)$')
_RX_LINK = re.compile(r'^(?P<file>[^:]*?)\s*:\s*(?P<sev>fatal error|error|warning)\s+(?P<code>LNK\d+)\s*:\s*(?P<msg>.*)$')
_RX_CMAKE = re.compile(r'^CMake (?P<sev>Error|Warning)(?: \(dev\))? at (?P<file>.+?):(?P<line>\d+)')
_RX_CMAKE_PLAIN = re.compile(r'^CMake (?P<sev>Error|Warning):\s*(?P<msg>.*)$')
_RX_NINJA = re.compile(r'^ninja: (?P<sev>error|warning):\s*(?P<msg>.*)$')
_RX_NVCC = re.compile(r'^(?P<file>.+?)\((?P<line>\d+)\)\s*:\s*(?P<sev>error|warning)\s*:\s*(?P<msg>.*)$')


def parse_diagnostics(lines):
    """Parse compiler/linker/CMake/Ninja output lines into dicts: {severity, file, line, col, code, message, raw}.

    Order tried per line: MSVC (file(line,col): error Cxxxx) -> LNK -> "CMake Error at
    file:line" (body grabbed from the next two-space-indented line) -> plain
    "CMake Error: ..." -> "ninja: error:" -> NVCC. Exact duplicates are dropped
    (dedupe_consecutive is for the human-readable rendering path). Used by
    build/compile on_done in PluginBuilderExt and dev/ci.py:build_project;
    tests/test_core.py:DiagnosticsTests covers MSVC+linker and CMake cases.
    """
    diags = []
    seen = set()
    pending_cmake = None
    for raw in lines:
        line = raw.rstrip('\r\n')
        d = None
        m = _RX_MSVC.match(line)
        if m:
            d = {'severity': 'error' if 'error' in m.group('sev') else 'warning', 'file': m.group('file').strip(),
                 'line': int(m.group('line')), 'col': int(m.group('col') or 0), 'code': m.group('code'),
                 'message': m.group('msg').strip()}
        else:
            m = _RX_LINK.match(line)
            if m:
                d = {'severity': 'error' if 'error' in m.group('sev') else 'warning', 'file': m.group('file').strip(),
                     'line': 0, 'col': 0, 'code': m.group('code'), 'message': m.group('msg').strip()}
            else:
                m = _RX_CMAKE.match(line)
                if m:
                    d = {'severity': m.group('sev').lower(), 'file': m.group('file'), 'line': int(m.group('line')),
                         'col': 0, 'code': 'CMAKE', 'message': ''}
                    pending_cmake = d
                else:
                    m = _RX_CMAKE_PLAIN.match(line)
                    if m:
                        d = {'severity': m.group('sev').lower(), 'file': 'CMakeLists.txt', 'line': 0, 'col': 0,
                             'code': 'CMAKE', 'message': m.group('msg').strip()}
                    else:
                        m = _RX_NINJA.match(line)
                        if m:
                            d = {'severity': m.group('sev'), 'file': 'build.ninja', 'line': 0, 'col': 0,
                                 'code': 'NINJA', 'message': m.group('msg').strip()}
                        else:
                            m = _RX_NVCC.match(line)
                            if m:
                                d = {'severity': m.group('sev'), 'file': m.group('file').strip(),
                                     'line': int(m.group('line')), 'col': 0, 'code': 'NVCC',
                                     'message': m.group('msg').strip()}
        if d is None:
            # CMake error bodies follow the header line indented by two spaces
            if pending_cmake is not None and line.startswith('  ') and not pending_cmake['message']:
                pending_cmake['message'] = line.strip()
            elif not line.strip():
                pending_cmake = None
            continue
        d['raw'] = line
        key = (d['severity'], d['file'], d['line'], d['code'], d['message'])
        if key in seen:
            continue
        seen.add(key)
        diags.append(d)
    return diags


def summarize_diagnostics(diags):
    errors = sum(1 for d in diags if d['severity'] == 'error')
    warnings = sum(1 for d in diags if d['severity'] == 'warning')
    return errors, warnings


# =================================================================================================
#  Files
# =================================================================================================

def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def files_identical(a, b):
    """True when a and b have identical content, hashing only when the cheap checks cannot decide.

    Fast paths, in order (no file content read):
      * different sizes                      -> different
      * same size and same mtime (ns)        -> identical  (shutil.copy2 preserves mtime, so a file
                                                deployed by safe_replace_file keeps its source's stamp)
    Only a same-size / different-mtime pair is SHA-256 hashed. This is what keeps a hot reload from
    hashing hundreds of MB of unchanged runtime DLLs (oneMKL is ~520 MB) on TouchDesigner's main thread.
    """
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    if sa.st_size != sb.st_size:
        return False
    # Equal stamps prove nothing for a file written moments ago: NTFS stamps come from a coarse clock
    # (up to ~16 ms), so two quick writes of equal size can share one. Only trust the stamp for files
    # older than 2 s - exactly the large, rarely-changing runtime DLLs the fast path is for.
    if sa.st_mtime_ns == sb.st_mtime_ns and (time.time_ns() - sa.st_mtime_ns) > 2_000_000_000:
        return True
    try:
        return file_sha256(a) == file_sha256(b)
    except OSError:
        return False


def read_runtime_dll_manifest(build_dir, plugin_name):
    """Runtime DLL file names recorded by TDPlugin.cmake's td_plugin_add_runtime_dll, or None.

    CMake writes build/<name>_runtime_dlls.txt (one file name per line). When present, PluginBuilder
    stages exactly these next to the plugin instead of every *.dll found in build/bin/<Config>/ (which
    also picks up anything a user dropped there by hand, e.g. a 520 MB oneMKL runtime for a bench).
    """
    path = os.path.join(build_dir, f'{plugin_name}_runtime_dlls.txt')
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        names = [ln.strip() for ln in f if ln.strip()]
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def coalesce_output(lines, max_lines=200):
    """Join queued output lines into one printable block, capped at max_lines.

    Returns (text, dropped). Printing to the TouchDesigner textport is not free, and a build can emit
    hundreds of lines in one frame; one print per frame with a cap keeps the main thread responsive.
    The full output stays in BuildJob.output (and the parsed diagnostics in the build_errors DAT).
    """
    lines = [ln if ln.endswith('\n') else ln + '\n' for ln in lines]
    if len(lines) <= max_lines:
        return ''.join(lines), 0
    head = lines[:max_lines]
    dropped = len(lines) - max_lines
    return ''.join(head) + f'  ... {dropped} more line(s) this frame (full log in the job output)\n', dropped


def _free_old_slot(dst):
    """Return a path dst.old / dst.old1..old50 that does not exist (removing unlocked leftovers).

    A previous incarnation that is still mapped by TouchDesigner cannot be deleted, only left alone, so
    the first slot that is free (or can be freed) wins. Raises OSError if all 51 slots are locked.
    """
    base = dst + '.old'
    for i in range(0, 51):
        cand = base if i == 0 else f'{base}{i}'
        if not os.path.exists(cand):
            return cand
        try:
            os.remove(cand)
            return cand
        except OSError:
            continue
    raise OSError(f'no free .old slot next to {dst} (51 previous versions still mapped)')


def safe_replace_file(src, dst, keep_old=True):
    """
    Replace dst with src using the Windows rename-in-place trick.

    A DLL loaded by TouchDesigner can't be overwritten or deleted, but it *can* be renamed. So the
    existing dst is renamed to dst + '.old' (a previous .old is removed first if possible), then the
    src is copied to dst. Returns (changed: bool, note: str).

    Hash gate first: identical content short-circuits with (False, 'unchanged') so hot-reload
    does not thrash the loader. If a previous .old is still mapped, falls back to .old1..old50.
    The standalone build path uses the same idea in cmake/TDDeploy.cmake.
    Callers: PluginBuilderExt._do_copy_plugin / install_plugin, tests FileTests.
    """
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    if os.path.exists(dst):
        same = files_identical(src, dst)
        if same:
            return False, 'unchanged'
        old = _free_old_slot(dst)
        os.replace(dst, old)
        note = 'replaced (previous renamed to ' + os.path.basename(old)[len(os.path.basename(dst)):] + ')'
    else:
        old = None
        note = 'new'
    shutil.copy2(src, dst)
    if not keep_old and old:
        try:
            os.remove(old)
        except OSError:
            pass
    return True, note


def cleanup_old_files(directory):
    """Delete unlocked *.old / *.old<N> leftovers. Returns number removed."""
    removed = 0
    if not os.path.isdir(directory):
        return 0
    for fn in os.listdir(directory):
        if re.search(r'\.old\d*$', fn):
            try:
                os.remove(os.path.join(directory, fn))
                removed += 1
            except OSError:
                pass
    return removed


# =================================================================================================
#  SDK versions
# =================================================================================================

_RX_API = re.compile(r'const int (\w+)CPlusPlusAPIVersion\s*=\s*(\d+)')
_RX_COMMON = re.compile(r'const int OP_CommonAPIVersion\s*=\s*(\d+)')


def _api_from_header(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except OSError:
        return None, None
    m = _RX_API.search(text)
    fam, ver = (m.group(1), int(m.group(2))) if m else (None, None)
    c = _RX_COMMON.search(text)
    return (fam, ver), (int(c.group(1)) if c else None)


def read_sdk_versions(include_dir):
    """API versions of the headers PluginBuilder compiles against.

    Reads <include_dir>/<FAMILY>_CPlusPlusBase.h for `<Family>CPlusPlusAPIVersion`
    and CPlusPlus_Common.h for OP_CommonAPIVersion. Current baseline: API 10 family
    headers, Common = 2 (tests/test_core.py:SdkTests asserts CHOP >= 10).
    Compared against the running TD by PluginBuilderExt._check_sdk_versions.
    """
    versions = {}
    for fam in FAMILIES:
        (f, v), _ = _api_from_header(os.path.join(include_dir, f'{fam}_CPlusPlusBase.h'))
        if f:
            versions[f] = v
    _, common = _api_from_header(os.path.join(include_dir, 'CPlusPlus_Common.h'))
    versions['Common'] = common
    return versions


def installed_sdk_versions(td_samples_dir):
    """API versions shipped by the running TouchDesigner (Samples/CPlusPlus).

    Layout differs from include/: SOP headers live under SimpleShapesSOP/, TOP under
    CudaTOP/, POP under SimpleShapesPOP/. Written by dev/sync_templates.py:sync_headers
    into include/sdk_versions.json as well.
    """
    versions = {}
    layout = {'CHOP': 'CHOP', 'DAT': 'DAT', 'SOP': 'SimpleShapesSOP', 'TOP': 'CudaTOP', 'POP': 'SimpleShapesPOP'}
    for fam, sub in layout.items():
        (f, v), _ = _api_from_header(os.path.join(td_samples_dir, sub, f'{fam}_CPlusPlusBase.h'))
        if f:
            versions[f] = v
    _, common = _api_from_header(os.path.join(td_samples_dir, 'CHOP', 'CPlusPlus_Common.h'))
    versions['Common'] = common
    return versions


_RX_TD_DIR = re.compile(r'^TouchDesigner(?:[ ._-]?(\d{4})\.(\d+))?$', re.IGNORECASE)


def find_td_installs(search_roots=None):
    """Return installed TouchDesigner folders, newest build first: [(build_tuple, path), ...].

    TouchDesigner 2025+ installs into versioned folders (``TouchDesigner.2025.33230``); older
    installs used an unversioned ``TouchDesigner`` folder, which sorts last (build (0, 0)).
    Only folders that contain ``Samples/CPlusPlus`` count. Used by dev/sync_templates.py and as the
    TD_SAMPLES_DIR fallback so nothing hard-codes a path that a TD upgrade removes.
    """
    if search_roots is None:
        search_roots = []
        for env in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
            base = os.environ.get(env)
            if base:
                search_roots.append(os.path.join(base, 'Derivative'))
    found = {}
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            m = _RX_TD_DIR.match(name)
            path = os.path.join(root, name)
            if not m or not os.path.isdir(os.path.join(path, 'Samples', 'CPlusPlus')):
                continue
            build = (int(m.group(1)), int(m.group(2))) if m.group(1) else (0, 0)
            found[os.path.normcase(os.path.abspath(path))] = (build, path)
    return sorted(found.values(), key=lambda t: t[0], reverse=True)


def td_build_label(build):
    """(2025, 33230) -> '2025.33230'; (0, 0) -> 'unversioned'."""
    return f'{build[0]}.{build[1]}' if build and build[0] else 'unversioned'


def sdk_mismatch_message(ours, installed):
    diffs = [f'{k}: {ours.get(k)} vs installed {installed.get(k)}'
             for k in sorted(set(ours) | set(installed))
             if installed.get(k) is not None and ours.get(k) != installed.get(k)]
    if not diffs:
        return ''
    return ('SDK headers differ from the running TouchDesigner (' + ', '.join(diffs) +
            '). Run dev/sync_templates.py to resync include/ and templates/.')


# =================================================================================================
#  Parameter mirroring helpers (pure)
# =================================================================================================

def par_signature(entries):
    """entries: iterable of (name, page, style, size, label, menu_names_tuple). Returns a hashable signature."""
    return tuple((str(n), str(p), str(s), int(sz), str(l), tuple(m or ())) for n, p, s, sz, l, m in entries)


# -------------------------------------------------------------------------------------------------
# Reload persistence for loader parameters
#
# Mirrors of the sets in PluginBuilderExt, kept here so the decision logic is unit-testable without
# TouchDesigner. A "persisted par" survives a fresh-node reload; the rest are either reload-control
# (plugin, unloadplugin, reinitpulse, callbacks, ...) or structural (pageindex, common rename).
# -------------------------------------------------------------------------------------------------

BUILTIN_LOADER_PARS = frozenset({
    'unloadplugin', 'plugin', 'reinit', 'reinitpulse',
    'timeslice', 'scope', 'srselect', 'exportmethod',
    'autoexportroot', 'exporttable', 'commonrenamefrom', 'commonrenameto',
    'outputresolution', 'resolutionw', 'resolutionh', 'aspect', 'aspectw', 'aspecth',
    'fill', 'filter', 'coord', 'format', 'pixelformat', 'colorformat',
    'pageindex', 'renamefrom', 'renameto', 'callbacks', 'language',
})

# Pulsable/structural pars whose value must never be snapshotted/restored by the reload
# machinery. Driving these during a reload would immediately re-trigger unload/reinit or
# rename pages out from under the mirror (see PluginBuilderExt._reload_loader_fresh).
LOADER_PARS_RELOAD_CONTROL = frozenset({
    'plugin', 'unloadplugin', 'reinit', 'reinitpulse',
    'callbacks', 'language', 'commonrenamefrom', 'commonrenameto', 'pageindex',
    'renamefrom', 'renameto',
})

# Mode sentinels exchanged with PluginBuilderExt so the Core never imports td.ParMode.
# Ext resolves the real enum once via _par_mode() and passes it to normalize_par_mode.
PAR_CONSTANT, PAR_EXPRESSION, PAR_BIND = 'CONSTANT', 'EXPRESSION', 'BIND'


def normalize_par_mode(mode, par_mode):
    """Map a td.ParMode member onto its string sentinel; anything unrecognised is CONSTANT."""
    if par_mode is None:
        return PAR_CONSTANT
    table = {
        getattr(par_mode, 'EXPRESSION', None): PAR_EXPRESSION,
        getattr(par_mode, 'BIND', None): PAR_BIND,
        getattr(par_mode, 'CONSTANT', None): PAR_CONSTANT,
    }
    return table.get(mode, PAR_CONSTANT)


def is_persisted_par(name, is_custom, style):
    """True if a loader parameter's value should be snapshotted/restored across a reload.

    Includes custom parameters and the built-in *value* parameters (timeslice, scope,
    outputresolution, ...), and excludes Pulse and Header pars plus the reload-control/structural pars.
    The `style` argument (e.g. 'Pulse') is enough to decide; ParMode is irrelevant here."""
    lname = (name or '').lower()
    if not lname or style in ('Pulse', 'Header', 'Header2'):
        return False
    if lname in LOADER_PARS_RELOAD_CONTROL:
        return False
    is_custom = bool(is_custom) or (bool(name) and name[0].isupper())
    return lname in BUILTIN_LOADER_PARS or is_custom


def snapshot_par(name, style, mode, val, expr, bind_expr):
    """Reduce a single parameter to a (name, kind, value) snapshot record, or None to skip.
    `mode` must be one of the PAR_* sentinels returned by normalize_par_mode. For menus the captured
    value is the entry NAME, so later index shifts are harmless.  Pulse and Header pars are
    skipped (return None) — they carry no value."""
    if style in ('Pulse', 'Header', 'Header2'):
        return None
    if mode == PAR_EXPRESSION:
        return (name, 'expr', expr)
    if mode == PAR_BIND:
        return (name, 'bind', bind_expr)
    return (name, 'val', val)


def restore_action(name, kind, value, menu_names):
    """Decide how to apply one snapshot record to a live parameter.

    Returns (action, detail) where action is one of:
      'apply_value'  – set p.val = value
      'apply_expr'   – set p.expr = value (+ mode EXPRESSION)
      'apply_bind'   – set p.bindExpr = value (+ mode BIND)
      'skip'         – do not touch (detail explains why)"""
    if kind == 'expr':
        return ('apply_expr', value)
    if kind == 'bind':
        return ('apply_bind', value)
    if menu_names and value not in menu_names:
        return ('skip', 'entry no longer exists')
    return ('apply_value', value)


def parameter_definition_diff(before, after):
    """before/after: dict[str, tuple[str,...]] of page/menu_names keyed by par name.
    Returns (added, removed, changed_menus) lists (sorted)."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(n for n in after if n in before and after[n] != before[n])
    return added, removed, changed


def dedupe_consecutive(items):
    """Yield items with immediately-repeated duplicates collapsed (msvc repeats whole warning blocks
    verbatim across translation units). Order otherwise preserved.

    Applied to rendered diagnostic strings in PluginBuilderExt.compile_plugin before printing.
    """
    prev = _SENTINEL = object()
    for item in items:
        if item != prev:
            yield item
            prev = item


# =================================================================================================
#  IDE integration
# =================================================================================================

def render_vscode_files(plugin_name, td_exe, toe_path, project_folder, sdk_include_dir, extra_includes=()):
    """Build .vscode/{launch,c_cpp_properties,settings}.json dicts for a new project.

    Attach-to-process + Launch TouchDesigner configs (cppvsdbg), compile_commands.json
    from the Ninja build dir, C++17 / windows-msvc-x64, *.cu/*.cuh as C++.
    Written by PluginBuilderExt._write_vscode_files; covered by VscodeTests.test_render.
    """
    launch = {
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Attach to running TouchDesigner",
                "type": "cppvsdbg",
                "request": "attach",
                "processId": "${command:pickProcess}",
                "symbolSearchPath": "${workspaceFolder}/build/bin/Debug;${workspaceFolder}/build/bin/RelWithDebInfo",
            },
            {
                "name": f"Launch TouchDesigner ({os.path.basename(toe_path)})",
                "type": "cppvsdbg",
                "request": "launch",
                "program": td_exe,
                "args": [toe_path],
                "cwd": project_folder,
                "console": "internalConsole",
                "environment": [{"name": "TOUCH_TEXT_CONSOLE", "value": "1"}],
            },
        ],
    }
    props = {
        "version": 4,
        "configurations": [
            {
                "name": "Win32",
                "compileCommands": "${workspaceFolder}/build/compile_commands.json",
                "includePath": ["${workspaceFolder}/source", sdk_include_dir] + list(extra_includes),
                "defines": ["_WINDOWS", "WIN32", "NOMINMAX", "_USE_MATH_DEFINES", f'TD_PLUGIN_NAME="{plugin_name}"'],
                "intelliSenseMode": "windows-msvc-x64",
                "cppStandard": "c++17",
                "cStandard": "c17",
            }
        ],
    }
    settings = {
        "cmake.buildDirectory": "${workspaceFolder}/build",
        "cmake.generator": "Ninja",
        "files.associations": {"*.cu": "cpp", "*.cuh": "cpp"},
    }
    return {'launch.json': launch, 'c_cpp_properties.json': props, 'settings.json': settings}


# =================================================================================================
#  Build execution
# =================================================================================================

# CREATE_NO_WINDOW is also defined earlier near _run_quiet; kept here so the build
# section reads as one unit (same value, harmless duplicate at class scope).

class WindowsJobObject:
    """Kill-on-close job object so cmake/ninja/cl children die with the runner (Windows only, best effort).

    2.2.1: the kernel32 calls carry explicit ctypes signatures. Without them ctypes passes and returns
    HANDLEs as 32-bit C ints - it only worked because handle values happen to be small. terminate()
    kills every process in the job (the running build's whole tree: cmd -> cmake/ninja -> cl.exe), which
    is what Cancel Build needs; Popen.kill() alone stopped only the direct child and left cl.exe running.
    """

    def __init__(self):
        self.handle = None
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.TerminateJobObject.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            self._k32 = k32
            self.handle = k32.CreateJobObjectW(None, None)
            if not self.handle:
                return

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in
                            ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                             'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                            ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                            ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                            ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                            ('SchedulingClass', wintypes.DWORD)]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IO_COUNTERS),
                            ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                            ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        except Exception:  # noqa: BLE001
            self.handle = None

    def assign(self, pid):
        if not self.handle:
            return False
        try:
            PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
            h = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
            if not h:
                return False
            ok = bool(self._k32.AssignProcessToJobObject(self.handle, h))
            self._k32.CloseHandle(h)
            return ok
        except Exception:  # noqa: BLE001
            return False

    def terminate(self, exit_code=1):
        """Kill every process currently in the job (the job object itself stays usable). True on success.

        The runner runs one job at a time, and a finished build's processes have already left the job,
        so this is exactly "the running build's process tree".
        """
        if not self.handle:
            return False
        try:
            return bool(self._k32.TerminateJobObject(self.handle, int(exit_code)))
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        if self.handle:
            try:
                self._k32.CloseHandle(self.handle)   # KILL_ON_JOB_CLOSE terminates remaining children
            except Exception:  # noqa: BLE001
                pass
            self.handle = None


class BuildJob:
    """One unit of work for BuildRunner (configure / build / clean / test / custom).

    on_done runs on the main thread inside BuildRunner.poll — never from the worker —
    so TouchDesigner callbacks can safely touch ops/params. returncode 0 and not
    cancelled => ok; duration_ms is wall time between start and finish.
    """
    _next_id = 1

    def __init__(self, kind, args, cwd, label='', on_done=None, env_extra=None, func=None):
        self.id = BuildJob._next_id
        BuildJob._next_id += 1
        self.kind = kind            # 'configure' | 'build' | 'clean' | 'test' | 'custom' | 'call'
        self.args = list(args)
        # In-process work (kind 'call'): func(job, emit) runs on the runner thread instead of a
        # subprocess. emit(text) streams a line like a child's stdout; the return value (None/int) is
        # the returncode; anything the caller needs back goes into job.result. Used for file work that
        # must not run on TouchDesigner's main thread (DLL hashing/staging, installs).
        self.func = func
        self.result = None
        self.error = None
        self.cwd = cwd
        self.label = label or kind
        self.on_done = on_done
        self.env_extra = env_extra or {}
        self.output = []
        self.returncode = None
        self.started = None
        self.finished = None
        self.cancelled = False

    @property
    def duration_ms(self):
        if self.started is None or self.finished is None:
            return 0.0
        return (self.finished - self.started) * 1000.0

    @property
    def ok(self):
        return self.returncode == 0 and not self.cancelled


class BuildRunner:
    """
    Runs build jobs one after another on a background thread inside a captured MSVC environment.

    Usage (main thread):
        runner = BuildRunner(env_factory=lambda: capture_vc_env(...), capture_output=True)
        runner.start()
        runner.submit(BuildJob('build', ['ninja', '-C', 'build'], cwd, on_done=cb))
        ...each frame:  for ev in runner.poll(): ...     # dispatches on_done on the calling thread

    Events from poll(): ('env', ok, message) | ('line', job, text) | ('start', job) | ('done', job)

    Shutdown contract: set _stop, cancel pending, kill current, push sentinel None,
    join with timeout, then close the WindowsJobObject (KILL_ON_JOB_CLOSE reaps any
    leftover cmake/ninja/cl children). PluginBuilderExt polls via a one-frame
    run() loop (_schedule_poll / _poll).
    """

    def __init__(self, env_factory, capture_output=True, use_job_object=True, log=None):
        self._env_factory = env_factory
        self._capture = capture_output
        self._use_job = use_job_object
        self._log = log or (lambda *a, **k: None)
        self._events = queue.Queue()
        self._jobs = queue.Queue()
        self._env = None
        self._env_error = None
        self._env_ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._current = None
        self._proc = None
        self._job_obj = WindowsJobObject() if use_job_object else None
        self._lock = threading.Lock()
        self.pending_count = 0
        self._pending = []          # submitted, not yet started (for pending_kinds); guarded by _lock

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name='PluginBuilder-BuildRunner', daemon=True)
        self._thread.start()

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def env_ready(self):
        return self._env_ready.is_set() and self._env is not None

    @property
    def env(self):
        return self._env

    @property
    def busy(self):
        return self._current is not None or self.pending_count > 0

    @property
    def current_job(self):
        return self._current

    def shutdown(self, timeout=3.0):
        self._stop.set()
        self.cancel_pending()
        self.kill_current()
        self._jobs.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        if self._job_obj:
            self._job_obj.close()
            self._job_obj = None

    # ---- submission -------------------------------------------------------------------------
    def submit(self, job):
        with self._lock:
            self.pending_count += 1
            self._pending.append(job)
        self._jobs.put(job)
        return job

    def pending_kinds(self):
        """Kinds of the jobs queued but not started yet, oldest first (e.g. ['configure', 'build'])."""
        with self._lock:
            return [j.kind for j in self._pending]

    def cancel_pending(self):
        """Cancel every job submitted but not started yet. Returns how many were cancelled.

        2.2.2: the cancellation is decided from `_pending` under the lock, not from what is still in the queue.
        A job the worker had already taken out of the queue but not yet started (between `_jobs.get()` and its
        `_pending` bookkeeping) used to escape the cancel and run anyway; now it is marked cancelled here and the
        worker skips it. A shutdown sentinel found in the queue is put back.
        """
        sentinel = False
        try:
            while True:
                if self._jobs.get_nowait() is None:
                    sentinel = True
        except queue.Empty:
            pass
        if sentinel:
            self._jobs.put(None)
        n = 0
        with self._lock:
            for j in self._pending:
                if not j.cancelled:
                    j.cancelled = True
                    j.returncode = -2
                    self._events.put(('done', j))
                    n += 1
            self.pending_count = 0
            self._pending.clear()
        return n

    def kill_current(self):
        """Stop the running subprocess job and its whole process tree. True when something was running.

        2.2.1: with a job object, TerminateJobObject kills the tree (cmd -> cmake/ninja -> cl.exe / link.exe).
        Popen.kill() alone stopped only the direct child, so Cancel Build left the compilers running (holding
        .obj/.pdb locks the next build then tripped over) until the runner itself shut down.
        """
        p = self._proc
        if p is not None and p.poll() is None:
            if self._job_obj is not None:
                self._job_obj.terminate()
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
            return True
        return False

    # ---- polling (main thread) --------------------------------------------------------------
    def poll(self, max_events=10000):
        events = []
        for _ in range(max_events):
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                break
            events.append(ev)
            if ev[0] == 'done':
                job = ev[1]
                if job.on_done:
                    try:
                        job.on_done(job)
                    except Exception as e:  # noqa: BLE001
                        self._log(f'[BuildRunner] on_done error for {job.label}: {e}')
        return events

    # ---- worker -----------------------------------------------------------------------------
    def _worker(self):
        try:
            self._env = self._env_factory()
            self._env_ready.set()
            self._events.put(('env', True, 'MSVC environment ready'))
        except Exception as e:  # noqa: BLE001
            self._env_error = str(e)
            self._env_ready.set()
            self._events.put(('env', False, str(e)))
            return
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                break
            with self._lock:
                self.pending_count = max(0, self.pending_count - 1)
                if job in self._pending:
                    self._pending.remove(job)
            if job.cancelled:
                continue
            self._run_job(job)

    def _run_job(self, job):
        self._current = job
        job.started = time.perf_counter()
        self._events.put(('start', job))
        if job.func is not None:
            def emit(text):
                line = text if text.endswith('\n') else text + '\n'
                job.output.append(line)
                self._events.put(('line', job, line))
            try:
                rc = job.func(job, emit)
                job.returncode = 0 if rc is None else int(rc)
            except Exception as e:  # noqa: BLE001
                job.error = e
                job.returncode = -1
                emit(f'[BuildRunner] {job.label} failed: {e}')
            finally:
                job.finished = time.perf_counter()
                self._current = None
                self._events.put(('done', job))
            return
        env = dict(self._env)
        env.update({k: str(v) for k, v in job.env_extra.items()})
        try:
            popen_kwargs = dict(cwd=job.cwd, env=env, text=True, bufsize=1,
                                creationflags=CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            if self._capture:
                popen_kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding='utf-8', errors='replace')
            self._proc = subprocess.Popen(job.args, **popen_kwargs)
            if self._job_obj:
                self._job_obj.assign(self._proc.pid)
            if self._capture and self._proc.stdout is not None:
                try:
                    for line in iter(self._proc.stdout.readline, ''):
                        job.output.append(line)
                        self._events.put(('line', job, line))
                except (ValueError, OSError):
                    pass
            job.returncode = self._proc.wait()
        except Exception as e:  # noqa: BLE001
            job.returncode = -1
            msg = f'[BuildRunner] failed to run {job.args[0] if job.args else "?"}: {e}\n'
            job.output.append(msg)
            self._events.put(('line', job, msg))
        finally:
            job.finished = time.perf_counter()
            p = self._proc
            if p is not None and p.stdout is not None:
                try:
                    p.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
            self._current = None
            self._events.put(('done', job))
