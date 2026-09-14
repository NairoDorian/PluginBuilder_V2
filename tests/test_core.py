"""
Headless unit tests for PluginBuilderCore (no TouchDesigner required).

    cd PluginBuilder_V2
    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'source'))

import PluginBuilderCore as core  # noqa: E402
import CMakeBlocks  # noqa: E402


class NamingTests(unittest.TestCase):
    def test_validate_plugin_name(self):
        self.assertTrue(core.validate_plugin_name('FFT')[0])
        self.assertTrue(core.validate_plugin_name('My_Plugin2')[0])
        self.assertFalse(core.validate_plugin_name('')[0])
        self.assertFalse(core.validate_plugin_name('2fast')[0])
        self.assertFalse(core.validate_plugin_name('my plugin')[0])
        self.assertFalse(core.validate_plugin_name('build')[0])

    def test_sanitize_op_type(self):
        self.assertEqual(core.sanitize_op_type('FFT'), 'Fft')
        self.assertEqual(core.sanitize_op_type('FFT_v2'), 'Fftv2')
        self.assertEqual(core.sanitize_op_type('myCHOP'), 'Mychop')
        self.assertEqual(core.sanitize_op_type('123abc'), 'Abc')
        self.assertEqual(core.sanitize_op_type('___'), 'Customop')

    def test_make_op_icon(self):
        self.assertEqual(core.make_op_icon('FFT'), 'FFT')
        self.assertEqual(core.make_op_icon('a'), 'AXX')
        self.assertEqual(core.make_op_icon('my_plugin'), 'MYP')

    def test_td_project_name(self):
        self.assertEqual(core.td_project_name('Plugin_FFT.94.toe'), 'Plugin_FFT')
        self.assertEqual(core.td_project_name('Plugin_FFT.toe'), 'Plugin_FFT')
        self.assertEqual(core.td_project_name('my.project.12.toe'), 'my.project')
        self.assertEqual(core.td_project_name('demo'), 'demo')


class SettingsTests(unittest.TestCase):
    def test_parse_settings_ok(self):
        text = """
