"""MIT License

Copyright (c) 2024 Keith Lostracco
Copyright (c) 2026 PluginBuilder_V2 contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# =================================================================================================
# PluginBuilderExt — TouchDesigner adapter around PluginBuilderCore.
#
#   * All TouchDesigner-independent logic (naming, settings, templates, manifests, diagnostics,
#     the build runner, hot-swap file replacement) lives in source/PluginBuilderCore.py and is
#     unit-tested headlessly.  This file only talks to TouchDesigner.
#   * Public entry points called from the .tox (parexec / folder DATs / execute DAT) keep their
#     names:  OnParValueChange, OnParPulse, OnPluginUpdate, OnSourceUpdate, OnCMakeListsUpdate,
#     CheckAndPrintOutput, RefreshDats, PostCreatePlugin, EnableCreatePars, BuildAndCompile,
#     SendCommand, GetOutput, PrintOutput, sync_custom_parameters, create_plugin, build_plugin,
#     compile_plugin, install_plugin, close_subprocess, start_subprocess, clear_plugin_builder.
#
# Log prefixes: [Init] [Create] [Build] [Compile] [Build→Copy] [Install] [Source] [CMake]
#               [Runner] [ParamSync] [Cleanup] [Test]
#
# Contracts: PLUGIN_BUILDER_DIR / td_add_plugin / __Plugins__ deploy / PLUGINBUILDER_BUILD=1
# env and plugin.json schema live in PluginBuilderCore + cmake/TDPlugin.cmake; this file is
# the TD-only surface. Sibling repo ../Plugin_FFT is a consumer of that contract (never
# modified here). Known facts: API 10 headers, CMake+Ninja+MSVC, v2.1.4; v2.1.1 added the
# hot-reload parameter-definition refresh (_reload_loader_fresh).
# =================================================================================================

import importlib.util
import json
import os
import re
import shutil
import sys
import time

# -------------------------------------------------------------------------------------------------
# Module loading: prefer the on-disk source files under <PluginBuilderDir>/source so the .tox
# always runs the current code even when its embedded DATs are stale. Falls back to the DATs.
# -------------------------------------------------------------------------------------------------

def _load_module_from(path, name):
    """Import a .py by filesystem path and register it in sys.modules under `name`.

    Lets the .tox run the on-disk source/PluginBuilderCore.py even when its embedded DAT
    copy is stale (dev loop: edit file -> reload COMP -> new code without re-export).
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


def _import_support_module(name, plugin_builder_dir):
    """Load source/<name>.py when present, else fall back to the embedded DAT module.

    plugin_builder_dir comes from _peek_plugin_builder_dir (pre-parse settings peek).
    Failures loading the file fall back to __import__(name) with an [Init] WARNING.
    """
    candidate = os.path.join(plugin_builder_dir or '', 'source', f'{name}.py')
    if plugin_builder_dir and os.path.exists(candidate):
        try:
            return _load_module_from(candidate, name)
        except Exception as e:  # noqa: BLE001
            print(f'[Init] WARNING: could not load {candidate}: {e}; falling back to DAT module')
    return __import__(name)


# -------------------------------------------------------------------------------------------------
# Reload persistence logic lives in PluginBuilderCore (pure, unit-testable) — see the
# "Parameter mirroring helpers (pure)" section: is_persisted_par / snapshot_par /
# restore_action / parameter_definition_diff. PluginBuilderCore is imported per-instance as
# self.core (see __init__), so these methods can't reference it at module scope; the helpers below
# just resolve the td.ParMode binding once.
# -------------------------------------------------------------------------------------------------


def _par_mode():
    """Return td.ParMode (a TouchDesigner builtin) if available, else None — used to normalize a
    parameter's mode onto the PAR_* sentinels before calling the pure Core helpers.

    Resolution order: module globals (TD injects builtins) then `import td`.
    Module-level on purpose: _snapshot_loader_values / _restore_loader_values call it by bare name.
    """
    pm = globals().get('ParMode')
    if pm is None:
        try:
            import td as _td  # type: ignore
            pm = getattr(_td, 'ParMode', None)
        except Exception:  # noqa: BLE001
            pm = None
    return pm


STATUS_PAGE = 'Status'   # custom page name for Buildstatus / Lastbuild / Loadeddll / pulses


