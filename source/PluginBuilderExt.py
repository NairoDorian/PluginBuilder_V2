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
# =================================================================================================

import importlib.util
import json
import os
import shutil
import sys
import time

# -------------------------------------------------------------------------------------------------
# Module loading: prefer the on-disk source files under <PluginBuilderDir>/source so the .tox
# always runs the current code even when its embedded DATs are stale. Falls back to the DATs.
# -------------------------------------------------------------------------------------------------

def _load_module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


def _import_support_module(name, plugin_builder_dir):
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
    parameter's mode onto the PAR_* sentinels before calling the pure Core helpers."""
    pm = globals().get('ParMode')
    if pm is None:
        try:
            import td as _td  # type: ignore
            pm = getattr(_td, 'ParMode', None)
        except Exception:  # noqa: BLE001
            pm = None
    return pm


STATUS_PAGE = 'Status'


class PluginBuilderExt:
    """
    Creates, builds, compiles, hot-reloads and installs C++ plugins for TouchDesigner.

    Lifecycle:
        1. create_plugin()   — scaffold a project from a template (CMakeLists via TDPlugin.cmake, plugin.json,
                               .vscode/, launch.vs.json) and start the first configure+build
        2. build_plugin()    — cmake configure (Ninja)                      [BuildRunner job]
        3. compile_plugin()  — ninja build                                   [BuildRunner job]
        4. _do_copy_plugin() — hash-gated, rename-in-place copy of the DLL (+ runtime DLLs) into __Plugins__/
                               and re-init of the plugin_loader op; then parameter sync
        5. install_plugin()  — copy to ~/Documents/Derivative/Plugins/<name>/
    """

    # ========================================================================================== #
    #  INITIALIZATION                                                                            #
    # ========================================================================================== #

    def __init__(self, ownerComp):
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
        self.runner = None
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

        # --- dispatch maps ---
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

        # --- directory conventions ---
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
        try:
            self.close_subprocess()
        except Exception:  # noqa: BLE001
            pass

    # ========================================================================================== #
    #  LOGGING / STATUS                                                                          #
    # ========================================================================================== #

    @staticmethod
    def _log(tag, message):
        print(f"[{tag}] {message}")

    def _debug(self, tag, message):
        if self.verbose:
            print(f"[{tag}] {message}")

    def _ensure_status_pars(self):
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
        except Exception as e:  # noqa: BLE001
            self._debug('Init', f'status pars not created: {e}')

    def _set_status(self, text):
        self._last_status = text
        try:
            self.ownerComp.par.Buildstatus = text
        except Exception:  # noqa: BLE001
            pass

    def _set_error(self, message):
        self._log('Build', f'ERROR: {message}')
        for fn in ('addError', 'addScriptError'):
            try:
                getattr(self.ownerComp, fn)(message)
                break
            except Exception:  # noqa: BLE001
                continue

    def _set_warning(self, message):
        self._log('Build', f'WARNING: {message}')
        for fn in ('addWarning', 'addScriptWarning'):
            try:
                getattr(self.ownerComp, fn)(message)
                break
            except Exception:  # noqa: BLE001
                continue

    def _clear_errors(self):
        for fn in ('clearScriptErrors',):
            try:
                getattr(self.ownerComp, fn)(recurse=False)
            except Exception:  # noqa: BLE001
                pass

    def _errors_dat(self):
        dat = self.builderComp.op('build_errors')
        if dat is None:
            try:
                dat = self.builderComp.create(tableDAT, 'build_errors')
                dat.nodeX, dat.nodeY = 400, -200
            except Exception:  # noqa: BLE001
                return None
        return dat

    def _publish_diagnostics(self, diags):
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
    # ========================================================================================== #

    @staticmethod
    def _peek_plugin_builder_dir(settings_text):
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
    # ========================================================================================== #

    def check_paths(self):
        """Resolve the toolchain (settings.ini > vswhere > PATH > VS-bundled). Never raises."""
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
        samples = self.TDSamplesDir
        if not samples or not os.path.isdir(samples) or not self.sdk_versions:
            return
        installed = self.core.installed_sdk_versions(samples)
        msg = self.core.sdk_mismatch_message(self.sdk_versions, installed)
        if msg:
            self._log('Init', 'WARNING: ' + msg)
            self._set_warning(msg)

    def _populate_template_menu(self):
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
    # ========================================================================================== #

    def create_plugin(self):
        """Scaffold a new plugin project from a template and kick off the first configure + build."""
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
            extra = []
            if 'cuda' not in meta.get('features', []):
                extra.append(f'# td_plugin_optimize({name} AVX2 FAST_MATH)')
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
            os.makedirs(f"{self.working_dir}/source")
            replacements = self.core.template_replacements(
                name, op_type, name, op_icon,
                self.settings['plugininfo']['Author'], self.settings['plugininfo']['Email'],
                self.settings['plugininfo'].get('HelpURL', ''))
            src_dir = os.path.join(meta['dir'], 'source')
            files = sorted(os.listdir(src_dir))
            for file_name in files:
                with open(os.path.join(src_dir, file_name), 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
                text = self.core.render_source(text, meta.get('replace', template_name), name, replacements)
                out_name = file_name.replace(meta.get('replace', template_name), name)
                with open(f"{self.working_dir}/source/{out_name}", 'w', encoding='utf-8') as f:
                    f.write(text)
            self._log('Create', f"Copied {len(files)} template source file(s)")

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
        try:
            return globals().get(f"{op_type.lower()}{family}") is not None
        except Exception:  # noqa: BLE001
            return False

    def _write_launch_vs_json(self):
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
        toe = f"{project.folder}/{self.TDProjectName}.toe"
        files = self.core.render_vscode_files(self.Pluginname, self.TDPath, toe, project.folder,
                                              self.include_dir.replace('\\', '/'))
        vs_dir = f"{self.working_dir}/.vscode"
        os.makedirs(vs_dir, exist_ok=True)
        for fn, data in files.items():
            with open(os.path.join(vs_dir, fn), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)

    def destroy_children(self):
        preserved = ['builder', 'source', 'CMakeLists']
        for child in self.ownerComp.findChildren(depth=1):
            if child.name not in preserved:
                child.destroy()

    def create_plugin_loader(self, plugin_type):
        """Create the loader chain (optional in → cplusplus loader → out) for a family."""
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

    def assemble_cmake_text_basic(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'CHOP', self.PluginBuilderDir)

    def assemble_cmake_text_cuda(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'TOP', self.PluginBuilderDir, features=['cuda'])

    def assemble_cmake_text_python(self):
        return self.cmake_blocks.assemble(self.Pluginname, 'CHOP', self.PluginBuilderDir, features=['python'])

    # ========================================================================================== #
    #  BUILD & COMPILE                                                                           #
    # ========================================================================================== #

    def _configure_args(self):
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
        args = ['ninja', '-C', 'build']
        jobs = str(self.settings['options'].get('ParallelJobs', '')).strip()
        if jobs.isdigit():
            args += ['-j', jobs]
        return args

    def _cmake_inputs_hash(self):
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
        """CMake configure. Skipped when nothing relevant changed unless force=True."""
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
        """Ninja build. Coalesces repeated requests while a build is running."""
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
        if self.runner.pending_count > 0:
            # A configure (or build) is already queued; the existing queue chains to a build, so
            # don't stack a duplicate build on top of it.
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
        self.build_plugin(then_compile=True, force=True)

    def cancel_build(self):
        if self.runner is None:
            return
        n = self.runner.cancel_pending()
        killed = self.runner.kill_current()
        self._rebuild_after_current = False
        self._log('Build', f"cancelled {n} pending job(s){', killed running job' if killed else ''}")
        self._set_status('Cancelled')

    def clean_build(self):
        build_dir = os.path.join(self.abs_working_dir, 'build')
        if self.runner is not None:
            self.cancel_build()
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
            self._log('Build', f"removed {build_dir}")
        self._last_configure_hash = None
        self._set_status('Clean')

    def run_tests(self):
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
        self.runner.submit(self.core.BuildJob('test', ['ctest', '--test-dir', 'build', '--output-on-failure'],
                                              self.abs_working_dir, label='ctest', on_done=on_done))
        self._schedule_poll()

    def RefreshDats(self):
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
    # ========================================================================================== #

    def clear_plugin_builder(self):
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
    # ========================================================================================== #

    def install_plugin(self):
        self._log('Install', f"Installing '{self.Pluginname}' from {self.plugin_dir}")
        if not os.path.exists(self.plugin_dir):
            self._set_error(f"Source folder {self.plugin_dir} does not exist.")
            return
        install_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Derivative', 'Plugins')
        os.makedirs(install_dir, exist_ok=True)
        dest = os.path.join(install_dir, self.Pluginname)
        os.makedirs(dest, exist_ok=True)
        self.core.cleanup_old_files(dest)
        copied = 0
        for root, _dirs, files in os.walk(self.plugin_dir):
            for fn in files:
                if fn.endswith('.old') or '.old' in fn[-6:]:
                    continue
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, self.plugin_dir)
                dst = os.path.join(dest, rel)
                try:
                    changed, note = self.core.safe_replace_file(src, dst)
                    self._log('Install', f"  {rel}: {note}")
                    copied += int(changed)
                except Exception as e:  # noqa: BLE001
                    self._set_error(f"Could not install {rel}: {e}")
                    return
        self._log('Install', f"INSTALLATION COMPLETE: {copied} file(s) updated in {dest}")
        self._set_status(f'Installed → {dest}')

    # ========================================================================================== #
    #  UI HELPERS                                                                                #
    # ========================================================================================== #

    def disable_create_pars(self):
        self.ownerComp.par.Createplugin.enable = False
        self.ownerComp.par.Pluginname.readOnly = True
        self.ownerComp.par.Plugintemplate.readOnly = True
        self.ownerComp.par.Createinputop.enable = False

    def EnableCreatePars(self):
        self.ownerComp.par.Createplugin.enable = True
        self.ownerComp.par.Pluginname.readOnly = False
        self.ownerComp.par.Plugintemplate.readOnly = False
        self.ownerComp.par.Createinputop.enable = True

    def PostCreatePlugin(self):
        if self.CMakeListsDat is not None:
            self.CMakeListsDat.cook(force=True)

    def file_locked(self, filepath):
        """True if the file can't be opened for writing (a DLL mapped by a process denies write access)."""
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
    # ========================================================================================== #

    @staticmethod
    def _get_custom_page(op, page_name):
        if op is None or not getattr(op, 'valid', True):
            return None
        for page in getattr(op, 'customPages', []):
            if page.name == page_name:
                return page
        return None

    def _mirrored_page_names(self):
        try:
            names = self.ownerComp.fetch('PB_mirror_pages', [])
        except Exception:  # noqa: BLE001
            names = []
        names = list(names) if isinstance(names, (list, tuple)) else []
        if 'Custom' not in names:
            names.append('Custom')
        return names

    def _store_mirrored_pages(self, names):
        try:
            self.ownerComp.store('PB_mirror_pages', list(names))
        except Exception:  # noqa: BLE001
            pass

    def _own_page_names(self):
        """Pages that belong to the builder UI itself (never mirrored into)."""
        mirrored = set(self._mirrored_page_names())
        return {p.name for p in self.ownerComp.customPages if p.name not in mirrored}

    @staticmethod
    def _copy_par_attributes(src_p, dst_p):
        # menus first so that a menu 'val' is accepted afterwards
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
        """Return list of (tuplet_name, page_name, style, components[list of Par], label, menu_names)."""
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
        if not self.mirror_pages:
            page_name = 'Custom'
        elif page_name in own_pages:
            page_name = f'{page_name} (Plugin)'
        page = self._get_custom_page(self.ownerComp, page_name)
        if page is None:
            page = self.ownerComp.appendCustomPage(page_name)
        return page

    def _create_mirror_par(self, page, tname, style, size, label):
        """Create a parameter of the same style on the mirror page. Returns the (first) Par or None."""
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
        """Mirror the loader's custom parameters (pages preserved) onto ownerComp with BIND expressions."""
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
    # ========================================================================================== #

    def OnParValueChange(self, par, prev):
        if par.name in self.on_par_value_change_map:
            self.on_par_value_change_map[par.name](par.eval(), prev)
        elif self.loader_op is not None and getattr(self.loader_op, 'valid', True):
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
        self._log('Runner', f"Output mode changed: '{prev}' → '{value}', restarting build runner...")
        self.close_subprocess()
        self.start_subprocess()

    def onPluginname(self, value, prev):
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
        self.RefreshDats()

    # ========================================================================================== #
    #  FILE CHANGE CALLBACKS                                                                     #
    # ========================================================================================== #

    def OnPluginUpdate(self):
        """folder_bin DAT callback: something changed in build/bin — schedule a hash-gated copy."""
        if self.loader_op is None or self.Pluginname == '':
            return
        if not os.path.exists(self.build_path):
            self._debug('Build→Copy', f"DLL not yet produced ({self.build_path})")
            return
        self._schedule_copy()

    def _schedule_copy(self, delay_frames=10):
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
        bin_dir = self.CurrentBinDir
        out = []
        if os.path.isdir(bin_dir):
            for fn in os.listdir(bin_dir):
                if fn.lower().endswith('.dll') and fn != f'{self.Pluginname}.dll':
                    out.append(os.path.join(bin_dir, fn))
        return out

    # ---------------- loader reload with fresh parameter definitions ----------------------------- #
    #
    # TouchDesigner keeps the parameter OBJECTS of a C++ OP across a plugin re-init: setupParameters()
    # adds parameters that did not exist yet, but an existing parameter keeps its stored definition —
    # changed menu entries, labels, ranges or defaults only show up on a node that is created fresh
    # (which is what a TouchDesigner restart does). So a reload first unloads the plugin, checks
    # whether that removed the custom parameters, and if TouchDesigner kept them recreates the loader
    # node (same name, same wires). Values / expressions / bindings are snapshotted and restored.

    def _custom_loader_pars(self, loader=None):
        """Custom parameters only (used for the menu-diff log and COMP mirroring)."""
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
        ...). Excludes the reload-control/structural pars (handled by is_persisted_par)."""
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
        resolution / pixel format / etc."""
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
        """target defaults to the current loader_op (the freshly loaded node)."""
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
        reload (clears it first so create() doesn't silently suffix the name)."""
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
        return new

    def _reload_loader_fresh(self, plugin_path):
        """Unload -> (recreate node if needed) -> load plugin_path, restoring the parameter values.
        Hardened: re-resolves the loader defensively, snapshots the previous DLL path so a failed
        map can be rolled back, and guards each non-recoverable step with try/except + _set_error."""
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
            return
        # 5. Put the user's values / expressions / bindings back
        restored, skipped = self._restore_loader_values(snapshot)
        after = {p.name: tuple(getattr(p, 'menuNames', ()) or ()) for p in self._custom_loader_pars()}
        added, removed, menus = self.core.parameter_definition_diff(before, after)
        if added or removed or menus:
            self._log('Reload', f"parameter definitions changed: added {added or '-'}, removed {removed or '-'}, "
                                f"menus changed {menus or '-'}")
        self._debug('Reload', f'{restored} parameter value(s) restored' + (f", skipped {skipped}" if skipped else ''))

    def _do_copy_plugin(self, force=False):
        """
        Hot-swap the built DLL into __Plugins__/ with the rename-in-place trick and reload the loader.

        The DLL file is replaced with the rename trick (a mapped DLL can be renamed on Windows, so the old
        image stays valid until the loader lets go of it); the loader is then reloaded with fresh parameter
        definitions by _reload_loader_fresh(). If the build output is still being written (linker lock) the
        copy is retried a few frames later.
        """
        if self.loader_op is None or self.Pluginname == '':
            return
        build_path = self.build_path
        plugin_path = f"{self.plugin_dir}/{self.Pluginname}.dll"
        if not os.path.exists(build_path):
            self._log('Build→Copy', f"ERROR: Build output {build_path} does not exist.")
            return

        # linker still writing? (size/mtime moved since scheduling or file not openable)
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

        try:
            new_hash = self.core.file_sha256(build_path)
        except OSError as e:
            self._set_error(f"cannot hash {build_path}: {e}")
            return
        if not force and new_hash == self._loaded_dll_hash and os.path.exists(plugin_path):
            self._debug('Build→Copy', 'DLL unchanged — reload skipped')
            return

        os.makedirs(self.plugin_dir, exist_ok=True)
        self.core.cleanup_old_files(self.plugin_dir)
        try:
            changed, note = self.core.safe_replace_file(build_path, plugin_path)
            for dll in self._runtime_dlls_in_bin():
                dst = os.path.join(self.plugin_dir, os.path.basename(dll))
                _c, n = self.core.safe_replace_file(dll, dst)
                self._debug('Build→Copy', f"  runtime {os.path.basename(dll)}: {n}")
        except PermissionError as e:
            if self.open_attempts < 20:
                self.open_attempts += 1
                self._debug('Build→Copy', f"destination locked ({e}); retrying (attempt {self.open_attempts}/20)")
                run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=5)
                return
            self._set_error(f"Could not replace {plugin_path}: {e}")
            self.open_attempts = 0
            return
        except Exception as e:  # noqa: BLE001
            self._set_error(f"Copy failed: {e}")
            return

        self._log('Build→Copy', f"{os.path.basename(plugin_path)}: {note} ({os.path.getsize(plugin_path)} bytes)")
        self._reload_loader_fresh(plugin_path)
        self._loaded_dll_hash = new_hash
        try:
            self.ownerComp.par.Loadeddll = f"{os.path.basename(plugin_path)} @ {time.strftime('%H:%M:%S')}"
        except Exception:  # noqa: BLE001
            pass
        self._log('Build→Copy', f"Plugin reloaded from {plugin_path}")
        run("args[0].ext.PluginBuilderExt.sync_custom_parameters(verbose=True)", self.ownerComp, delayFrames=15)

    def OnSourceUpdate(self):
        if not self.CompileOnUpdate:
            self._debug('Source', 'source changed but Compile On Update is off')
            return
        self._log('Source', f"Source file changed, recompiling '{self.Pluginname}'...")
        self.compile_plugin()

    def OnCMakeListsUpdate(self):
        if not os.path.exists(self.CMakeListsPath):
            self._log('CMake', f"CMakeLists.txt not found at {self.CMakeListsPath}")
            return
        self._log('CMake', "CMakeLists.txt changed, re-running CMake configure...")
        self.build_plugin(then_compile=self.CompileOnUpdate, force=True)

    # ========================================================================================== #
    #  BUILD RUNNER MANAGEMENT                                                                   #
    # ========================================================================================== #

    def _env_factory(self):
        extra_path = [self.toolchain.get('ninja_dir', ''), self.toolchain.get('cmake_dir', '')]
        extra_env = {'PLUGINBUILDER_BUILD': '1', 'PLUGIN_BUILDER_DIR': self.PluginBuilderDir}
        samples = self.TDSamplesDir
        if samples:
            extra_env['TD_SAMPLES_DIR'] = samples
        return self.core.capture_vc_env(self.vcvarsall, self.settings['options'].get('Arch', 'x64') or 'x64',
                                        extra_path_dirs=extra_path, extra_env=extra_env)

    def _ensure_runner(self):
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
        """Legacy name: start the build runner (lazy; safe to call repeatedly)."""
        if not os.path.exists(self.abs_working_dir):
            self._debug('Runner', f"not started — working dir does not exist: {self.abs_working_dir}")
            return False
        return self._ensure_runner()

    def close_subprocess(self):
        if self.runner is not None:
            self._log('Runner', "Shutting down build runner...")
            try:
                self.runner.shutdown(timeout=2.0)
            except Exception as e:  # noqa: BLE001
                self._log('Runner', f"shutdown error: {e}")
            self.runner = None
            self.process = None

    def SendCommand(self, command):
        """Run an arbitrary shell command inside the MSVC environment (legacy API)."""
        if not self._ensure_runner():
            raise RuntimeError("Build runner could not be started.")
        self.runner.submit(self.core.BuildJob('custom', ['cmd.exe', '/c', command], self.abs_working_dir, label=command))
        self._schedule_poll()

    def _schedule_poll(self):
        for r in runs:
            if r.group == 'pb_poll':
                r.kill()
        run("args[0].ext.PluginBuilderExt._poll()", self.ownerComp, group='pb_poll', delayFrames=1)

    def _poll(self):
        """Drain runner events on the main thread; reschedules itself while the runner is alive."""
        if self.runner is None:
            return
        for ev in self.runner.poll():
            kind = ev[0]
            if kind == 'line':
                print(ev[2], end='')
            elif kind == 'env':
                ok, msg = ev[1], ev[2]
                if ok:
                    self._log('Runner', msg)
                else:
                    self._set_error(f"MSVC environment failed: {msg}")
                    self._set_status('Toolchain error')
            elif kind == 'start':
                self._debug('Runner', f"▶ {ev[1].label}")
        if self.runner is not None and self.runner.alive:
            delay = 1 if self.runner.busy else 15
            run("args[0].ext.PluginBuilderExt._poll()", self.ownerComp, group='pb_poll', delayFrames=delay)

    def CheckAndPrintOutput(self):
        self._poll()

    def GetOutput(self):
        lines = []
        if self.runner is not None:
            for ev in self.runner.poll():
                if ev[0] == 'line':
                    lines.append(ev[2])
        return lines

    def PrintOutput(self):
        for line in self.GetOutput():
            print(line, end='')