[Paths]
PluginBuilderDir = ${USER_PATH}/PB
NinjaDir = C:\\ninja
VCVarsall = C:\\Program Files\\VS\\vcvarsall.bat
[PluginInfo]
Author = Me
Email = me@x.com
[Options]
LogLevel = debug
"""
        s = core.parse_settings(text, 'C:\\Users\\me')
        self.assertEqual(s['errors'], [])
        self.assertEqual(s['paths']['PluginBuilderDir'], os.path.normpath('C:\\Users\\me/PB'))
        self.assertEqual(s['plugininfo']['Author'], 'Me')
        self.assertEqual(s['options']['LogLevel'], 'debug')
        self.assertFalse(s['dev_mode'])

    def test_parse_settings_never_raises(self):
        s = core.parse_settings('this is not ini\n[[[', 'C:\\Users\\me')
        self.assertTrue(s['errors'])
        s = core.parse_settings('', 'C:\\Users\\me')
        self.assertEqual(s['errors'], [])
        self.assertEqual(s['paths']['PluginBuilderDir'], '')

    def test_dev_mode(self):
        s = core.parse_settings('[DevMode]\nx=1\n', 'h')
        self.assertTrue(s['dev_mode'])


class TemplateTests(unittest.TestCase):
    def test_load_templates_from_repo(self):
        templates = core.load_templates(os.path.join(ROOT, 'templates'))
        self.assertIn('BasicCHOP', templates)
        self.assertEqual(templates['BasicCHOP']['family'], 'CHOP')
        for name, meta in templates.items():
            self.assertIn(meta['family'], core.FAMILIES, name)
            self.assertTrue(meta['sources'], name)
            main = f"{meta.get('replace', name)}.cpp"
            self.assertIn(main, meta['sources'], f'{name} must contain {main}')

    def test_render_source(self):
        text = 'class BasicCHOP { }; // BASICCHOP\nopType->setString("#__OP_TYPE__#");'
        rep = core.template_replacements('Fft', 'Fftcustom', 'FFT', 'FFT', 'A', 'e', 'url')
        out = core.render_source(text, 'BasicCHOP', 'Fft', rep)
        self.assertIn('class Fft {', out)
        self.assertIn('// FFT', out)
        self.assertIn('"Fftcustom"', out)

    def test_templates_have_all_placeholders(self):
        templates = core.load_templates(os.path.join(ROOT, 'templates'))
        for name, meta in templates.items():
            main = os.path.join(meta['dir'], 'source', f"{meta.get('replace', name)}.cpp")
            with open(main, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            for ph in ('#__OP_TYPE__#', '#__OP_LABEL__#', '#__OP_ICON__#', '#__OP_AUTHOR__#', '#__OP_EMAIL__#'):
                self.assertIn(ph, text, f'{name}: missing {ph}')
            self.assertIn('setAPIVersion(', text, f'{name}: not an API-10 style template')


class CMakeTests(unittest.TestCase):
    def test_assemble(self):
        text = CMakeBlocks.assemble('Fft', 'CHOP', 'C:/PB', features=['python'], extra_lines=['# hi'])
        self.assertTrue(text.startswith("# {'plugin_type': 'CHOP'}"))
        self.assertIn('project(Fft LANGUAGES CXX)', text)
        self.assertIn('td_add_plugin(Fft FAMILY CHOP FEATURES python)', text)
        self.assertIn('include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)', text)
        self.assertIn('set(PLUGIN_BUILDER_DIR "C:/PB")', text)
        self.assertTrue(text.rstrip().endswith('# hi'))

    def test_assemble_no_features(self):
        text = CMakeBlocks.assemble('X', 'POP', 'C:/PB')
        self.assertIn('td_add_plugin(X FAMILY POP)', text)

    def test_module_exists(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'cmake', 'TDPlugin.cmake')))
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'cmake', 'TDDeploy.cmake')))


class ManifestTests(unittest.TestCase):
    def test_header_parse(self):
        self.assertEqual(core.plugin_type_from_cmake_header("# {'plugin_type': 'CHOP'}\n"), 'CHOP')
        self.assertIsNone(core.plugin_type_from_cmake_header("# {'plugin_type': 'XXX'}"))
        self.assertIsNone(core.plugin_type_from_cmake_header("cmake_minimum_required(VERSION 3.21)"))
        self.assertIsNone(core.plugin_type_from_cmake_header("# __import__('os')"))

    def test_manifest_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(core.read_plugin_manifest(d))
            with open(os.path.join(d, 'CMakeLists.txt'), 'w') as f:
                f.write("# {'plugin_type': 'SOP'}\nproject(x)\n")
            m = core.read_plugin_manifest(d)
            self.assertEqual(m['family'], 'SOP')
            self.assertTrue(m.get('legacy'))
            core.write_plugin_manifest(d, {'name': 'x', 'family': 'TOP'})
            self.assertEqual(core.read_plugin_manifest(d)['family'], 'TOP')
        finally:
            shutil.rmtree(d)


class DiagnosticsTests(unittest.TestCase):
    def test_msvc_and_linker(self):
        lines = [
            'C:/proj/source/FFT.cpp(123,45): error C2065: \'foo\': undeclared identifier\n',
            'C:/proj/source/FFT.cpp(123,45): error C2065: \'foo\': undeclared identifier\n',  # duplicate
            'C:/proj/source/DSP.h(10): warning C4244: conversion\n',
            'FFT.obj : error LNK2019: unresolved external symbol fftwf_plan\n',
            'ninja: build stopped: subcommand failed.\n',
            'ninja: error: loading \'build.ninja\': No such file\n',
        ]
        d = core.parse_diagnostics(lines)
        self.assertEqual(len(d), 4)
        self.assertEqual(d[0]['line'], 123)
        self.assertEqual(d[0]['col'], 45)
        self.assertEqual(d[0]['code'], 'C2065')
        self.assertEqual(d[1]['severity'], 'warning')
        self.assertEqual(d[2]['code'], 'LNK2019')
        self.assertEqual(d[3]['code'], 'NINJA')
        self.assertEqual(core.summarize_diagnostics(d), (3, 1))

    def test_cmake(self):
        lines = ['CMake Error at CMakeLists.txt:12 (include):\n', '  include could not find requested file:\n', '\n',
                 'CMake Error: The source directory does not exist.\n']
        d = core.parse_diagnostics(lines)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[0]['line'], 12)
        self.assertIn('could not find', d[0]['message'])


class FileTests(unittest.TestCase):
    def test_safe_replace_file(self):
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, 'new.dll')
            dst = os.path.join(d, 'out', 'p.dll')
            with open(src, 'wb') as f:
                f.write(b'v1')
            changed, note = core.safe_replace_file(src, dst)
            self.assertTrue(changed)
            self.assertEqual(note, 'new')
            changed, note = core.safe_replace_file(src, dst)
            self.assertFalse(changed)
            self.assertEqual(note, 'unchanged')
            with open(src, 'wb') as f:
                f.write(b'v2')
            changed, note = core.safe_replace_file(src, dst)
            self.assertTrue(changed)
            self.assertTrue(os.path.exists(dst + '.old'))
            with open(dst, 'rb') as f:
                self.assertEqual(f.read(), b'v2')
            self.assertEqual(core.cleanup_old_files(os.path.dirname(dst)), 1)
        finally:
            shutil.rmtree(d)


class SdkTests(unittest.TestCase):
    def test_read_sdk_versions(self):
        v = core.read_sdk_versions(os.path.join(ROOT, 'include'))
        self.assertGreaterEqual(v.get('CHOP', 0), 10)
        self.assertEqual(v.get('Common'), 2)
        self.assertIn('POP', v)

    def test_mismatch_message(self):
        self.assertEqual(core.sdk_mismatch_message({'CHOP': 10}, {'CHOP': 10}), '')
        self.assertIn('CHOP: 9 vs installed 10', core.sdk_mismatch_message({'CHOP': 9}, {'CHOP': 10}))


class RunnerTests(unittest.TestCase):
    def test_runner_runs_jobs_in_order_and_reports_exit_codes(self):
        env = dict(os.environ)
        runner = core.BuildRunner(lambda: env, capture_output=True, use_job_object=False)
        runner.start()
        results = []
        py = sys.executable
        j1 = core.BuildJob('custom', [py, '-c', 'print("hello"); import sys; sys.exit(0)'], os.getcwd(),
                           on_done=lambda j: results.append(('a', j.returncode, ''.join(j.output))))
        j2 = core.BuildJob('custom', [py, '-c', 'import sys; print("boom", file=sys.stderr); sys.exit(3)'], os.getcwd(),
                           on_done=lambda j: results.append(('b', j.returncode, ''.join(j.output))))
        runner.submit(j1)
        runner.submit(j2)
        import time
        t0 = time.time()
        while len(results) < 2 and time.time() - t0 < 60:
            runner.poll()
            time.sleep(0.02)
        runner.shutdown()
        self.assertEqual([r[0] for r in results], ['a', 'b'])
        self.assertEqual(results[0][1], 0)
        self.assertIn('hello', results[0][2])
        self.assertEqual(results[1][1], 3)
        self.assertIn('boom', results[1][2])
        self.assertGreater(j2.duration_ms, 0)

    def test_env_failure_is_reported(self):
        def bad():
            raise RuntimeError('no vcvarsall')
        runner = core.BuildRunner(bad, capture_output=True, use_job_object=False)
        runner.start()
        import time
        events = []
        t0 = time.time()
        while not events and time.time() - t0 < 10:
            events += runner.poll()
            time.sleep(0.01)
        runner.shutdown()
        self.assertEqual(events[0][0], 'env')
        self.assertFalse(events[0][1])


class VscodeTests(unittest.TestCase):
    def test_render(self):
        files = core.render_vscode_files('Fft', 'C:/TD/TouchDesigner.exe', 'C:/p/x.toe', 'C:/p', 'C:/PB/include')
        self.assertIn('launch.json', files)
        json.dumps(files['launch.json'])
        self.assertEqual(files['launch.json']['configurations'][0]['request'], 'attach')
        self.assertIn('C:/PB/include', files['c_cpp_properties.json']['configurations'][0]['includePath'])


class LoaderParPersistenceTests(unittest.TestCase):
    """Pure logic for which loader parameters survive a fresh-node reload, and snapshot/restore."""

    def test_is_persisted_par_classification(self):
        is_p = core.is_persisted_par
        # built-in value pars are persisted
        self.assertTrue(is_p('timeslice', False, 'Toggle'))
        self.assertTrue(is_p('outputresolution', False, 'Menu'))
        self.assertTrue(is_p('pixelformat', False, 'Menu'))
        # reload-control / structural pars are never persisted
        for ctrl in ('plugin', 'unloadplugin', 'reinitpulse', 'reinit', 'callbacks', 'language',
                     'pageindex', 'commonrenamefrom', 'commonrenameto', 'renamefrom', 'renameto'):
            self.assertFalse(is_p(ctrl, False, 'Str'), ctrl)
        # pulses never persisted
        self.assertFalse(is_p('refreshpulse', True, 'Pulse'))
        # custom pars (uppercase name) are persisted
        self.assertTrue(is_p('Gain', True, 'Float'))
        self.assertTrue(is_p('CustomPar', False, 'Float'))
        # empty / junk names are not
        self.assertFalse(is_p('', False, 'Str'))
        self.assertFalse(is_p(None, False, 'Str'))

    def test_snapshot_par_modes(self):
        const = core.snapshot_par('timeslice', 'Toggle', core.PAR_CONSTANT, True, None, None)
        self.assertEqual(const, ('timeslice', 'val', True))
        expr = core.snapshot_par('gain', 'Float', core.PAR_EXPRESSION, 0, 'me.parent().width', None)
        self.assertEqual(expr, ('gain', 'expr', 'me.parent().width'))
        bind = core.snapshot_par('gain', 'Float', core.PAR_BIND, 0, None, '../gain')
        self.assertEqual(bind, ('gain', 'bind', '../gain'))
        self.assertIsNone(core.snapshot_par('go', 'Pulse', core.PAR_CONSTANT, True, None, None))
        # menu value is captured as the entry name, not an index
        menu = core.snapshot_par('srselect', 'Menu', core.PAR_CONSTANT, 'bypass', None, None)
        self.assertEqual(menu, ('srselect', 'val', 'bypass'))

    def test_restore_action_decisions(self):
        act = core.restore_action
        self.assertEqual(act('gain', 'val', 0.5, ()), ('apply_value', 0.5))
        self.assertEqual(act('gain', 'expr', 'op()', ()), ('apply_expr', 'op()'))
        self.assertEqual(act('gain', 'bind', '../x', ()), ('apply_bind', '../x'))
        # menu whose entry still exists -> apply_value
        self.assertEqual(act('srselect', 'val', 'bypass', ('bypass', 'normalize')), ('apply_value', 'bypass'))
        # menu whose entry vanished -> skip with reason
        action, detail = act('srselect', 'val', 'gone', ('bypass',))
        self.assertEqual((action, detail), ('skip', 'entry no longer exists'))

    def test_normalize_par_mode_without_td(self):
        # With no par_mode binding, everything collapses to CONSTANT (safe fallback).
        self.assertEqual(core.normalize_par_mode(object(), None), core.PAR_CONSTANT)
        self.assertEqual(core.normalize_par_mode(None, None), core.PAR_CONSTANT)

    def test_parameter_definition_diff(self):
        before = {'A': ('on', 'off'), 'B': ('x',), 'Gone': ('old',)}
        after = {'A': ('on', 'off'), 'B': ('x', 'y'), 'New': ('n',)}
        added, removed, changed = core.parameter_definition_diff(before, after)
        self.assertEqual(added, ['New'])
        self.assertEqual(removed, ['Gone'])
        self.assertEqual(changed, ['B'])
        # unchanged menus produce no changed set
        a, r, c = core.parameter_definition_diff({'A': ('a',)}, {'A': ('a',)})
        self.assertEqual((a, r, c), ([], [], []))


class DedupeTests(unittest.TestCase):
    def test_collapses_only_immediate_duplicates(self):
        src = ['a', 'a', 'b', 'a', 'c', 'c', 'c', '']
        self.assertEqual(list(core.dedupe_consecutive(src)), ['a', 'b', 'a', 'c', ''])
        # empty input is fine
        self.assertEqual(list(core.dedupe_consecutive([])), [])


if __name__ == '__main__':
    unittest.main()