class PluginBuilderExt:
    """
    Creates, builds, compiles, hot-reloads and installs C++ plugins for TouchDesigner.

    Loaded as the COMP's Extension (ownerComp passed by TD). Public method names are part
    of the .tox contract (parexec / folder DAT / execute DAT call them by name) — do not rename.

    Lifecycle:
        1. create_plugin()   — scaffold a project from a template (CMakeLists via TDPlugin.cmake, plugin.json,
                               .vscode/, launch.vs.json) and start the first configure+build
        2. build_plugin()    — cmake configure (Ninja)                      [BuildRunner job]
        3. compile_plugin()  — ninja build                                   [BuildRunner job]
        4. _do_copy_plugin() — hash-gated, rename-in-place copy of the DLL (+ runtime DLLs) into __Plugins__/
                               and re-init of the plugin_loader op; then parameter sync
        5. install_plugin()  — copy to ~/Documents/Derivative/Plugins/<name>/

    Cross-refs: pure logic in PluginBuilderCore (same names); CMake text from CMakeBlocks;
    deploy algorithm shared with cmake/TDDeploy.cmake. Consumer: sibling ../Plugin_FFT.
    """

    # ========================================================================================== #
    #  INITIALIZATION                                                                            #
    #  Runs once per COMP instance (extension constructor). Heavy work is deferred: DAT        #
    #  refresh is delayFrames=120; first sync_custom_parameters() is immediate but cheap       #
    #  when no loader exists yet.                                                              #
    # ========================================================================================== #

    def __init__(self, ownerComp):
        """Wire DATs, settings, Core modules, dispatch maps, toolchain, templates, SDK check."""
        # ownerComp is the PluginBuilder COMP; builder/ holds settings + folder DATs,
        # source/ holds the file-watching DATs used for auto-rebuild.
        self.ownerComp = ownerComp
        self.builderComp = ownerComp.op('builder')
        self.SettingsDat = self.builderComp.op('settings')
        self.folder_binDat = self.builderComp.op('folder_bin')
        self.sourceComp = ownerComp.op('source')
        self.folder_sourceDat = self.sourceComp.op('sync/folder_source') if self.sourceComp else None
        self.CMakeListsDat = self.ownerComp.op('CMakeLists')

        self.user_home = os.environ.get('USERPROFILE', os.environ.get('HOME', ''))

        # --- settings (never raises) ---
        settings_text = self.SettingsDat.text if self.SettingsDat is not None else ''
        pb_dir_hint = self._peek_plugin_builder_dir(settings_text)
        self.core = _import_support_module('PluginBuilderCore', pb_dir_hint)
        self.cmake_blocks = _import_support_module('CMakeBlocks', pb_dir_hint)
        self.settings = self.core.parse_settings(settings_text, self.user_home)
        self.dev_mode = self.settings['dev_mode']
        self.verbose = str(self.settings['options'].get('LogLevel', 'info')).lower() == 'debug'
        self.mirror_pages = str(self.settings['options'].get('MirrorPages', 'true')).lower() not in ('0', 'false', 'no')

        # --- state ---
        self.runner = None                 # BuildRunner (background thread + main-thread poll)
        self.process = None                     # legacy attribute (some callbacks test it)
        self.queue = None
        self.loader_op = self.ownerComp.op('plugin_loader')
        self.open_attempts = 0
        self._build_mtime = None
        self._last_copied_hash = None
        self._last_synced_sig = None
        self._last_synced_pars = None
        self._rebuild_after_current = False
        self._poll_scheduled = False
        self._last_status = ''
        self._last_configure_hash = None
        self._loaded_dll_hash = None

        # --- dispatch maps: parameter name -> handler (OnParValueChange / OnParPulse) ---
        self.on_par_value_change_map = {
            'Outputto': self.onOutputto,
            'Pluginname': self.onPluginname,
        }
        self.on_par_pulse_map = {
            'Createplugin': self.create_plugin,
            'Buildplugin': self.build_plugin,
            'Compileplugin': self.compile_plugin,
            'Closesubprocess': self.close_subprocess,
            'Installplugin': self.install_plugin,
            'Refreshcustompars': lambda: self.sync_custom_parameters(force_rebuild=True, verbose=True),
            'Cancelbuild': self.cancel_build,
            'Cleanbuild': self.clean_build,
            'Runtests': self.run_tests,
            'Reloadplugin': lambda: self._do_copy_plugin(force=True),
            'Generateparams': self.generate_parameters,
            'Bakeplugin': self.bake_plugin,
        }

        # --- loader op types by plugin family (POP guarded: older TD builds have no POPs) ---
        self.loader_op_map = {
            'CHOP': {'loader': cplusplusCHOP, 'in': inCHOP, 'out': outCHOP},
            'TOP':  {'loader': cplusplusTOP,  'in': inTOP,  'out': outTOP},
            'DAT':  {'loader': cplusplusDAT,  'in': inDAT,  'out': outDAT},
            'SOP':  {'loader': cplusplusSOP,  'in': inSOP,  'out': outSOP},
        }
        pop_loader = globals().get('cplusplusPOP')
        if pop_loader is not None:
            self.loader_op_map['POP'] = {'loader': pop_loader, 'in': globals().get('inPOP'), 'out': globals().get('outPOP')}

        # --- directory conventions (relative to project.folder) ---
        self.plugin_projects_dir = 'PluginProjects'
        self.plugins_dir = '__Plugins__'

        # --- toolchain & paths ---
        self.PathsValid = False
        self.toolchain = {'vcvarsall': '', 'ninja_dir': '', 'cmake_dir': '', 'errors': [], 'sources': {}}
        self.check_paths()

        # --- templates ---
        self.templates = self.core.load_templates(self.template_dir) if self.PluginBuilderDir else {}
        self.template_map = {name: {'type': m['family'], 'replace': m.get('replace', name), 'meta': m}
                             for name, m in self.templates.items()}
        self._populate_template_menu()

        # --- SDK version check against the running TouchDesigner ---
        self.sdk_versions = self.core.read_sdk_versions(self.include_dir) if self.PluginBuilderDir else {}
        self._check_sdk_versions()

        # --- status parameters (created dynamically; harmless if they already exist) ---
        self._ensure_status_pars()

        # --- deferred DAT refresh + parameter sync ---
        # 120 frames: let the COMP finish loading before force-cooking folder DATs.
        run("args[0].RefreshDats()", self.ownerComp, delayFrames=120)
        self.sync_custom_parameters()

        self._log('Init', f"PluginBuilderExt initialized (dev_mode={self.dev_mode}, paths_valid={self.PathsValid}, "
                          f"plugin='{self.Pluginname}', templates={len(self.templates)}, sdk={self.sdk_versions})")
        if self.settings['errors']:
            for e in self.settings['errors']:
                self._log('Init', f'WARNING: {e}')
        if not self.PathsValid:
            self._set_status('Toolchain not configured — see textport')

    def __del__(self):
        """Best-effort runner teardown when the extension object is collected."""
        try:
            self.close_subprocess()
        except Exception:  # noqa: BLE001
            pass

    # ========================================================================================== #
    #  LOGGING / STATUS                                                                          #
    #  _log always prints; _debug only when LogLevel=debug; status/errors/warnings also land  #
    #  on the COMP so the UI and script logs stay in sync.                                     #
    # ========================================================================================== #

    @staticmethod
    def _log(tag, message):
        """Always print '[tag] message' to the textport (public log channel)."""
        print(f"[{tag}] {message}")

    def _debug(self, tag, message):
        """Print only when [Options] LogLevel == debug (self.verbose)."""
        if self.verbose:
            print(f"[{tag}] {message}")

    def _ensure_status_pars(self):
        """Create the Status page + Buildstatus/Lastbuild/Loadeddll str + pulse pars if missing.

        Idempotent (skips names that already exist); failures are debug-logged only so a
        locked COMP still finishes __init__.
        """
        try:
            page = None
            for p in self.ownerComp.customPages:
                if p.name == STATUS_PAGE:
                    page = p
                    break
            if page is None:
                page = self.ownerComp.appendCustomPage(STATUS_PAGE)
            existing = {p.name for p in page.pars}
            if 'Buildstatus' not in existing:
                page.appendStr('Buildstatus', label='Build Status')[0].readOnly = True
            if 'Lastbuild' not in existing:
                page.appendStr('Lastbuild', label='Last Build')[0].readOnly = True
            if 'Loadeddll' not in existing:
                page.appendStr('Loadeddll', label='Loaded DLL')[0].readOnly = True
            if 'Cancelbuild' not in existing:
                page.appendPulse('Cancelbuild', label='Cancel Build')
            if 'Cleanbuild' not in existing:
                page.appendPulse('Cleanbuild', label='Clean Build Dir')
            if 'Reloadplugin' not in existing:
                page.appendPulse('Reloadplugin', label='Force Reload Plugin (fresh parameter definitions)')
            if 'Runtests' not in existing:
                page.appendPulse('Runtests', label='Run Tests (ctest)')
            if 'Generateparams' not in existing:
                page.appendPulse('Generateparams', label='Generate Parameters (parameters.json)')
            if 'Bakeplugin' not in existing:
                page.appendPulse('Bakeplugin', label='Bake for Performance (standalone node)')
        except Exception as e:  # noqa: BLE001
            self._debug('Init', f'status pars not created: {e}')

    def _set_status(self, text):
        """Update Buildstatus (also caches _last_status for callers that cannot touch pars)."""
        self._last_status = text
        try:
            self.ownerComp.par.Buildstatus = text
        except Exception:  # noqa: BLE001
            pass

    def _set_error(self, message):
        """Log [Build] ERROR and surface via addError/addScriptError (tries both)."""
        self._log('Build', f'ERROR: {message}')
        for fn in ('addError', 'addScriptError'):
            try:
                getattr(self.ownerComp, fn)(message)
                break
            except Exception:  # noqa: BLE001
                continue

    def _set_warning(self, message):
        """Log [Build] WARNING and surface via addWarning/addScriptWarning."""
        self._log('Build', f'WARNING: {message}')
        for fn in ('addWarning', 'addScriptWarning'):
            try:
                getattr(self.ownerComp, fn)(message)
                break
            except Exception:  # noqa: BLE001
                continue

    def _clear_errors(self):
        """Clear script errors on the COMP (recurse=False) before a new build attempt."""
        for fn in ('clearScriptErrors',):
            try:
                getattr(self.ownerComp, fn)(recurse=False)
            except Exception:  # noqa: BLE001
                pass

    def _errors_dat(self):
        """Get-or-create builder/build_errors tableDAT (diagnostics sink for the UI)."""
        dat = self.builderComp.op('build_errors')
        if dat is None:
            try:
                dat = self.builderComp.create(tableDAT, 'build_errors')
                dat.nodeX, dat.nodeY = 400, -200
            except Exception:  # noqa: BLE001
                return None
        return dat

    def _publish_diagnostics(self, diags):
        """Rewrite build_errors with parse_diagnostics rows (severity/file/line/col/code/message)."""
        dat = self._errors_dat()
        if dat is None:
            return
        try:
            dat.clear()
            dat.appendRow(['severity', 'file', 'line', 'col', 'code', 'message'])
            for d in diags:
                dat.appendRow([d['severity'], d['file'], d['line'], d['col'], d['code'], d['message']])
        except Exception as e:  # noqa: BLE001
            self._debug('Build', f'could not publish diagnostics: {e}')

    # ========================================================================================== #
    #  PROPERTIES                                                                                #
    #  Path conventions: project.folder is the .toe directory; work lives under                #
    #  PluginProjects/<name>/ (CMakeLists.txt, source/, build/bin/<Config>/) and deploys to    #
    #  __Plugins__/<name>/<name>.dll.                                                           #
    # ========================================================================================== #

    @staticmethod
    def _peek_plugin_builder_dir(settings_text):
        """Cheap [Paths] PluginBuilderDir peek used BEFORE parse_settings to locate source/*.py."""
        for line in (settings_text or '').splitlines():
            if line.strip().lower().startswith('pluginbuilderdir'):
                _, _, v = line.partition('=')
                v = v.strip().strip('"').replace('${USER_PATH}', os.environ.get('USERPROFILE', ''))
                return os.path.normpath(v) if v else ''
        return ''

    @property
    def working_dir(self):
        return f"{self.plugin_projects_dir}/{self.Pluginname}"

    @property
    def abs_working_dir(self):
        return f"{project.folder}/{self.working_dir}"

    @property
    def plugin_dir(self):
        return f"{self.plugins_dir}/{self.Pluginname}"

    @property
    def Pluginname(self):
        return self.ownerComp.par.Pluginname.eval()

    @property
    def CMakeListsPath(self):
        return f"{self.working_dir}/CMakeLists.txt"

    @property
    def CMakeListsExists(self):
        return os.path.exists(self.CMakeListsPath)

    @property
    def build_config(self):
        return self.ownerComp.par.Buildconfig.eval()

    @property
    def PluginBuilderDir(self):
        return self.settings['paths'].get('PluginBuilderDir', '')

    @property
    def include_dir(self):
        custom = self.settings['paths'].get('SdkIncludeDir', '')
        return custom if custom else f"{self.PluginBuilderDir}/include"

    @property
    def SourceDir(self):
        return f"{self.working_dir}/source"

    @property
    def ninja_dir(self):
        return self.toolchain.get('ninja_dir', '')

    @property
    def template_dir(self):
        return f"{self.PluginBuilderDir}/templates"

    @property
    def vcvarsall(self):
        return self.toolchain.get('vcvarsall', '')

    @property
    def CurrentBinDir(self):
        return f"PluginProjects/{self.Pluginname}/build/bin/{self.build_config}"

    @property
    def TDProjectName(self):
        return self.core.td_project_name(project.name)

    @property
    def TDPath(self):
        return f"{app.binFolder}/TouchDesigner.exe"

    @property
    def TDSamplesDir(self):
        try:
            return f"{app.samplesFolder}/CPlusPlus"
        except Exception:  # noqa: BLE001
            return ''

    @property
    def PluginPath(self):
        return f"{self.plugin_dir}/{self.Pluginname}.dll"

    @property
    def build_path(self):
        return f"{self.CurrentBinDir}/{self.Pluginname}.dll"

    @property
    def CompileOnUpdate(self):
        return bool(self.ownerComp.par.Compileonupdate.eval())

    @property
    def cmake_build_cmd(self):
        """Human-readable configure command (the runner receives the argv list from _configure_args)."""
        return ' '.join(self._configure_args())

    @property
    def cmake_build_plugin_cmd(self):
        return ' '.join(self._build_args())

    @property
    def cmake_clean_cmd(self):
        return 'ninja -C build clean'

    # ========================================================================================== #
    #  PATH / TOOLCHAIN VALIDATION                                                               #
    #  Never raises: missing tools are textport errors + PathsValid=False so create/build     #
    #  fail with a clear status instead of an exception.                                       #
    # ========================================================================================== #

    def check_paths(self):
        """Resolve the toolchain (settings.ini > vswhere > PATH > VS-bundled). Never raises.

        Sets self.PathsValid; failures are printed to the textport (Init) so a missing
        vcvarsall/ninja is visible without opening a dialog. Called from __init__.
        """
        paths = self.settings['paths']
        ok = True
        if not paths.get('PluginBuilderDir') or not os.path.isdir(paths['PluginBuilderDir']):
            self._log('Init', f"ERROR: settings.ini [Paths] PluginBuilderDir '{paths.get('PluginBuilderDir')}' does not exist. "
                              "Open SetSettings.toe and set it to the PluginBuilder_V2 folder.")
            ok = False
        self.toolchain = self.core.discover_toolchain(paths, extra_search_dirs=[os.path.join(self.user_home, 'ninja')])
        for e in self.toolchain['errors']:
            self._log('Init', f'ERROR: {e}')
            ok = False
        if self.toolchain['sources']:
            self._debug('Init', f"toolchain: {self.toolchain['sources']}")
        self.PathsValid = ok
        return ok

    def _check_sdk_versions(self):
        """Warn (textport + addWarning) when include/ headers differ from the running TD.

        Message comes from PluginBuilderCore.sdk_mismatch_message and tells the user to
        re-run dev/sync_templates.py. No-op if TD samples dir is unavailable.
        """
        samples = self.TDSamplesDir
        if not samples or not os.path.isdir(samples) or not self.sdk_versions:
            return
        installed = self.core.installed_sdk_versions(samples)
        msg = self.core.sdk_mismatch_message(self.sdk_versions, installed)
        if msg:
            self._log('Init', 'WARNING: ' + msg)
            self._set_warning(msg)

    def _populate_template_menu(self):
        """Fill Plugintemplate menu from load_templates(); keep the current selection if valid."""
        try:
            par = self.ownerComp.par.Plugintemplate
            names = list(self.templates.keys())
            if names:
                current = par.eval()
                par.menuNames = names
                par.menuLabels = [f"{n} ({self.templates[n]['family']})" for n in names]
                if current in names:
                    par.val = current
        except Exception as e:  # noqa: BLE001
            self._debug('Init', f'template menu not updated: {e}')

    # ========================================================================================== #
    #  PLUGIN CREATION                                                                           #
    #  create_plugin is the only writer of a new PluginProjects/<name>/ tree.                  #
    # ========================================================================================== #

    def create_plugin(self):
        """Scaffold a new plugin project from a template and kick off the first configure + build.

        Steps: validate name + toolchain -> resolve template/meta/family -> make
        PluginProjects/<name>/{CMakeLists.txt, plugin.json, source/*, .vscode/, launch.vs.json}
        with render_source placeholders -> create_plugin_loader(family) -> build_plugin(then_compile=True).
        On any failure after mkdir, the project dir is removed before re-raising.
        """
        name = self.Pluginname
        ok, msg = self.core.validate_plugin_name(name)
        if not ok:
            self._set_error(msg)
            raise ValueError(msg)
        if not self.PathsValid:
            self._set_error('Toolchain/paths are not configured — check the textport and settings.ini.')
            raise RuntimeError('Toolchain not configured')

        template_name = self.ownerComp.par.Plugintemplate.eval()
        meta = self.templates.get(template_name)
        if meta is None:
            msg = f"Unknown template '{template_name}' (available: {', '.join(self.templates) or 'none'})"
            self._set_error(msg)
            raise ValueError(msg)
        family = meta['family']
        if family not in self.loader_op_map:
            msg = f"This TouchDesigner build has no cplusplus{family} operator; cannot create a {family} plugin."
            self._set_error(msg)
            raise RuntimeError(msg)

        self._log('Create', f"Creating new plugin project '{name}' from template '{template_name}' ({family})")
        os.makedirs(self.plugin_projects_dir, exist_ok=True)
        if os.path.exists(self.working_dir):
            raise FileExistsError(f"Directory {self.working_dir} already exists. Rename plugin or delete the directory.")
        os.makedirs(self.working_dir)

        op_type = self.core.sanitize_op_type(name)
        op_icon = self.core.make_op_icon(name)
        if self._op_type_collides(op_type, family):
            alt = self.core.sanitize_op_type(name + 'custom')
            self._set_warning(f"opType '{op_type}' collides with a built-in {family}; using '{alt}'.")
            op_type = alt

        try:
            # --- CMakeLists.txt (tiny; includes cmake/TDPlugin.cmake) + plugin.json manifest ---
            extra = self.core.template_cmake_lines(meta, name)
            if not extra and 'cuda' not in meta.get('features', []):
                extra.append(f'# td_plugin_optimize({name} REALTIME)   # AVX2 + fast math + LTO; pair with td_cpu_guard.h')
            cmake_text = self.cmake_blocks.assemble(name, family, self.PluginBuilderDir,
                                                    features=meta.get('features', []), extra_lines=extra)
            with open(f"{self.working_dir}/CMakeLists.txt", 'w', encoding='utf-8') as f:
                f.write(cmake_text)
            manifest = {
                'name': name, 'family': family, 'optype': op_type, 'template': template_name,
                'features': meta.get('features', []), 'api_versions': self.sdk_versions,
                'plugin_builder_dir': self.PluginBuilderDir, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            self.core.write_plugin_manifest(self.working_dir, manifest)
            self._log('Create', 'Generated CMakeLists.txt + plugin.json')

            # --- IDE files ---
            shutil.copyfile(f"{self.PluginBuilderDir}/source/CMakePresets.json", f"{self.working_dir}/CMakePresets.json")
            self._write_launch_vs_json()
            self._write_vscode_files()

            # --- template sources ---
            replacements = self.core.template_replacements(
                name, op_type, name, op_icon,
                self.settings['plugininfo']['Author'], self.settings['plugininfo']['Email'],
                self.settings['plugininfo'].get('HelpURL', ''))
            files = self.core.scaffold_template_files(meta, self.working_dir, name, replacements)
            self._log('Create', f"Copied {len(files)} template file(s)")

            os.makedirs(self.plugin_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self._log('Create', f"ERROR: {e} — cleaning up project directory")
            shutil.rmtree(self.working_dir, ignore_errors=True)
            raise

        self.create_plugin_loader(family)
        self.build_plugin(then_compile=True)
        run("args[0].PostCreatePlugin()", self.ownerComp, delayFrames=300)
        if not self.dev_mode:
            self.disable_create_pars()
        self._log('Create', f"Plugin '{name}' created (opType='{op_type}') — configuring + compiling")

    def _op_type_collides(self, op_type, family):
        """True if a built-in operator already occupies this opType (e.g. 'fft' + 'CHOP')."""
        try:
            return globals().get(f"{op_type.lower()}{family}") is not None
        except Exception:  # noqa: BLE001
            return False

    def _write_launch_vs_json(self):
        """Instantiate source/launch.vs.json template into the project (Visual Studio launch)."""
        with open(f"{self.PluginBuilderDir}/source/launch.vs.json", 'r', encoding='utf-8') as f:
            launch = json.load(f)
        project_name = self.TDProjectName
        for cfg in launch['configurations']:
            cfg['name'] = cfg['name'].replace('__TD_PROJECT_NAME__', project_name)
            cfg['args'][0] = cfg['args'][0].replace('__TD_PROJECT_NAME__', project_name)
            cfg['projectTarget'] = cfg['projectTarget'].replace('__PLUGIN_NAME__', self.Pluginname)
            cfg['exe'] = cfg['exe'].replace('__TD_PATH__', self.TDPath)
        with open(f"{self.working_dir}/launch.vs.json", 'w', encoding='utf-8') as f:
            json.dump(launch, f, indent=4)

    def _write_vscode_files(self):
        """Write .vscode/{launch,c_cpp_properties,settings}.json via Core.render_vscode_files."""
        toe = f"{project.folder}/{self.TDProjectName}.toe"
        files = self.core.render_vscode_files(self.Pluginname, self.TDPath, toe, project.folder,
                                              self.include_dir.replace('\\', '/'))
        vs_dir = f"{self.working_dir}/.vscode"
        os.makedirs(vs_dir, exist_ok=True)
        for fn, data in files.items():
            with open(os.path.join(vs_dir, fn), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)

    def destroy_children(self):
        """Destroy depth-1 children except the preserved builder/source/CMakeLists DATs."""
        preserved = ['builder', 'source', 'CMakeLists']
        for child in self.ownerComp.findChildren(depth=1):
            if child.name not in preserved:
                child.destroy()

    def create_plugin_loader(self, plugin_type):
        """Create the loader chain (optional in → cplusplus loader → out) for a family.

        viewer=True is load-bearing (not cosmetic): the loader must cook every frame or
        Info CHOP/DAT / middle-click popup callbacks never run (see the long comment at
        the assignment). DLL path is __Plugins__/<name>/<name>.dll; unloadplugin tracks
        whether that file exists yet. Cross-refs: loader_op_map built in __init__,
        sync_custom_parameters re-resolves by the fixed name 'plugin_loader'.
        """
        info = self.loader_op_map.get(plugin_type)
        if info is None:
            self._set_error(f'No loader available for family {plugin_type}')
            return
        self._log('Create', f"Creating plugin loader chain (type={plugin_type})")
        self.destroy_children()

        create_input_op = bool(self.ownerComp.par.Createinputop.eval()) and info.get('in') is not None
        in_op = self.ownerComp.create(info['in'], 'in1') if create_input_op else None
        self.loader_op = self.ownerComp.create(info['loader'], 'plugin_loader')
        out_op = self.ownerComp.create(info['out'], 'out1') if info.get('out') is not None else None

        if in_op is not None:
            in_op.nodeX = -200
            in_op.outputConnectors[0].connect(self.loader_op.inputConnectors[0])
        self.loader_op.nodeX = 0
        # Pull the loader every frame by leaving its viewer flag on. This is load-bearing, not cosmetic.
        # Everything the plugin reports - the middle-click info popup, the Info CHOP, the Info DAT, and
        # the warning/error state - is produced by callbacks TouchDesigner only runs *inside a cook*
        # (the order is documented at the top of CHOP_CPlusPlusBase.h). The plugin asks to be cooked
        # every frame via cookEveryFrame, but that only keeps a node cooking once it has cooked; it
        # cannot cause the first one. The SDK's kick-start switch for that is
        # OP_CustomOPInfo::cookOnStart, and the same header is explicit that it "only works for Custom
        # Operators, not cases where the .dll is loaded into CPlusPlus CHOP" - which is exactly how this
        # loader hosts the plugin. An OP with an active viewer is cooked every frame, so that is the
        # kick-start available to this host. Without it a loader whose output nothing else views can sit
        # un-cooked, and then every one of those callbacks is dead at once: the popup comes up empty,
        # the Info CHOP/DAT stay blank, and a hard failure has no error badge. That failure has no
        # symptom other than the blank popup, which is why it is worth being deliberate about here.
        self.loader_op.viewer = True
        if out_op is not None:
            out_op.nodeX = 200
            self.loader_op.outputConnectors[0].connect(out_op.inputConnectors[0])

        dll = f"{self.plugin_dir}/{self.Pluginname}.dll"
        self.loader_op.par.plugin = dll
        self.loader_op.par.unloadplugin = not os.path.exists(dll)
        if os.path.exists(dll):
            try:
                self._loaded_dll_hash = self.core.file_sha256(dll)
                self.ownerComp.par.Loadeddll = os.path.basename(dll)
            except Exception:  # noqa: BLE001
                pass
        run("args[0].ext.PluginBuilderExt.sync_custom_parameters()", self.ownerComp, delayFrames=15)

    # ---------------- legacy CMake assembly helpers (kept for external callers) ----------------- #
    # Thin wrappers over CMakeBlocks.assemble; create_plugin calls cmake_blocks.assemble directly.

    def assemble_cmake_text_basic(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'CHOP', self.PluginBuilderDir)

    def assemble_cmake_text_cuda(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'TOP', self.PluginBuilderDir, features=['cuda'])

    def assemble_cmake_text_python(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'CHOP', self.PluginBuilderDir, features=['python'])

    # ========================================================================================== #
    #  BUILD & COMPILE                                                                           #
    #  configure -> BuildRunner('configure', _configure_args) then optional compile;          #
    #  compile  -> BuildRunner('build', _build_args) then _schedule_copy on success.          #
    #  Both gate on _cmake_inputs_hash / pending_count to avoid redundant work.               #
    # ========================================================================================== #

    def _configure_args(self):
        """cmake -B build -G Ninja with PLUGIN_BUILDER_DIR, PLUGIN_DIR, build type, compile_commands.

        Adds -DTD_SAMPLES_DIR when the running TD exposes Samples/CPlusPlus (for 3rdParty lookup).
        Displayed via cmake_build_cmd; submitted as a BuildJob('configure', ...) in build_plugin.
        """
        args = ['cmake', '-B', 'build', '-G', 'Ninja',
                f'-DPLUGIN_BUILDER_DIR={self.PluginBuilderDir}',
                f'-DPLUGIN_DIR={project.folder}/{self.plugin_dir}',
                f'-DCMAKE_BUILD_TYPE={self.build_config}',
                '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
        samples = self.TDSamplesDir
        if samples and os.path.isdir(samples):
            args.append(f'-DTD_SAMPLES_DIR={samples}')
        return args

    def _build_args(self):
        """ninja -C build [+ -j N when [Options] ParallelJobs is a digit]."""
        args = ['ninja', '-C', 'build']
        jobs = str(self.settings['options'].get('ParallelJobs', '')).strip()
        if jobs.isdigit():
            args += ['-j', jobs]
        return args

    def _cmake_inputs_hash(self):
        """Cheap change key for skipping configure: build type + PluginBuilderDir + file hashes.

        Compared against self._last_configure_hash in build_plugin; cleared by clean_build /
        onPluginname when loading a different project.
        """
        parts = [self.build_config, self.PluginBuilderDir]
        for fn in ('CMakeLists.txt', 'plugin.json'):
            p = f"{self.working_dir}/{fn}"
            if os.path.exists(p):
                try:
                    parts.append(self.core.file_sha256(p))
                except OSError:
                    pass
        return '|'.join(parts)

    def build_plugin(self, then_compile=False, force=False):
        """CMake configure. Skipped when nothing relevant changed unless force=True.

        Also self-heals a non-Ninja CMakeCache (deletes build/). On success chains to
        compile_plugin when then_compile; on failure publishes diagnostics to
        builder/build_errors and shows the first error in the status bar.
        """
        if not os.path.exists(self.abs_working_dir):
            self._set_error(f"Directory {self.abs_working_dir} does not exist.")
            return
        if not self.CMakeListsExists:
            self._log('Build', f"SKIPPED — no CMakeLists.txt found at {self.CMakeListsPath}")
            return

        build_dir = os.path.join(self.abs_working_dir, 'build')
        cache_file = os.path.join(build_dir, 'CMakeCache.txt')
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                if 'CMAKE_GENERATOR:INTERNAL=' in content and 'CMAKE_GENERATOR:INTERNAL=Ninja' not in content:
                    self._log('Build', "Detected non-Ninja CMake generator cache. Cleaning build directory...")
                    shutil.rmtree(build_dir, ignore_errors=True)
            except Exception as e:  # noqa: BLE001
                self._log('Build', f"Warning inspecting CMakeCache.txt: {e}")

        inputs_hash = self._cmake_inputs_hash()
        if not force and os.path.exists(cache_file) and inputs_hash == self._last_configure_hash:
            self._debug('Build', 'configure skipped (inputs unchanged)')
            if then_compile:
                self.compile_plugin()
            return

        if not self._ensure_runner():
            return
        self._log('Build', f"CMake configure '{self.Pluginname}' [{self.build_config}] in {self.abs_working_dir}")
        self._debug('Build', f"  {self.cmake_build_cmd}")
        self._set_status('Configuring…')

        def on_done(job, _then=then_compile, _hash=inputs_hash):
            diags = self.core.parse_diagnostics(job.output)
            errors, warnings = self.core.summarize_diagnostics(diags)
            self._publish_diagnostics(diags)
            if job.ok:
                self._last_configure_hash = _hash
                self._set_status(f'Configured ({job.duration_ms:.0f} ms)')
                self._log('Build', f"configure OK in {job.duration_ms:.0f} ms")
                if _then:
                    self.compile_plugin()
            else:
                first = next((d for d in diags if d['severity'] == 'error'), None)
                detail = f": {first['message']}" if first else ''
                self._set_status(f'CONFIGURE FAILED (exit {job.returncode})')
                self._set_error(f"CMake configure failed (exit {job.returncode}){detail}")
                if not self.verbose:
                    for line in job.output[-25:]:
                        print(line, end='')

        self.runner.submit(self.core.BuildJob('configure', self._configure_args(), self.abs_working_dir,
                                              label=f'configure {self.Pluginname}', on_done=on_done))
        self._schedule_poll()

    def compile_plugin(self):
        """Ninja build. Coalesces repeated requests while a build is running.

        If a build is already current, sets _rebuild_after_current (one trailing rebuild).
        If a configure/build is already queued, skips entirely (that queue chains to a build).
        On success schedules the hash-gated DLL copy (_schedule_copy) unless ninja reported
        'no work to do'; on failure prints de-duplicated diagnostics.
        """
        if not self.CMakeListsExists:
            self._log('Compile', "SKIPPED — no CMakeLists.txt found")
            return
        if not self._ensure_runner():
            return
        cur = self.runner.current_job
        if cur is not None and cur.kind == 'build':
            self._rebuild_after_current = True
            self._debug('Compile', 'build already running — will rebuild when it finishes')
            return
        if any(k in ('configure', 'build') for k in self.runner.pending_kinds()):
            # A configure (or build) is already queued; the existing queue chains to a build, so
            # don't stack a duplicate build on top of it. (Staging/install jobs do not count.)
            self._debug('Compile', 'build skipped — a configure/build is already queued')
            return

        self._log('Compile', f"Ninja build '{self.Pluginname}' → {self.build_path}")
        self._set_status('Compiling…')
        self._clear_errors()

        def on_done(job):
            diags = self.core.parse_diagnostics(job.output)
            errors, warnings = self.core.summarize_diagnostics(diags)
            self._publish_diagnostics(diags)
            if job.cancelled:
                self._set_status('Build cancelled')
                return
            if job.ok:
                no_work = any('no work to do' in line for line in job.output)
                self._set_status(f"OK ({job.duration_ms:.0f} ms{', ' + str(warnings) + ' warnings' if warnings else ''})")
                try:
                    self.ownerComp.par.Lastbuild = time.strftime('%H:%M:%S') + f" — {job.duration_ms:.0f} ms"
                except Exception:  # noqa: BLE001
                    pass
                self._log('Compile', f"build OK in {job.duration_ms:.0f} ms" + (' (no work to do)' if no_work else ''))
                if not no_work:
                    self._schedule_copy()
            else:
                first = next((d for d in diags if d['severity'] == 'error'), None)
                detail = f" — {first['file']}({first['line']}): {first['code']}: {first['message']}" if first else ''
                self._set_status(f'BUILD FAILED ({errors} error{"s" if errors != 1 else ""})')
                self._set_error(f"Build failed (exit {job.returncode}){detail}")
                if not self.verbose:
                    rendered = [f"  {d['severity']:<7} {d['file']}({d['line']}): {d['code']} {d['message']}" for d in diags]
                    for line in self.core.dedupe_consecutive(rendered):
                        print(line, end='')
            if self._rebuild_after_current:
                self._rebuild_after_current = False
                self.compile_plugin()

        self.runner.submit(self.core.BuildJob('build', self._build_args(), self.abs_working_dir,
                                              label=f'build {self.Pluginname}', on_done=on_done))
        self._schedule_poll()

    def BuildAndCompile(self):
        """Force configure + build (ignores the inputs-hash skip)."""
        self.build_plugin(then_compile=True, force=True)

    def cancel_build(self):
        """Cancel queued jobs, kill the running child, clear any trailing-rebuild flag."""
        if self.runner is None:
            return
        n = self.runner.cancel_pending()
        killed = self.runner.kill_current()
        self._rebuild_after_current = False
        self._log('Build', f"cancelled {n} pending job(s){', killed running job' if killed else ''}")
        self._set_status('Cancelled')

    def clean_build(self):
        """Cancel builds, rmtree PluginProjects/<name>/build, invalidate _last_configure_hash."""
        build_dir = os.path.join(self.abs_working_dir, 'build')
        if self.runner is not None:
            self.cancel_build()
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
            self._log('Build', f"removed {build_dir}")
        self._last_configure_hash = None
        self._set_status('Clean')

    def run_tests(self):
        """ctest --test-dir build --output-on-failure -LE perf (td_plugin_add_test targets; perf gates excluded)."""
        if not self._ensure_runner():
            return
        self._set_status('Testing…')

        def on_done(job):
            if job.ok:
                self._set_status(f'Tests passed ({job.duration_ms:.0f} ms)')
            else:
                self._set_status(f'TESTS FAILED (exit {job.returncode})')
                for line in job.output[-30:]:
                    print(line, end='')
        # -LE perf: benchmark gates (td_plugin_add_bench ... GATE) are timing-sensitive and slow; they run
        # on demand with `ctest -L perf` on an idle machine, not on every Run Tests pulse.
        self.runner.submit(self.core.BuildJob('test', ['ctest', '--test-dir', 'build', '--output-on-failure', '-LE', 'perf'],
                                              self.abs_working_dir, label='ctest', on_done=on_done))
        self._schedule_poll()

    def generate_parameters(self):
        """Regenerate source/Parameters.h/.cpp from <project>/parameters.json (source/ParamGen.py).

        Unchanged output files are not rewritten, so only a real spec change triggers the source watch
        and a rebuild (with Compile On Update on).
        """
        spec = f"{self.working_dir}/parameters.json"
        if not os.path.exists(spec):
            self._set_error(f"No parameters.json in {self.working_dir} (see templates/RealtimeCHOP/project/parameters.json).")
            return
        try:
            gen = _import_support_module('ParamGen', self.PluginBuilderDir)
            h, c = gen.generate_files(spec, f"{self.working_dir}/source")
        except Exception as e:  # noqa: BLE001
            self._set_error(f"Generate Parameters failed: {e}")
            return
        self._log('Params', f"Generated {h} and {c} from parameters.json")
        self._set_status('Parameters generated')

    def bake_plugin(self):
        """Create a standalone production node next to the PluginBuilder COMP ("Bake for Performance").

        A show should not run the plugin inside the development COMP: mirrored parameters evaluate BIND
        expressions, parameter-execute callbacks forward every change, folder DATs watch the source tree
        and a run() loop polls the build runner. The baked node is a plain cplusplus<Family> op that loads
        the deployed DLL with every parameter set to a constant (binds and expressions resolved to their
        current values), wired to nothing - connect it where the plugin is needed.
        """
        loader = self.ownerComp.op('plugin_loader')
        if loader is None or not getattr(loader, 'valid', True):
            self._set_error('Bake: no plugin_loader in the PluginBuilder COMP.')
            return
        parent = self.ownerComp.parent()
        name = f"{self.Pluginname}_baked"
        try:
            old = parent.op(name)
            if old is not None:
                old.destroy()
            node = parent.create(type(loader), name)
            node.nodeX, node.nodeY = self.ownerComp.nodeX + 200, self.ownerComp.nodeY - 150
            dll = os.path.abspath(f"{project.folder}/{self.plugin_dir}/{self.Pluginname}.dll").replace('\\', '/')
            node.par.plugin = dll
            node.par.unloadplugin = False
            node.cook(force=True)
            copied = 0
            for p in self._persisted_loader_pars(loader):
                q = getattr(node.par, p.name, None)
                if q is None or getattr(p, 'style', '') in ('Pulse', 'Header', 'Header2'):
                    continue
                try:
                    q.val = p.eval()             # constants only: no bind, no expression
                    copied += 1
                except Exception:  # noqa: BLE001
                    pass
            node.cook(force=True)
        except Exception as e:  # noqa: BLE001
            self._set_error(f"Bake failed: {e}")
            return
        self._log('Bake', f"Created {node.path} ({copied} parameter value(s) copied as constants, DLL {dll})")
        self._set_status(f'Baked → {node.name}')

    def RefreshDats(self):
        """Force-cook file-watching DATs (folder_bin, folder_source, CMakeLists) + param sync.

        Called on init (delayFrames=120) and from onPluginname after loading a project —
        drives OnPluginUpdate / OnSourceUpdate / OnCMakeListsUpdate when files change.
        """
        self._debug('Init', "Refreshing file-watching DATs...")
        for dat in (self.folder_binDat, self.folder_sourceDat, self.CMakeListsDat):
            if dat is not None:
                try:
                    dat.cook(force=True)
                except Exception:  # noqa: BLE001
                    pass
        self.sync_custom_parameters()

    # ========================================================================================== #
    #  CLEANUP                                                                                   #
    #  clear_plugin_builder is the full reset (name cleared / user action); cancel_build and   #
    #  clean_build only affect the build directory and queued jobs.                            #
    # ========================================================================================== #

    def clear_plugin_builder(self):
        """Unload the plugin, destroy loader chain, drop mirrored pages, clear Pluginname.

        Invoked when Pluginname is set to '' (onPluginname) or manually. Keeps the
        builder / source / CMakeLists children (destroy_children preserves them).
        """
        self._log('Cleanup', f"Clearing plugin builder (current: '{self.Pluginname}')")
        self.loader_op = self.ownerComp.op('plugin_loader')
        if self.loader_op is not None:
            self.loader_op.par.unloadplugin = True
            self.loader_op.cook(force=True)
        self.destroy_children()
        for page_name in self._mirrored_page_names():
            page = self._get_custom_page(self.ownerComp, page_name)
            if page:
                for p in list(page.pars):
                    try:
                        p.destroy()
                    except Exception:  # noqa: BLE001
                        pass
                if page_name != 'Custom':
                    try:
                        page.destroy()
                    except Exception:  # noqa: BLE001
                        pass
        self._store_mirrored_pages([])
        self._last_synced_sig = None
        if self.Pluginname != '':
            self.ownerComp.par.Pluginname = ''
        self._set_status('')
        self._log('Cleanup', "Plugin builder cleared")

    # ========================================================================================== #
    #  INSTALL                                                                                   #
    #  Copies the deployed __Plugins__ tree to the user-wide Derivative/Plugins folder so the  #
    #  plugin is available outside this .toe (safe_replace_file handles mapped DLLs).         #
    # ========================================================================================== #

    def install_plugin(self):
        """Copy __Plugins__/<name>/ (minus *.old) to ~/Documents/Derivative/Plugins/<name>/.

        Runs as an in-process BuildRunner job so a large runtime (oneMKL is ~520 MB) is compared and
        copied off TouchDesigner's main thread; files whose size and mtime already match are skipped
        without being read (safe_replace_file -> files_identical). An installed DLL that is currently
        mapped is still replaced via the rename-in-place trick. Result and errors land in on_done.
        """
        self._log('Install', f"Installing '{self.Pluginname}' from {self.plugin_dir}")
        if not os.path.exists(self.plugin_dir):
            self._set_error(f"Source folder {self.plugin_dir} does not exist.")
            return
        if not self._ensure_runner():
            return
        core = self.core
        src_root = os.path.abspath(self.plugin_dir)
        dest = os.path.join(os.path.expanduser('~'), 'Documents', 'Derivative', 'Plugins', self.Pluginname)

        def work(job, emit):
            os.makedirs(dest, exist_ok=True)
            core.cleanup_old_files(dest)
            copied, skipped = 0, 0
            for root_dir, _dirs, files in os.walk(src_root):
                for fn in files:
                    if re.search(r'\.old\d*$', fn):
                        continue
                    src = os.path.join(root_dir, fn)
                    rel = os.path.relpath(src, src_root)
                    changed, note = core.safe_replace_file(src, os.path.join(dest, rel))
                    if changed:
                        copied += 1
                        emit(f"[Install]   {rel}: {note}")
                    else:
                        skipped += 1
            job.result = (copied, skipped)
            return 0

        def on_done(job):
            if not job.ok:
                self._set_error(f"Install failed: {job.error}")
                self._set_status('Install FAILED')
                return
            copied, skipped = job.result
            self._log('Install', f"INSTALLATION COMPLETE: {copied} file(s) updated, {skipped} unchanged in {dest}")
            self._set_status(f'Installed → {dest}')

        self._set_status('Installing…')
        self.runner.submit(core.BuildJob('call', [], self.abs_working_dir, label=f'install {self.Pluginname}',
                                         on_done=on_done, func=work))
        self._schedule_poll()

    # ========================================================================================== #
    #  UI HELPERS                                                                                #
    # ========================================================================================== #

    def disable_create_pars(self):
        """Lock create UI after create_plugin (skipped when settings [DevMode] is present)."""
        self.ownerComp.par.Createplugin.enable = False
        self.ownerComp.par.Pluginname.readOnly = True
        self.ownerComp.par.Plugintemplate.readOnly = True
        self.ownerComp.par.Createinputop.enable = False

    def EnableCreatePars(self):
        """Unlock create UI (also called by dev/deploy.py:reset before export)."""
        self.ownerComp.par.Createplugin.enable = True
        self.ownerComp.par.Pluginname.readOnly = False
        self.ownerComp.par.Plugintemplate.readOnly = False
        self.ownerComp.par.Createinputop.enable = True

    def PostCreatePlugin(self):
        """delayFrames=300 after create: force-cook CMakeLists DAT so the editor shows it."""
        if self.CMakeListsDat is not None:
            self.CMakeListsDat.cook(force=True)

    def file_locked(self, filepath):
        """True if the file can't be opened for writing (a DLL mapped by a process denies write access).

        Used by _do_copy_plugin to detect a still-writing linker output before hashing/copying.
        Missing file -> False (nothing to lock yet).
        """
        try:
            fd = os.open(filepath, os.O_RDWR)
            os.close(fd)
        except FileNotFoundError:
            return False
        except (PermissionError, OSError):
            return True
        return False

    # ========================================================================================== #
    #  CUSTOM PARAMETER SYNC (page-preserving, signature-gated)                                  #
    #  Mirrors the loader's custom parameters onto ownerComp with BIND expressions so the UI    #
    #  lives on the PluginBuilder COMP while values drive plugin_loader.par.*.                  #
    #  Signature (par_signature) gates full rebuilds; MirrorPages=false collapses onto 'Custom'.#
    # ========================================================================================== #

    @staticmethod
    def _get_custom_page(op, page_name):
        """Find a custom page by name on op (None if op invalid / page missing)."""
        if op is None or not getattr(op, 'valid', True):
            return None
        for page in getattr(op, 'customPages', []):
            if page.name == page_name:
                return page
        return None

    def _mirrored_page_names(self):
        """Page names this COMP mirrors into (stored as 'PB_mirror_pages'; always includes 'Custom')."""
        try:
            names = self.ownerComp.fetch('PB_mirror_pages', [])
        except Exception:  # noqa: BLE001
            names = []
        names = list(names) if isinstance(names, (list, tuple)) else []
        if 'Custom' not in names:
            names.append('Custom')
        return names

    def _store_mirrored_pages(self, names):
        """Persist mirrored page list under storage key 'PB_mirror_pages' (cleared by deploy.py)."""
        try:
            self.ownerComp.store('PB_mirror_pages', list(names))
        except Exception:  # noqa: BLE001
            pass

    def _own_page_names(self):
        """Pages that belong to the builder UI itself (never mirrored into).

        A plugin page that collides with an own-page name is renamed '<page> (Plugin)'
        by _target_page_for so builder controls are never clobbered.
        """
        mirrored = set(self._mirrored_page_names())
        return {p.name for p in self.ownerComp.customPages if p.name not in mirrored}

    @staticmethod
    def _copy_par_attributes(src_p, dst_p):
        # menus first so that a menu 'val' is accepted afterwards
        # (menuNames/menuLabels must exist before assigning the current entry value)
        if hasattr(src_p, 'menuNames') and hasattr(dst_p, 'menuNames'):
            try:
                dst_p.menuNames = list(src_p.menuNames)
                dst_p.menuLabels = list(src_p.menuLabels)
            except Exception:  # noqa: BLE001
                pass
        for attr in ('default', 'min', 'max', 'normMin', 'normMax', 'clampMin', 'clampMax', 'enable', 'readOnly', 'help'):
            if hasattr(src_p, attr):
                try:
                    setattr(dst_p, attr, getattr(src_p, attr))
                except Exception:  # noqa: BLE001
                    pass

    def _collect_loader_pars(self):
        """Return list of (tuplet_name, page_name, style, components[list of Par], label, menu_names).

        Skips BUILTIN_LOADER_PARS (reload-control / structural builtins) and non-custom
        lowercase pars; only custom or Capitalized names are mirrored.
        """
        groups, order = {}, []
        try:
            all_pars = self.loader_op.pars()
        except Exception:  # noqa: BLE001
            all_pars = []
        for p in all_pars:
            name = p.name
            if not name or name.lower() in self.core.BUILTIN_LOADER_PARS:
                continue
            if not (getattr(p, 'isCustom', False) or name[0].isupper()):
                continue
            tname = getattr(p, 'tupletName', None) or name
            page_name = getattr(getattr(p, 'page', None), 'name', 'Custom') or 'Custom'
            if tname not in groups:
                groups[tname] = {'page': page_name, 'style': getattr(p, 'style', 'Str'), 'comps': [],
                                 'label': getattr(p, 'label', tname), 'menu': tuple(getattr(p, 'menuNames', ()) or ())}
                order.append(tname)
            groups[tname]['comps'].append(p)
        out = []
        for tname in order:
            g = groups[tname]
            comps = sorted(g['comps'], key=lambda q: getattr(q, 'vecIndex', 0))
            out.append((tname, g['page'], g['style'], comps, g['label'], g['menu']))
        return out

    def _target_page_for(self, page_name, own_pages):
        """Resolve (and create if needed) the destination page for a loader parameter page."""
        if not self.mirror_pages:
            page_name = 'Custom'
        elif page_name in own_pages:
            page_name = f'{page_name} (Plugin)'
        page = self._get_custom_page(self.ownerComp, page_name)
        if page is None:
            page = self.ownerComp.appendCustomPage(page_name)
        return page

    def _create_mirror_par(self, page, tname, style, size, label):
        """Create a parameter of the same style on the mirror page. Returns the (first) Par or None.

        Falls back to appendStr when the style-specific append* method is missing or throws
        (unknown style from a newer TD); logs [ParamSync] ERROR and returns None on total failure.
        """
        method = getattr(page, f'append{style}', None)
        try:
            if method is None:
                res = page.appendStr(tname, label=label)
            elif style in ('Float', 'Int', 'Toggle') and size > 1:
                res = method(tname, label=label, size=size)
            else:
                res = method(tname, label=label)
        except Exception:  # noqa: BLE001
            try:
                res = page.appendStr(tname, label=label)
            except Exception as e:  # noqa: BLE001
                self._log('ParamSync', f"  ERROR creating '{tname}' ({style}): {e}")
                return None
        return res[0] if isinstance(res, (list, tuple)) else res

    def _bind_components(self, src_comps, dst_first, is_pulse):
        """Copy attributes then BIND each mirror component to loader.par.<name> (pulses stay unbound)."""
        dst_comps = list(getattr(dst_first, 'tuplet', None) or [dst_first])
        for src_p, dst_p in zip(src_comps, dst_comps):
            self._copy_par_attributes(src_p, dst_p)
            try:
                if is_pulse:
                    dst_p.bindExpr = ''
                else:
                    # Parameter expressions on a COMP evaluate in the COMP's parent network, so the
                    # loader (a child of this COMP) must be addressed through `me`.
                    dst_p.bindExpr = f"me.op('{self.loader_op.name}').par.{src_p.name}"
                    try:
                        dst_p.mode = ParMode.BIND
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

    def sync_custom_parameters(self, force_rebuild=False, verbose=False):
        """Mirror the loader's custom parameters (pages preserved) onto ownerComp with BIND expressions.

        Fast path: same par_signature -> only re-assert bindings (loader may have been
        recreated). Rebuild path: compute target pages, destroy stale/wrong-style pars,
        create/update the rest, drop empty mirrored pages (keep 'Custom'), store
        PB_mirror_pages + _last_synced_sig. Force with the Refreshcustompars pulse.
        """
        self.loader_op = self.ownerComp.op('plugin_loader')
        if self.loader_op is None or not getattr(self.loader_op, 'valid', True):
            return
        try:
            self.loader_op.cook(force=True)
        except Exception:  # noqa: BLE001
            pass

        entries = self._collect_loader_pars()
        sig = self.core.par_signature(
            [(t, pg, st, len(comps), lb, mn) for (t, pg, st, comps, lb, mn) in entries])

        own_pages = self._own_page_names()
        if not force_rebuild and sig == self._last_synced_sig:
            # cheap path: re-assert bindings only (loader may have been re-created)
            for (tname, page_name, style, comps, label, menu) in entries:
                existing = getattr(self.ownerComp.par, tname, None) or getattr(self.ownerComp.par, comps[0].name, None)
                if existing is not None:
                    self._bind_components(comps, existing, style in ('Pulse', 'Header', 'Header2'))
            return

        # --- rebuild: compute target pages, remove stale pars, create/update the rest ---
        expected = {}
        for (tname, page_name, style, comps, label, menu) in entries:
            page = self._target_page_for(page_name, own_pages)
            expected[tname] = page.name
        mirrored_pages = set(self._mirrored_page_names()) | set(expected.values())

        for page_name in list(mirrored_pages):
            page = self._get_custom_page(self.ownerComp, page_name)
            if page is None:
                continue
            for p in list(page.pars):
                tn = getattr(p, 'tupletName', None) or p.name
                if force_rebuild or expected.get(tn) != page_name:
                    try:
                        p.destroy()
                    except Exception:  # noqa: BLE001
                        pass

        for (tname, page_name, style, comps, label, menu) in entries:
            page = self._get_custom_page(self.ownerComp, expected[tname])
            existing = getattr(self.ownerComp.par, tname, None) or getattr(self.ownerComp.par, comps[0].name, None)
            if existing is not None and getattr(existing, 'style', '') != style:
                try:
                    existing.destroy()
                except Exception:  # noqa: BLE001
                    pass
                existing = None
            if existing is None:
                existing = self._create_mirror_par(page, tname, style, len(comps), label)
                if existing is None:
                    continue
            else:
                try:
                    existing.label = label
                except Exception:  # noqa: BLE001
                    pass
            # Inside the loop: every mirrored parameter is bound, not only the last one (a for/else
            # here used to bind a single parameter per rebuild until the next fast-path sync).
            self._bind_components(comps, existing, style in ('Pulse', 'Header', 'Header2'))

        # drop mirrored pages that ended up empty (keep 'Custom' for compatibility)
        for page_name in list(mirrored_pages):
            page = self._get_custom_page(self.ownerComp, page_name)
            if page is not None and page_name != 'Custom' and not list(page.pars):
                try:
                    page.destroy()
                except Exception:  # noqa: BLE001
                    pass
                mirrored_pages.discard(page_name)
        self._store_mirrored_pages(sorted(mirrored_pages))
        self._last_synced_sig = sig

        final = [f"{t}@{expected[t]}" for (t, *_rest) in entries]
        if verbose or final != self._last_synced_pars:
            self._log('ParamSync', f"Mirrored {len(entries)} parameter(s) on page(s): {sorted(set(expected.values()))}")
            for (tname, page_name, style, comps, label, menu) in entries:
                mode = 'read-only' if style in ('Pulse', 'Header', 'Header2') else f"BIND → {self.loader_op.name}.par.{comps[0].name}"
                self._log('ParamSync', f"  {tname:<16} [{style:<6} x{len(comps)}] page='{expected[tname]}' ({mode})")
            self._last_synced_pars = final

    # ========================================================================================== #
    #  PARAMETER CALLBACKS                                                                       #
    #  Wired from the COMP's parameter execute DATs. Known handlers go through the dispatch     #
    #  maps built in __init__; everything else is forwarded to the loader (mirror writes).      #
    # ========================================================================================== #

    def OnParValueChange(self, par, prev):
        if par.name in self.on_par_value_change_map:
            self.on_par_value_change_map[par.name](par.eval(), prev)
        elif self.loader_op is not None and getattr(self.loader_op, 'valid', True):
            # A mirrored parameter is BIND-linked to the loader, so the bind already carried the value;
            # writing it again would set the loader parameter twice (two parameter-change cooks).
            pm = _par_mode()
            if pm is not None and getattr(par, 'mode', None) == getattr(pm, 'BIND', object()):
                return
            loader_par = getattr(self.loader_op.par, par.name, None)
            if loader_par is not None:
                try:
                    loader_par.val = par.eval()
                except Exception:  # noqa: BLE001
                    pass

    def OnParPulse(self, par):
        if par.name in self.on_par_pulse_map:
            self.on_par_pulse_map[par.name]()
        elif self.loader_op is not None and getattr(self.loader_op, 'valid', True):
            loader_par = getattr(self.loader_op.par, par.name, None)
            if loader_par is not None and getattr(loader_par, 'isPulse', False):
                try:
                    loader_par.pulse()
                except Exception:  # noqa: BLE001
                    pass

    def onOutputto(self, value, prev):
        """Output routing changed: tear down and restart the build runner with the new sink."""
        self._log('Runner', f"Output mode changed: '{prev}' → '{value}', restarting build runner...")
        self.close_subprocess()
        self.start_subprocess()

    def onPluginname(self, value, prev):
        """Pluginname changed: '' clears the builder; existing manifest loads the project;
        otherwise just refresh DATs. Failed manifest reads must NOT unload a healthy plugin
        (see the long comment — _ensure_loader_live repairs that intermittent case)."""
        if value == '':
            self._log('Init', f"Plugin name cleared (was '{prev}'), clearing builder...")
            self.clear_plugin_builder()
            return
        manifest = self.core.read_plugin_manifest(self.working_dir) if os.path.isdir(self.working_dir) else None
        if manifest:
            family = manifest['family']
            self._log('Init', f"Loading existing PluginProject '{value}' (type={family})")
            self.create_plugin_loader(family)
            self._last_configure_hash = None
            self.start_subprocess()
            self.sync_custom_parameters(verbose=True)
            self._set_status('Project loaded')
            return
        self.loader_op = self.ownerComp.op('plugin_loader')
        if self.loader_op is not None:
            self.loader_op.par.unloadplugin = True
            self.loader_op.cook(force=True)
            # Unloading is right when there is genuinely nothing to load - but this branch is reached on a
            # *failed manifest read*, which is not the same thing and is not always permanent. If the
            # working directory is momentarily unreadable, or the manifest is mid-write, or the timing of
            # parameter initialization puts this callback before `working_dir` resolves, then a node that
            # had a perfectly good plugin loaded gets its plugin killed and never gets it back.
            #
            # That is the whole of a bug that reads as intermittent: an unloaded CPlusPlus CHOP has no
            # plugin, so it has no information surface at all - a blank middle-click popup - while its last
            # cooked output stays on the screen and looks healthy, so the node appears to work. It comes
            # back the moment anything reloads the loader, which is why a rebuild appears to "fix" it.
            #
            # _ensure_loader_live() is the right repair and not a blind one: it re-enables the plugin only
            # when `plugin` actually holds a path, so a node with nothing configured still ends up unloaded,
            # and setting the viewer flag is what keeps the node cooking so this cannot recur through the
            # other door (see create_plugin_loader).
            self._ensure_loader_live()
        self.RefreshDats()

    # ========================================================================================== #
    #  FILE CHANGE CALLBACKS                                                                     #
    #  Driven by folder DAT cooks (RefreshDats / file watches): build/bin -> copy, source ->   #
    #  compile, CMakeLists -> reconfigure. All debounced or hash-gated.                        #
    # ========================================================================================== #

    def OnPluginUpdate(self):
        """folder_bin DAT callback: something changed in build/bin — schedule a hash-gated copy.

        No-op until build_path exists (first configure may not have linked yet).
        Cross-ref: _schedule_copy -> _do_copy_plugin -> _reload_loader_fresh.
        """
        if self.loader_op is None or self.Pluginname == '':
            return
        if not os.path.exists(self.build_path):
            self._debug('Build→Copy', f"DLL not yet produced ({self.build_path})")
            return
        self._schedule_copy()

    def _schedule_copy(self, delay_frames=10):
        """Debounce DLL copies: kill prior 'copy_dll' runs, record mtime, schedule _do_copy_plugin."""
        for r in runs:
            if r.group == 'copy_dll':
                r.kill()
        self.open_attempts = 0
        try:
            self._build_mtime = os.path.getmtime(self.build_path)
        except OSError:
            self._build_mtime = None
        self._debug('Build→Copy', f"copy scheduled in {delay_frames} frames (debounce)")
        run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=delay_frames)

    def _runtime_dlls_in_bin(self):
        """Runtime DLLs to stage next to the plugin (FFTW, OpenCV, ...), excluding the plugin itself.

        Preferred source: build/<name>_runtime_dlls.txt, written by TDPlugin.cmake's
        td_plugin_add_runtime_dll - exactly the DLLs the project declared. Without it (projects
        configured by an older TDPlugin.cmake) every *.dll in build/bin/<Config>/ is staged, which also
        sweeps up anything dropped there by hand (e.g. a 520 MB oneMKL runtime copied in for a bench).
        """
        bin_dir = self.CurrentBinDir
        if not os.path.isdir(bin_dir):
            return []
        declared = self.core.read_runtime_dll_manifest(os.path.join(self.working_dir, 'build'), self.Pluginname)
        if declared is not None:
            return [os.path.join(bin_dir, fn) for fn in declared
                    if fn != f'{self.Pluginname}.dll' and os.path.exists(os.path.join(bin_dir, fn))]
        return [os.path.join(bin_dir, fn) for fn in os.listdir(bin_dir)
                if fn.lower().endswith('.dll') and fn != f'{self.Pluginname}.dll']

    # ---------------- loader reload with fresh parameter definitions ----------------------------- #
    #
    # TouchDesigner keeps the parameter OBJECTS of a C++ OP across a plugin re-init: setupParameters()
    # adds parameters that did not exist yet, but an existing parameter keeps its stored definition —
    # changed menu entries, labels, ranges or defaults only show up on a node that is created fresh
    # (which is what a TouchDesigner restart does). So a reload first unloads the plugin, checks
    # whether that removed the custom parameters, and if TouchDesigner kept them recreates the loader
    # node (same name, same wires). Values / expressions / bindings are snapshotted and restored.
    #
    # v2.1.1: this path was added after Plugin_FFT's Planner=Patient menu change did not appear
    # on an already-open loader. Helpers are pure and unit-tested (LoaderParPersistenceTests).

    def _custom_loader_pars(self, loader=None):
        """Custom parameters only (used for the menu-diff log and COMP mirroring).

        Filters out BUILTIN_LOADER_PARS; keeps isCustom or Capitalized names only.
        """
        loader = loader or self.loader_op
        out = []
        try:
            all_pars = loader.pars()
        except Exception:  # noqa: BLE001
            return out
        for p in all_pars:
            name = p.name
            if not name or name.lower() in self.core.BUILTIN_LOADER_PARS:
                continue
            if getattr(p, 'isCustom', False) or name[0].isupper():
                out.append(p)
        return out

    def _persisted_loader_pars(self, loader=None):
        """Pars whose value should survive a fresh-node reload: every custom parameter plus the
        built-in value parameters (timeslice, scope, outputresolution, resolutionw/h, pixelformat,
        ...). Excludes the reload-control/structural pars (handled by is_persisted_par).

        Pure predicate: Core.is_persisted_par(name, is_custom, style).
        """
        loader = loader or self.loader_op
        out = []
        try:
            all_pars = loader.pars()
        except Exception:  # noqa: BLE001
            return out
        for p in all_pars:
            try:
                name = p.name
                is_custom = getattr(p, 'isCustom', False)
                style = getattr(p, 'style', '')
            except Exception:  # noqa: BLE001
                continue
            if self.core.is_persisted_par(name, is_custom, style):
                out.append(p)
        return out

    def _snapshot_loader_values(self, loader=None):
        """[(name, mode_name, value)] for every persisted loader parameter (pulses skipped).
        Built-in pars are included so a fresh-node reload no longer resets timeslice / output
        resolution / pixel format / etc. Classification is pure: Core.is_persisted_par /
        snapshot_par / normalize_par_mode (unit-tested in LoaderParPersistenceTests).
        """
        par_mode = _par_mode()
        snap = []
        for p in self._persisted_loader_pars(loader):
            try:
                name = p.name
                style = getattr(p, 'style', '')
                mode = self.core.normalize_par_mode(getattr(p, 'mode', None), par_mode)
                val = getattr(p, 'val', None)
                expr = getattr(p, 'expr', None)
                bind = getattr(p, 'bindExpr', None)
            except Exception as e:  # noqa: BLE001
                self._debug('Reload', f'skipped snapshot of {p.name}: {e}')
                continue
            rec = self.core.snapshot_par(name, style, mode, val, expr, bind)
            if rec is not None:
                snap.append(rec)
        return snap

    def _restore_loader_values(self, snapshot, target=None):
        """target defaults to the current loader_op (the freshly loaded node).

        Each record goes through Core.restore_action (apply_value / apply_expr /
        apply_bind / skip); vanished menu entries are skipped with a reason, not errors.
        Returns (restored_count, skipped_list).
        """
        target = target or self.loader_op
        par_mode = _par_mode()
        restored, skipped = 0, []
        for name, kind, value in snapshot:
            p = getattr(target.par, name, None)
            if p is None:
                skipped.append(name)
                continue
            try:
                menu_names = tuple(getattr(p, 'menuNames', ()) or ())
                action, detail = self.core.restore_action(name, kind, value, menu_names)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{name} ({e})")
                continue
            if action == 'skip':
                skipped.append(f"{name}={value!r} ({detail})")
                continue
            try:
                if action == 'apply_expr':
                    p.expr = value
                    if par_mode is not None: p.mode = par_mode.EXPRESSION
                elif action == 'apply_bind':
                    p.bindExpr = value
                    if par_mode is not None: p.mode = par_mode.BIND
                else:
                    p.val = value
                restored += 1
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{name} ({e})")
        return restored, skipped

    def _recreate_loader_node(self):
        """Replace the loader op by a fresh node of the same type, keeping the 'plugin_loader' name,
        position and wires. Tolerates a stale 'plugin_loader_fresh' left behind by a crashed prior
        reload (clears it first so create() doesn't silently suffix the name).

        Only called from _reload_loader_fresh when TD kept the old parameter objects across
        unload — a fresh node is the only way setupParameters() re-runs with new definitions
        (v2.1.1 hot-reload fix, discovered via Plugin_FFT's Planner=Patient menu).
        """
        old = self.loader_op
        if old is None:
            return None
        # Clear any abandoned fresh node from a previous (crashed) reload so create() doesn't silently
        # suffix the name, which would later break the name-based lookup in sync_custom_parameters().
        stale = self.ownerComp.op('plugin_loader_fresh')
        if stale is not None and getattr(stale, 'valid', False):
            try:
                stale.destroy()
            except Exception as e:  # noqa: BLE001
                self._debug('Reload', f'could not clear stale plugin_loader_fresh: {e}')
        new = self.ownerComp.create(type(old), 'plugin_loader_fresh')
        new.nodeX, new.nodeY = old.nodeX, old.nodeY
        for i, conn in enumerate(old.inputConnectors):
            for src in list(conn.connections):
                try:
                    src.connect(new.inputConnectors[i])
                except Exception as e:  # noqa: BLE001
                    self._debug('Reload', f'dropped input wire on recreate: {e}')
        for i, conn in enumerate(old.outputConnectors):
            for dst in list(conn.connections):
                try:
                    new.outputConnectors[i].connect(dst)
                except Exception as e:  # noqa: BLE001
                    self._debug('Reload', f'dropped output wire on recreate: {e}')
        old.destroy()
        # Keep the conventional 'plugin_loader' name: sync_custom_parameters() and the other
        # callbacks resolve the loader by this name (see _resolve in refresh/resync paths), so a
        # user-rename is not preserved across a fresh-node recreate. The stale-node cleanup above is
        # the regression fix that matters here (a crashed reload used to leave 'plugin_loader_fresh'
        # behind and break the name-based lookup).
        new.name = 'plugin_loader'
        self.loader_op = new
        # A brand-new node inherits none of the old one's state - only the wires are copied above - so the
        # viewer flag that gets the loader cooked every frame (see create_plugin_loader) has to be set
        # again here. Dropping it on recreate is precisely how a reload can leave behind a node that never
        # cooks again, and with it an info popup that never fills in again.
        new.viewer = True
        return new

    def _ensure_loader_live(self):
        """Re-assert the two things the loader's information output depends on.

        Both are cheap, idempotent, and have each been the difference between a node whose info popup is
        full and one whose popup comes up empty with no other symptom: the plugin has to actually be
        loaded (unloadplugin off), and the node has to be cooked every frame (viewer on - see
        create_plugin_loader for why that pull is the only kick-start this host has).

        Called from onPluginname (failed-manifest path) and at the end of every _reload_loader_fresh
        branch including rollback.
        """
        if self.loader_op is None or not getattr(self.loader_op, 'valid', True):
            return
        try:
            self.loader_op.viewer = True
        except Exception as e:  # noqa: BLE001
            self._debug('Reload', f'could not set the loader viewer flag: {e}')
        try:
            pars = self.loader_op.par
            have_plugin = hasattr(pars, 'plugin') and bool(str(pars.plugin.eval() or '').strip())
            if have_plugin and hasattr(pars, 'unloadplugin') and pars.unloadplugin.eval():
                pars.unloadplugin = False
                self._debug('Reload', 'loader was left unloaded with a plugin path set; plugin reloaded')
            self.loader_op.cook(force=True)
        except Exception as e:  # noqa: BLE001
            self._debug('Reload', f'could not re-assert the loader plugin: {e}')

    def _reload_loader_fresh(self, plugin_path):
        """Unload -> (recreate node if needed) -> load plugin_path, restoring the parameter values.
        Hardened: re-resolves the loader defensively, snapshots the previous DLL path so a failed
        map can be rolled back, and guards each non-recoverable step with try/except + _set_error.

        Steps (numbered in-body): 1 unload+cook, 2 recreate if custom pars survived,
        3 map new DLL, 4 reinit pulse, 5 restore snapshot + log parameter_definition_diff.
        Every path ends in _ensure_loader_live().
        """
        # Re-resolve in case the COMP was edited underneath us between schedule and cook.
        self.loader_op = self.ownerComp.op('plugin_loader')
        if self.loader_op is None or not getattr(self.loader_op, 'valid', True):
            self._set_error('Reload skipped: plugin_loader node not found.')
            return

        before = {p.name: tuple(getattr(p, 'menuNames', ()) or ()) for p in self._custom_loader_pars()}
        snapshot = self._snapshot_loader_values()
        previous_dll = None
        try:
            previous_dll = self.loader_op.par.plugin.eval() if hasattr(self.loader_op.par, 'plugin') else None
        except Exception:  # noqa: BLE001
            previous_dll = None

        # 1. Unload Plugin ON + cook: TouchDesigner destroys the plugin instance and releases the DLL image
        try:
            self.loader_op.par.unloadplugin = True
            self.loader_op.cook(force=True)
        except Exception as e:  # noqa: BLE001
            self._debug('Reload', f'unload failed: {e}')
        # 2. If the custom parameters survived the unload, only a fresh node gets fresh definitions
        if self._custom_loader_pars():
            self._debug('Reload', 'TouchDesigner kept the parameter objects across unload; recreating the loader node')
            self._recreate_loader_node()
        # 3. Unload Plugin OFF + cook: the new DLL is mapped and setupParameters() runs on a clean node
        load_error = None
        try:
            self.loader_op.par.plugin = plugin_path
            self.loader_op.par.unloadplugin = False
            self.loader_op.cook(force=True)
        except Exception as e:  # noqa: BLE001
            load_error = e
        # 4. Re-Init pulse + cook: a full instance re-creation on the freshly mapped DLL
        if load_error is None:
            try:
                if hasattr(self.loader_op.par, 'reinitpulse'):
                    self.loader_op.par.reinitpulse.pulse()
                elif hasattr(self.loader_op.par, 'reinit'):
                    self.loader_op.par.reinit.pulse()
                self.loader_op.cook(force=True)
            except Exception as e:  # noqa: BLE001
                load_error = e
        if load_error is not None:
            # Roll back to the previous DLL so the loader isn't left half-unloaded with no recovery.
            self._set_error(f'Reload failed to load {plugin_path}: {load_error}; rolling back.')
            try:
                if previous_dll is not None:
                    self.loader_op.par.plugin = previous_dll
                    self.loader_op.par.unloadplugin = False
                    self.loader_op.cook(force=True)
            except Exception as e2:  # noqa: BLE001
                self._debug('Reload', f'rollback also failed: {e2}')
            try:
                self._restore_loader_values(snapshot)
            except Exception as e3:  # noqa: BLE001
                self._debug('Reload', f'rollback value restore failed: {e3}')
            self._ensure_loader_live()
            return
        # 5. Put the user's values / expressions / bindings back
        restored, skipped = self._restore_loader_values(snapshot)
        after = {p.name: tuple(getattr(p, 'menuNames', ()) or ()) for p in self._custom_loader_pars()}
        added, removed, menus = self.core.parameter_definition_diff(before, after)
        if added or removed or menus:
            self._log('Reload', f"parameter definitions changed: added {added or '-'}, removed {removed or '-'}, "
                                f"menus changed {menus or '-'}")
        self._debug('Reload', f'{restored} parameter value(s) restored' + (f", skipped {skipped}" if skipped else ''))
        # Last step on every path through a reload, including the rollback above: leave the loader both
        # loaded and cooking, rather than trusting each caller to have done it.
        self._ensure_loader_live()

    def _do_copy_plugin(self, force=False):
        """
        Hot-swap the built DLL into __Plugins__/ with the rename-in-place trick and reload the loader.

        Main thread (cheap): lock / still-being-written check on the build output, then submit the
        staging as an in-process BuildRunner job. Runner thread: hash-gate, rename-in-place copy of the
        plugin DLL and its declared runtime DLLs (safe_replace_file, size+mtime fast path first).
        Main thread again, in on_done: _reload_loader_fresh() - the only step that must touch
        TouchDesigner. Nothing that reads or hashes files runs on the main thread any more, so a reload
        does not drop frames even with hundreds of MB of runtime DLLs next to the plugin.
        force=True (the Reloadplugin pulse) reloads even when the DLL hash is unchanged.
        """
        if self.loader_op is None or self.Pluginname == '':
            return
        build_path = self.build_path
        plugin_path = f"{self.plugin_dir}/{self.Pluginname}.dll"
        if not os.path.exists(build_path):
            self._log('Build→Copy', f"ERROR: Build output {build_path} does not exist.")
            return

        # linker still writing? (mtime moved since scheduling or file not openable)
        try:
            mtime = os.path.getmtime(build_path)
        except OSError:
            mtime = None
        if self.file_locked(build_path) or (self._build_mtime is not None and mtime is not None and mtime > self._build_mtime):
            self._build_mtime = mtime
            if self.open_attempts < 40:
                self.open_attempts += 1
                self._debug('Build→Copy', f"build output still changing/locked (attempt {self.open_attempts}/40), retrying...")
                run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=5)
                return
            self._set_error(f"{build_path} stayed locked; giving up on this reload (use 'Force Reload Plugin').")
            self.open_attempts = 0
            return
        self.open_attempts = 0
        if not self._ensure_runner():
            return

        core = self.core
        plugin_dir = self.plugin_dir
        runtime = self._runtime_dlls_in_bin()
        loaded_hash = self._loaded_dll_hash

        def stage(job, emit):
            # Runner thread: files only, no TouchDesigner API.
            t0 = time.perf_counter()
            new_hash = core.file_sha256(build_path)
            if not force and new_hash == loaded_hash and os.path.exists(plugin_path):
                job.result = {'skipped': True, 'hash': new_hash}
                return 0
            os.makedirs(plugin_dir, exist_ok=True)
            core.cleanup_old_files(plugin_dir)
            changed, note = core.safe_replace_file(build_path, plugin_path)
            notes = []
            for dll in runtime:
                dst = os.path.join(plugin_dir, os.path.basename(dll))
                _c, n = core.safe_replace_file(dll, dst)
                notes.append(f"{os.path.basename(dll)}: {n}")
            job.result = {'skipped': False, 'hash': new_hash, 'note': note, 'runtime': notes,
                          'size': os.path.getsize(plugin_path), 'ms': (time.perf_counter() - t0) * 1000.0}
            return 0

        def on_done(job):
            # Main thread.
            if job.cancelled:
                return
            if not job.ok:
                err = job.error
                if isinstance(err, PermissionError) and self.open_attempts < 20:
                    self.open_attempts += 1
                    self._debug('Build→Copy', f"destination locked ({err}); retrying (attempt {self.open_attempts}/20)")
                    run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=5)
                    return
                self.open_attempts = 0
                self._set_error(f"Copy failed: {err}")
                return
            self.open_attempts = 0
            r = job.result or {}
            if r.get('skipped'):
                self._debug('Build→Copy', 'DLL unchanged — reload skipped')
                return
            for n in r.get('runtime', []):
                self._debug('Build→Copy', f"  runtime {n}")
            self._log('Build→Copy', f"{os.path.basename(plugin_path)}: {r.get('note')} ({r.get('size')} bytes, "
                                    f"staged off-thread in {r.get('ms', 0.0):.1f} ms)")
            self._reload_loader_fresh(plugin_path)
            self._loaded_dll_hash = r.get('hash')
            try:
                self.ownerComp.par.Loadeddll = f"{os.path.basename(plugin_path)} @ {time.strftime('%H:%M:%S')}"
            except Exception:  # noqa: BLE001
                pass
            self._log('Build→Copy', f"Plugin reloaded from {plugin_path}")
            run("args[0].ext.PluginBuilderExt.sync_custom_parameters(verbose=True)", self.ownerComp, delayFrames=15)

        self.runner.submit(core.BuildJob('call', [], self.abs_working_dir, label=f'stage {self.Pluginname}',
                                         on_done=on_done, func=stage))
        self._schedule_poll()

    def OnSourceUpdate(self):
        """folder_source DAT callback: a .cpp/.h changed — recompile when Compile On Update is on."""
        if not self.CompileOnUpdate:
            self._debug('Source', 'source changed but Compile On Update is off')
            return
        self._log('Source', f"Source file changed, recompiling '{self.Pluginname}'...")
        self.compile_plugin()

    def OnCMakeListsUpdate(self):
        """CMakeLists DAT callback: force reconfigure (and compile when Compile On Update is on)."""
        if not os.path.exists(self.CMakeListsPath):
            self._log('CMake', f"CMakeLists.txt not found at {self.CMakeListsPath}")
            return
        self._log('CMake', "CMakeLists.txt changed, re-running CMake configure...")
        self.build_plugin(then_compile=self.CompileOnUpdate, force=True)

    # ========================================================================================== #
    #  BUILD RUNNER MANAGEMENT                                                                   #
    #  One BuildRunner per extension; env built by _env_factory (vcvarsall + PLUGINBUILDER_   #
    #  BUILD=1). Events drained on the main thread via the 'pb_poll' run() loop.               #
    # ========================================================================================== #

    def _env_factory(self):
        """Build the MSVC environment for BuildRunner: vcvarsall + ninja/cmake PATH + build env.

        Sets PLUGINBUILDER_BUILD=1 (so TDPlugin.cmake skips POST_BUILD deploy),
        PLUGIN_BUILDER_DIR (CMake include path), and TD_SAMPLES_DIR when available.
        Called on the runner's worker thread by BuildRunner._worker.
        """
        extra_path = [self.toolchain.get('ninja_dir', ''), self.toolchain.get('cmake_dir', '')]
        extra_env = {'PLUGINBUILDER_BUILD': '1', 'PLUGIN_BUILDER_DIR': self.PluginBuilderDir}
        samples = self.TDSamplesDir
        if samples:
            extra_env['TD_SAMPLES_DIR'] = samples
        return self.core.capture_vc_env(self.vcvarsall, self.settings['options'].get('Arch', 'x64') or 'x64',
                                        extra_path_dirs=extra_path, extra_env=extra_env)

    def _ensure_runner(self):
        """Start BuildRunner if needed (lazy). Returns False when the toolchain is invalid."""
        if self.runner is not None and self.runner.alive:
            return True
        if not self.PathsValid:
            self._set_error('Toolchain not configured (vcvarsall / ninja). See textport and settings.ini.')
            return False
        capture = self.ownerComp.par.Outputto.eval() != 'TOUCH_TEXT_CONSOLE'
        self.runner = self.core.BuildRunner(self._env_factory, capture_output=capture, log=lambda m: self._log('Runner', m))
        self.runner.start()
        self.process = self.runner        # legacy attribute
        self._log('Runner', f"Build runner started (vcvarsall={self.vcvarsall}, ninja={self.ninja_dir})")
        self._schedule_poll()
        return True

    def start_subprocess(self):
        """Legacy name: start the build runner (lazy; safe to call repeatedly).

        No-op (False) until the working dir exists — onPluginname calls this when loading.
        """
        if not os.path.exists(self.abs_working_dir):
            self._debug('Runner', f"not started — working dir does not exist: {self.abs_working_dir}")
            return False
        return self._ensure_runner()

    def close_subprocess(self):
        """Shut down the BuildRunner (legacy name kept for parexec/execute DATs)."""
        if self.runner is not None:
            self._log('Runner', "Shutting down build runner...")
            try:
                self.runner.shutdown(timeout=2.0)
            except Exception as e:  # noqa: BLE001
                self._log('Runner', f"shutdown error: {e}")
            self.runner = None
            self.process = None

    def SendCommand(self, command):
        """Run an arbitrary shell command inside the MSVC environment (legacy API).

        Wrapped as BuildJob('custom', ['cmd.exe','/c',command]) — not evaluated by a shell
        beyond cmd.exe itself; requires _ensure_runner().
        """
        if not self._ensure_runner():
            raise RuntimeError("Build runner could not be started.")
        self.runner.submit(self.core.BuildJob('custom', ['cmd.exe', '/c', command], self.abs_working_dir, label=command))
        self._schedule_poll()

    def _schedule_poll(self):
        """(Re)arm the one-frame _poll run() loop under group 'pb_poll' (kills any prior arm)."""
        for r in runs:
            if r.group == 'pb_poll':
                r.kill()
        run("args[0].ext.PluginBuilderExt._poll()", self.ownerComp, group='pb_poll', delayFrames=1)

    def _poll(self):
        """Drain runner events on the main thread; reschedules itself only while there is work.

        Output lines of one frame are joined into a single print, capped at 200 lines
        (Core.coalesce_output) - textport printing is not free, and a full build can emit hundreds of
        lines at once. The loop re-arms every frame while a job runs or events arrived, every 15 frames
        while the MSVC environment is still being captured, and stops when the runner is idle; every
        submit path calls _schedule_poll() again.
        """
        if self.runner is None:
            return
        lines = []
        events = self.runner.poll()
        for ev in events:
            kind = ev[0]
            if kind == 'line':
                lines.append(ev[2])
            elif kind == 'env':
                ok, msg = ev[1], ev[2]
                if ok:
                    self._log('Runner', msg)
                else:
                    self._set_error(f"MSVC environment failed: {msg}")
                    self._set_status('Toolchain error')
            elif kind == 'start':
                self._debug('Runner', f"▶ {ev[1].label}")
        if lines:
            text, _dropped = self.core.coalesce_output(lines, 200)
            print(text, end='')
        if self.runner is not None and self.runner.alive:
            if self.runner.busy or events:
                run("args[0].ext.PluginBuilderExt._poll()", self.ownerComp, group='pb_poll', delayFrames=1)
            elif not self.runner.env_ready:
                run("args[0].ext.PluginBuilderExt._poll()", self.ownerComp, group='pb_poll', delayFrames=15)
            # else: idle - no Python runs per frame until the next submit re-arms the loop

    def CheckAndPrintOutput(self):
        """Legacy alias for one _poll() drain (parexec-friendly name)."""
        self._poll()

    def GetOutput(self):
        """Pop queued stdout lines from the runner (does not run on_done side effects beyond poll)."""
        lines = []
        if self.runner is not None:
            for ev in self.runner.poll():
                if ev[0] == 'line':
                    lines.append(ev[2])
        return lines

    def PrintOutput(self):
        """Print everything GetOutput() would return."""
        for line in self.GetOutput():
            print(line, end='')
