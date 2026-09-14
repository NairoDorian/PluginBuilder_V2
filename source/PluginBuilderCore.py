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

FAMILIES = ('CHOP', 'TOP', 'DAT', 'SOP', 'POP')

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
    """Return (ok, message). A plugin name is used as CMake target, C++ class name, file name and DLL name."""
    if not name:
        return False, 'Plugin name is empty.'
    if not _NAME_RX.match(name):
        return False, ("Plugin name must start with a letter and contain only letters, digits or '_' "
                       f"(got '{name}').")
    if name.lower() in ('build', 'source', 'test', 'tests', 'cmake', 'all', 'install', 'clean'):
        return False, f"'{name}' clashes with a reserved build target name."
    return True, ''


def sanitize_op_type(name, fallback='Customop'):
    """TouchDesigner opType rule: first char A-Z, remaining chars a-z0-9 only."""
    alnum = re.sub(r'[^A-Za-z0-9]', '', name or '')
    if not alnum:
        return fallback
    alnum = alnum.lower()
    # opType must start with a letter
    alnum = re.sub(r'^[0-9]+', '', alnum) or fallback.lower()
    return alnum[0].upper() + alnum[1:]


def make_op_icon(name):
    alnum = re.sub(r'[^A-Za-z0-9]', '', name or '').upper()
    return (alnum[:3] or 'CST').ljust(3, 'X')


def td_project_name(project_name):
    """'Plugin_FFT.94.toe' -> 'Plugin_FFT', 'my.project.toe' -> 'my.project', 'demo' -> 'demo'."""
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
    if value is None:
        return ''
    value = value.strip().strip('"')
    value = value.replace('${USER_PATH}', user_home).replace('%USERPROFILE%', user_home)
    return os.path.normpath(os.path.expandvars(value)) if value else ''


def parse_settings(text, user_home):
    """Parse settings.ini text into a dict. Never raises; problems are returned in ['errors']."""
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


def _run_quiet(args, timeout=20):
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa: BLE001
        return -1, '', str(e)


def find_visual_studio():
    """Return (installationPath, displayName) of the newest VS with the C++ x64 toolset, or (None, None)."""
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
    """Resolve vcvarsall / ninja / cmake. settings.ini values win; otherwise vswhere + PATH + VS bundled tools."""
    info = {'vcvarsall': '', 'ninja_dir': '', 'cmake_dir': '', 'vs_path': '', 'sources': {}, 'errors': []}
    vs_path, vs_name = find_visual_studio()
    info['vs_path'] = vs_path or ''

    # vcvarsall
    cand = settings_paths.get('VCVarsall', '')
    if cand and os.path.exists(cand):
        info['vcvarsall'], info['sources']['vcvarsall'] = cand, 'settings.ini'
    elif vs_path and os.path.exists(os.path.join(vs_path, 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')):
        info['vcvarsall'] = os.path.join(vs_path, 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')
        info['sources']['vcvarsall'] = f'vswhere ({vs_name})'
    else:
        info['errors'].append('vcvarsall.bat not found (install Visual Studio C++ tools or set [Paths] VCVarsall).')

    # ninja
    cand = settings_paths.get('NinjaDir', '')
    bundled_ninja = os.path.join(vs_path, 'Common7', 'IDE', 'CommonExtensions', 'Microsoft', 'CMake', 'Ninja') if vs_path else ''
    which_ninja = shutil.which('ninja')
    if cand and os.path.exists(os.path.join(cand, 'ninja.exe')):
        info['ninja_dir'], info['sources']['ninja'] = cand, 'settings.ini'
    elif which_ninja:
        info['ninja_dir'], info['sources']['ninja'] = os.path.dirname(which_ninja), 'PATH'
    elif bundled_ninja and os.path.exists(os.path.join(bundled_ninja, 'ninja.exe')):
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
    bundled_cmake = os.path.join(vs_path, 'Common7', 'IDE', 'CommonExtensions', 'Microsoft', 'CMake', 'CMake', 'bin') if vs_path else ''
    which_cmake = shutil.which('cmake')
    if cand and os.path.exists(os.path.join(cand, 'cmake.exe')):
        info['cmake_dir'], info['sources']['cmake'] = cand, 'settings.ini'
    elif which_cmake:
        info['cmake_dir'], info['sources']['cmake'] = os.path.dirname(which_cmake), 'PATH'
    elif bundled_cmake and os.path.exists(os.path.join(bundled_cmake, 'cmake.exe')):
        info['cmake_dir'], info['sources']['cmake'] = bundled_cmake, 'Visual Studio bundled'
    return info


def capture_vc_env(vcvarsall, arch='x64', extra_path_dirs=(), extra_env=None, timeout=120):
    """Run vcvarsall once and return the resulting environment dict (or raise RuntimeError)."""
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
    """Discover templates: templates/<Name>/template.json (preferred) or legacy folder layout."""
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
    return {
        '#__OP_TYPE__#': op_type,
        '#__OP_LABEL__#': op_label,
        '#__OP_ICON__#': op_icon,
        '#__OP_AUTHOR__#': author,
        '#__OP_EMAIL__#': email,
        '#__OP_HELPURL__#': help_url or '',
        '#__PLUGIN_NAME__#': plugin_name,
    }


def render_source(text, replace_name, plugin_name, replacements=None):
    """Rename the template class/file identifiers and substitute metadata placeholders."""
    text = text.replace(replace_name, plugin_name)
    text = text.replace(replace_name.upper(), plugin_name.upper())
    for k, v in (replacements or {}).items():
        text = text.replace(k, v)
    return text


# =================================================================================================
#  Manifests
# =================================================================================================

MANIFEST_NAME = 'plugin.json'


def plugin_type_from_cmake_header(first_line):
    """Parse the legacy '# {'plugin_type': 'CHOP'}' header. Returns family or None."""
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
    """Return manifest dict for a project (plugin.json, else derived from CMakeLists header). None if not a project."""
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
    """Parse compiler/linker/CMake/Ninja output lines into dicts: {severity, file, line, col, code, message, raw}."""
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


def safe_replace_file(src, dst, keep_old=True):
    """
    Replace dst with src using the Windows rename-in-place trick.

    A DLL loaded by TouchDesigner can't be overwritten or deleted, but it *can* be renamed. So the
    existing dst is renamed to dst + '.old' (a previous .old is removed first if possible), then
    src is copied to dst. Returns (changed: bool, note: str).
    """
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    if os.path.exists(dst):
        try:
            if file_sha256(src) == file_sha256(dst):
                return False, 'unchanged'
        except OSError:
            pass
        old = dst + '.old'
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                # still mapped by a previous incarnation; use a numbered fallback
                i = 1
                while os.path.exists(f'{old}{i}') and i < 50:
                    try:
                        os.remove(f'{old}{i}')
                    except OSError:
                        i += 1
                        continue
                    break
                old = f'{old}{i}'
        os.replace(dst, old)
        note = 'replaced (previous renamed to .old)'
    else:
        note = 'new'
    shutil.copy2(src, dst)
    if not keep_old:
        try:
            os.remove(dst + '.old')
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
    """API versions of the headers PluginBuilder compiles against."""
    versions = {}
    for fam in FAMILIES:
        (f, v), _ = _api_from_header(os.path.join(include_dir, f'{fam}_CPlusPlusBase.h'))
        if f:
            versions[f] = v
    _, common = _api_from_header(os.path.join(include_dir, 'CPlusPlus_Common.h'))
    versions['Common'] = common
    return versions


def installed_sdk_versions(td_samples_dir):
    """API versions shipped by the running TouchDesigner (Samples/CPlusPlus)."""
    versions = {}
    layout = {'CHOP': 'CHOP', 'DAT': 'DAT', 'SOP': 'SimpleShapesSOP', 'TOP': 'CudaTOP', 'POP': 'SimpleShapesPOP'}
    for fam, sub in layout.items():
        (f, v), _ = _api_from_header(os.path.join(td_samples_dir, sub, f'{fam}_CPlusPlusBase.h'))
        if f:
            versions[f] = v
    _, common = _api_from_header(os.path.join(td_samples_dir, 'CHOP', 'CPlusPlus_Common.h'))
    versions['Common'] = common
    return versions


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

# Pulsable/structural pars whose value must never be snapshotted/restored by the reload machinery.
LOADER_PARS_RELOAD_CONTROL = frozenset({
    'plugin', 'unloadplugin', 'reinit', 'reinitpulse',
    'callbacks', 'language', 'commonrenamefrom', 'commonrenameto', 'pageindex',
    'renamefrom', 'renameto',
})

# Mode sentinels exchanged with PluginBuilderExt so the Core never imports td.ParMode.
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
    outputresolution, ...), and excludes Pulse pars plus the reload-control/structural pars.
    The `style` argument (e.g. 'Pulse') is enough to decide; ParMode is irrelevant here."""
    lname = (name or '').lower()
    if not lname or style == 'Pulse':
        return False
    if lname in LOADER_PARS_RELOAD_CONTROL:
        return False
    is_custom = bool(is_custom) or (bool(name) and name[0].isupper())
    return lname in BUILTIN_LOADER_PARS or is_custom


def snapshot_par(name, style, mode, val, expr, bind_expr):
    """Reduce a single parameter to a (name, kind, value) snapshot record, or None to skip.
    `mode` must be one of the PAR_* sentinels returned by normalize_par_mode. For menus the captured
    value is the entry NAME, so later index shifts are harmless."""
    if style == 'Pulse':
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
    verbatim across translation units). Order otherwise preserved."""
    prev = _SENTINEL = object()
    for item in items:
        if item != prev:
            yield item
            prev = item


# =================================================================================================
#  IDE integration
# =================================================================================================

def render_vscode_files(plugin_name, td_exe, toe_path, project_folder, sdk_include_dir, extra_includes=()):
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

CREATE_NO_WINDOW = 0x08000000


class WindowsJobObject:
    """Kill-on-close job object so cmake/ninja/cl children die with the runner (Windows only, best effort)."""

    def __init__(self):
        self.handle = None
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
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

    def close(self):
        if self.handle:
            try:
                self._k32.CloseHandle(self.handle)   # KILL_ON_JOB_CLOSE terminates remaining children
            except Exception:  # noqa: BLE001
                pass
            self.handle = None


class BuildJob:
    _next_id = 1

    def __init__(self, kind, args, cwd, label='', on_done=None, env_extra=None):
        self.id = BuildJob._next_id
        BuildJob._next_id += 1
        self.kind = kind            # 'configure' | 'build' | 'clean' | 'test' | 'custom'
        self.args = list(args)
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
        self._jobs.put(job)
        return job

    def cancel_pending(self):
        n = 0
        try:
            while True:
                j = self._jobs.get_nowait()
                if j is not None:
                    j.cancelled = True
                    j.returncode = -2
                    self._events.put(('done', j))
                    n += 1
        except queue.Empty:
            pass
        with self._lock:
            self.pending_count = 0
        return n

    def kill_current(self):
        p = self._proc
        if p is not None and p.poll() is None:
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
            if job.cancelled:
                continue
            self._run_job(job)

    def _run_job(self, job):
        self._current = job
        job.started = time.perf_counter()
        self._events.put(('start', job))
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
