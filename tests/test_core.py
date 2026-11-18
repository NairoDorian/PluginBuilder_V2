"""
Headless unit tests for PluginBuilderCore (no TouchDesigner required).

    cd PluginBuilder_V2
    python -m unittest discover -s tests -v

Contract: every test must pass (the count is reported by unittest; it is not maintained by hand
in the docs any more). Groups: Naming/Settings/Template/CMake/Manifest/Diagnostics/File/Sdk/
Runner/Vscode/LoaderParPersistence/Dedupe/Deploy/TdInstall/CallableJob. They pin the
Plugin_FFT-facing behaviour — API >= 10 headers, plugin.json round-trip, rename-in-place
safe_replace_file, BuildRunner ordering/exit codes, and the pure reload-persistence
helpers used by PluginBuilderExt._reload_loader_fresh (v2.1.1 parameter-definition refresh).
No TouchDesigner, no MSVC required; RunnerTests spawn only sys.executable children.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
# Put source/ on sys.path so PluginBuilderCore and CMakeBlocks import without installation.
sys.path.insert(0, os.path.join(ROOT, 'source'))

import PluginBuilderCore as core  # noqa: E402
import CMakeBlocks  # noqa: E402
import ParamGen  # noqa: E402

# sys.path.insert above lets `import PluginBuilderCore` resolve source/ without packaging.


class NamingTests(unittest.TestCase):
    """validate_plugin_name / sanitize_op_type / make_op_icon / td_project_name contracts."""
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
    """parse_settings: defaults, path expansion, [DevMode], never-raises on garbage."""
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

    def test_toolchain_discovery_runs_vswhere_only_when_needed(self):
        """2.2.1: settings.ini naming vcvarsall/ninja/cmake costs no vswhere (two processes, ~250 ms on TD's
        main thread at every extension init); a missing vcvarsall runs it once, however many tools need it."""
        d = tempfile.mkdtemp()
        try:
            for fn in ('vcvarsall.bat', 'ninja.exe', 'cmake.exe'):
                open(os.path.join(d, fn), 'w').close()
            calls = []
            saved = core.find_visual_studio
            core.find_visual_studio = lambda: (calls.append(1), (None, None))[1]
            try:
                info = core.discover_toolchain({'VCVarsall': os.path.join(d, 'vcvarsall.bat'), 'NinjaDir': d, 'CMakeDir': d})
                self.assertEqual(calls, [])
                self.assertEqual(info['sources'], {'vcvarsall': 'settings.ini', 'ninja': 'settings.ini', 'cmake': 'settings.ini'})
                self.assertEqual(info['errors'], [])
                info = core.discover_toolchain({'VCVarsall': '', 'NinjaDir': '', 'CMakeDir': ''}, extra_search_dirs=[d])
                self.assertLessEqual(len(calls), 1)
                self.assertTrue(any('vcvarsall' in e for e in info['errors']))
            finally:
                core.find_visual_studio = saved
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_dev_mode(self):
        s = core.parse_settings('[DevMode]\nx=1\n', 'h')
        self.assertTrue(s['dev_mode'])


class TemplateTests(unittest.TestCase):
    """load_templates / render_source / #__OP_*__# placeholder completeness (API 10)."""
    def test_load_templates_from_repo(self):
        """Repo templates/ must contain BasicCHOP (CHOP) and every template a valid family + sources."""
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
        """Every template main .cpp must carry all #__OP_*__# tokens and setAPIVersion( (API 10)."""
        templates = core.load_templates(os.path.join(ROOT, 'templates'))
        for name, meta in templates.items():
            main = os.path.join(meta['dir'], 'source', f"{meta.get('replace', name)}.cpp")
            with open(main, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            # The token must sit in the real setString("...") call, not merely somewhere in the file: a
            # token inside a comment would satisfy a substring check while the code lost it.
            for field, ph in (('opType', '#__OP_TYPE__#'), ('opLabel', '#__OP_LABEL__#'), ('opIcon', '#__OP_ICON__#'),
                              ('authorName', '#__OP_AUTHOR__#'), ('authorEmail', '#__OP_EMAIL__#')):
                self.assertRegex(text, r'%s->setString\(\s*"%s"\s*\)' % (field, ph), f'{name}: {field} placeholder')
            # ...and no placeholder may appear anywhere else (render_source would rewrite comment prose).
            for ln in text.splitlines():
                if '#__' in ln:
                    self.assertIn('setString(', ln, f'{name}: placeholder outside a setString call: {ln.strip()}')
            self.assertIn('setAPIVersion(', text, f'{name}: not an API-10 style template')


class CMakeTests(unittest.TestCase):
    """CMakeBlocks.assemble output shape + presence of cmake/TDPlugin.cmake & TDDeploy.cmake."""
    def test_assemble(self):
        """HEADER first line + project() + td_add_plugin + include(TDPlugin.cmake) + PLUGIN_BUILDER_DIR."""
        text = CMakeBlocks.assemble('Fft', 'CHOP', 'C:/PB', features=['python'], extra_lines=['# hi'])
        self.assertTrue(text.startswith("# {'plugin_type': 'CHOP'}"))
        self.assertIn('project(Fft LANGUAGES CXX)', text)
        self.assertIn('td_add_plugin(Fft FAMILY CHOP FEATURES python)', text)
        self.assertIn('include(${PLUGIN_BUILDER_DIR}/cmake/TDPlugin.cmake)', text)
        self.assertIn('set(PLUGIN_BUILDER_DIR "C:/PB")', text)
        self.assertTrue(text.rstrip().endswith('# hi'))

    def test_assemble_no_features(self):
        """Empty features must omit the FEATURES keyword (no trailing space before ')')."""
        text = CMakeBlocks.assemble('X', 'POP', 'C:/PB')
        self.assertIn('td_add_plugin(X FAMILY POP)', text)

    def test_module_exists(self):
        """Shared CMake modules must ship with the repo (CMakeBlocks includes them by path)."""
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'cmake', 'TDPlugin.cmake')))
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'cmake', 'TDDeploy.cmake')))


class ManifestTests(unittest.TestCase):
    """plugin.json round-trip + safe parsing of the legacy CMakeLists first-line header."""
    def test_header_parse(self):
        """plugin_type_from_cmake_header: valid family, unknown family, non-header, non-literal."""
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
    """parse_diagnostics / summarize_diagnostics over MSVC, LNK, CMake and ninja output."""
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
    """safe_replace_file rename-in-place hot swap + cleanup_old_files (.old removal)."""
    def test_safe_replace_file(self):
        """Rename-in-place: new -> unchanged -> replaced(.old) -> cleanup_old_files."""
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
    """include/ SDK headers: CHOP API >= 10, Common >= 3 (TD 2025.33230+), POP present; mismatch message."""
    def test_read_sdk_versions(self):
        """include/ headers: family API >= 10, Common >= 3 with its BitMaskFlags.h, POP present."""
        inc = os.path.join(ROOT, 'include')
        v = core.read_sdk_versions(inc)
        self.assertGreaterEqual(v.get('CHOP', 0), 10)
        self.assertGreaterEqual(v.get('Common') or 0, 3)
        self.assertIn('POP', v)
        # CPlusPlus_Common.h (Common 3) #includes BitMaskFlags.h; a sync that forgot it would not compile.
        self.assertTrue(os.path.exists(os.path.join(inc, 'BitMaskFlags.h')))
        with open(os.path.join(inc, 'sdk_versions.json'), 'r', encoding='utf-8') as f:
            rec = json.load(f)
        self.assertIn('td_build', rec)
        self.assertEqual(rec['versions'].get('Common'), v.get('Common'))

    def test_mismatch_message(self):
        """sdk_mismatch_message is empty on agreement, names family + versions otherwise."""
        self.assertEqual(core.sdk_mismatch_message({'CHOP': 10}, {'CHOP': 10}), '')
        self.assertIn('CHOP: 9 vs installed 10', core.sdk_mismatch_message({'CHOP': 9}, {'CHOP': 10}))


class RunnerTests(unittest.TestCase):
    """BuildRunner: FIFO job order, exit-code propagation, env-failure event."""
    def test_runner_runs_jobs_in_order_and_reports_exit_codes(self):
        """FIFO: first submitted job finishes first; returncode and captured stdout/stderr preserved."""
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

    @unittest.skipUnless(sys.platform == 'win32', 'job objects are Windows-only')
    def test_kill_current_kills_the_whole_process_tree(self):
        """2.2.1: Cancel must stop the grandchildren too (cmd -> ninja -> cl.exe), not only the direct child."""
        import ctypes
        import time
        env = dict(os.environ)
        runner = core.BuildRunner(lambda: env, capture_output=True, use_job_object=True)
        runner.start()
        py = sys.executable
        script = ('import subprocess, sys, time; '
                  'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                  'print("GRANDCHILD", p.pid, flush=True); time.sleep(60)')
        done = []
        job = core.BuildJob('custom', [py, '-c', script], os.getcwd(), on_done=lambda j: done.append(j))
        runner.submit(job)
        pid, t0 = None, time.time()
        while pid is None and time.time() - t0 < 30:
            for ev in runner.poll():
                if ev[0] == 'line' and ev[2].startswith('GRANDCHILD'):
                    pid = int(ev[2].split()[1])
            time.sleep(0.02)
        self.assertIsNotNone(pid, 'the child never reported its grandchild')
        self.assertTrue(runner.kill_current())
        t0 = time.time()
        while not done and time.time() - t0 < 30:
            runner.poll()
            time.sleep(0.02)
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE
        gone = True
        if h:
            gone = k32.WaitForSingleObject(h, 5000) == 0       # WAIT_OBJECT_0: it exited
            k32.CloseHandle(h)
        runner.shutdown()
        self.assertTrue(done, 'the killed job never reported done')
        self.assertTrue(gone, 'the grandchild process survived Cancel')

    def test_cancel_pending_also_cancels_a_job_already_taken_from_the_queue(self):
        """2.2.2: a job the worker has dequeued but not started yet must not escape Cancel."""
        runner = core.BuildRunner(lambda: dict(os.environ), capture_output=True, use_job_object=False)  # not started
        j1 = runner.submit(core.BuildJob('build', ['x'], os.getcwd()))
        j2 = runner.submit(core.BuildJob('build', ['y'], os.getcwd()))
        self.assertIs(runner._jobs.get_nowait(), j1)          # what the worker's _jobs.get() does
        self.assertEqual(runner.cancel_pending(), 2)
        self.assertTrue(j1.cancelled and j2.cancelled)
        self.assertEqual(sorted(ev[1].id for ev in runner.poll() if ev[0] == 'done'), sorted([j1.id, j2.id]))
        self.assertEqual(runner.pending_kinds(), [])
        runner._jobs.put(None)                                 # a shutdown sentinel survives a cancel
        runner.cancel_pending()
        self.assertIsNone(runner._jobs.get_nowait())

    def test_env_failure_is_reported(self):
        """env_factory raising (e.g. missing vcvarsall) emits ('env', False, msg) as the first event."""
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


class DeployTests(unittest.TestCase):
    """files_identical fast paths, .old slot allocation, runtime-DLL manifest, output coalescing."""
    def test_files_identical_fast_paths(self):
        d = tempfile.mkdtemp()
        try:
            a, b = os.path.join(d, 'a'), os.path.join(d, 'b')
            with open(a, 'wb') as f:
                f.write(b'abc')
            shutil.copy2(a, b)                                   # same size, same mtime -> no hash needed
            self.assertTrue(core.files_identical(a, b))
            with open(b, 'wb') as f:
                f.write(b'abcd')                                 # different size -> different
            self.assertFalse(core.files_identical(a, b))
            with open(b, 'wb') as f:
                f.write(b'abd')                                  # same size, new mtime -> hashed -> different
            os.utime(b, ns=(os.stat(a).st_atime_ns, os.stat(a).st_mtime_ns + 10_000_000))
            self.assertFalse(core.files_identical(a, b))
            with open(b, 'wb') as f:
                f.write(b'abc')                                  # same content, different mtime -> hashed -> same
            os.utime(b, ns=(os.stat(a).st_atime_ns, os.stat(a).st_mtime_ns + 10_000_000))
            self.assertTrue(core.files_identical(a, b))
            self.assertFalse(core.files_identical(a, os.path.join(d, 'missing')))
        finally:
            shutil.rmtree(d)

    def test_old_slot_and_keep_old_false(self):
        d = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(d, 's.dll'), os.path.join(d, 'p.dll')
            with open(src, 'wb') as f:
                f.write(b'1')
            core.safe_replace_file(src, dst)
            with open(src, 'wb') as f:
                f.write(b'22')
            changed, note = core.safe_replace_file(src, dst, keep_old=False)
            self.assertTrue(changed)
            self.assertFalse(os.path.exists(dst + '.old'))       # removed because keep_old=False
            self.assertEqual(core._free_old_slot(dst), dst + '.old')
        finally:
            shutil.rmtree(d)

    def test_runtime_dll_manifest(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(core.read_runtime_dll_manifest(d, 'FFT'))
            with open(os.path.join(d, 'FFT_runtime_dlls.txt'), 'w') as f:
                f.write('libfftw3f-3.3.11-avx2.dll\n\nLIBFFTW3F-3.3.11-AVX2.dll\nother.dll\n')
            self.assertEqual(core.read_runtime_dll_manifest(d, 'FFT'), ['libfftw3f-3.3.11-avx2.dll', 'other.dll'])
        finally:
            shutil.rmtree(d)

    def test_coalesce_output(self):
        text, dropped = core.coalesce_output(['a', 'b\n'], 10)
        self.assertEqual((text, dropped), ('a\nb\n', 0))
        text, dropped = core.coalesce_output([f'l{i}' for i in range(250)], 200)
        self.assertEqual(dropped, 50)
        self.assertEqual(text.count('\n'), 201)
        self.assertIn('50 more line(s)', text)


class TdInstallTests(unittest.TestCase):
    """find_td_installs: versioned folders sorted newest first, unversioned last, samples required."""
    def test_find_td_installs(self):
        d = tempfile.mkdtemp()
        try:
            for name, samples in (('TouchDesigner.2025.33230', True), ('TouchDesigner.2025.9000', True),
                                  ('TouchDesigner', True), ('TouchDesigner.2026.100', False), ('Other', True)):
                os.makedirs(os.path.join(d, name, 'Samples', 'CPlusPlus') if samples else os.path.join(d, name))
            found = core.find_td_installs([d])
            self.assertEqual([os.path.basename(p) for _b, p in found],
                             ['TouchDesigner.2025.33230', 'TouchDesigner.2025.9000', 'TouchDesigner'])
            self.assertEqual(core.td_build_label(found[0][0]), '2025.33230')
            self.assertEqual(core.td_build_label(found[-1][0]), 'unversioned')
        finally:
            shutil.rmtree(d)


class CallableJobTests(unittest.TestCase):
    """BuildRunner 'call' jobs run in-process on the runner thread; errors become returncode -1."""
    def test_call_jobs_and_pending_kinds(self):
        import threading
        import time
        runner = core.BuildRunner(lambda: dict(os.environ), capture_output=True, use_job_object=False)
        gate = threading.Event()
        seen = []

        def slow(job, emit):
            gate.wait(5)
            emit('working')
            job.result = threading.current_thread().name
            return 0

        def boom(job, emit):
            raise PermissionError('locked')

        j1 = core.BuildJob('call', [], os.getcwd(), func=slow, on_done=seen.append)
        j3 = core.BuildJob('call', [], os.getcwd(), func=boom, on_done=seen.append)
        runner.submit(j1)
        runner.submit(j3)
        self.assertEqual(runner.pending_kinds(), ['call', 'call'])
        runner.start()
        t0 = time.time()
        while runner.current_job is None and time.time() - t0 < 10:
            time.sleep(0.01)
        self.assertEqual(runner.pending_kinds(), ['call'])        # j1 started, j3 still queued
        gate.set()
        while len(seen) < 2 and time.time() - t0 < 20:
            runner.poll()
            time.sleep(0.01)
        runner.shutdown()
        self.assertEqual(seen[0], j1)
        self.assertEqual(j1.returncode, 0)
        self.assertEqual(j1.result, 'PluginBuilder-BuildRunner')   # ran on the runner thread
        self.assertIn('working\n', j1.output)
        self.assertEqual(j3.returncode, -1)
        self.assertIsInstance(j3.error, PermissionError)


class ParamGenTests(unittest.TestCase):
    """parameters.json -> Parameters.h/.cpp (source/ParamGen.py) and template project scaffolding."""
    SPEC = {'pages': [{'name': 'Main', 'pars': [
        {'name': 'Mode', 'type': 'menu', 'enum': 'Mode', 'items': [['Rms', 'RMS'], ['Peak', 'Peak']], 'default': 'Peak'},
        {'name': 'Gain', 'type': 'float', 'default': 1.5, 'min': 0, 'max': 4, 'clamp': [0, 10]},
        {'name': 'Count', 'type': 'int', 'default': 3, 'field': 'count'},
        {'name': 'Smooth', 'type': 'toggle', 'default': True},
        {'name': 'Attackms', 'type': 'float', 'default': 10, 'field': 'attackMs', 'enable_if': 'v.smooth'},
        {'name': 'Reset', 'type': 'pulse'}]}]}

    def test_generate(self):
        h, c = ParamGen.generate(self.SPEC)
        self.assertIn('enum class Mode : int { Rms = 0, Peak, COUNT };', h)
        self.assertIn('constexpr char GainName[] = "Gain";', h)
        self.assertIn('double gain = 1.5;', h)
        self.assertIn('int count = 3;', h)
        self.assertIn('bool smooth = true;', h)
        self.assertIn('Mode mode = Mode::Peak;', h)
        self.assertNotIn('reset', h.split('struct Values')[1].split('};')[0])        # pulses carry no value
        self.assertIn('static_assert(sizeof(kModeNames)', c)
        self.assertIn('v.gain = std::clamp(inputs->getParDouble(GainName), 0.0, 10.0);', c)
        self.assertIn('state->setEnableState(AttackmsName, static_cast<bool>(v.smooth));', c)
        self.assertIn('manager->appendPulse(np);', c)
        # menus fall back to the default on an out-of-range stored index
        self.assertIn('static_cast<int>(Mode::COUNT)) ? Values{}.mode', c)

    def test_validation(self):
        bad = [{'pages': []},
               {'pages': [{'name': 'P', 'pars': [{'name': 'gain', 'type': 'float'}]}]},          # not Capitalised
               {'pages': [{'name': 'P', 'pars': [{'name': 'Gain', 'type': 'string'}]}]},         # unsupported type
               {'pages': [{'name': 'P', 'pars': [{'name': 'Gain', 'type': 'float'}, {'name': 'Gain', 'type': 'int'}]}]},
               {'pages': [{'name': 'P', 'pars': [{'name': 'Mode', 'type': 'menu', 'items': [['A', 'a']], 'default': 'B'}]}]}]
        for spec in bad:
            with self.assertRaises(ParamGen.SpecError):
                ParamGen.generate(spec)

    def test_generate_files_skips_unchanged(self):
        d = tempfile.mkdtemp()
        try:
            spec = os.path.join(d, 'parameters.json')
            with open(spec, 'w') as f:
                json.dump(self.SPEC, f)
            h, c = ParamGen.generate_files(spec, d)
            m1 = os.stat(h).st_mtime_ns
            os.utime(h, ns=(m1 - 10_000_000_000, m1 - 10_000_000_000))
            ParamGen.generate_files(spec, d)
            self.assertEqual(os.stat(h).st_mtime_ns, m1 - 10_000_000_000)   # unchanged: not rewritten
        finally:
            shutil.rmtree(d)

    def test_realtime_template_scaffold(self):
        """RealtimeCHOP ships project files + parameters.json; scaffolding generates Parameters.h/.cpp."""
        templates = core.load_templates(os.path.join(ROOT, 'templates'))
        meta = templates['RealtimeCHOP']
        self.assertTrue(meta.get('generate_parameters'))
        d = tempfile.mkdtemp()
        try:
            rep = core.template_replacements('Meter', 'Meter', 'Meter', 'MET', 'A', 'e')
            files = core.scaffold_template_files(meta, d, 'Meter', rep)
            for rel in ('source/Meter.cpp', 'source/Meter.h', 'source/MeterCore.h', 'parameters.json',
                        'tests/core_tests.cpp', 'bench/core_bench.cpp', 'source/Parameters.h', 'source/Parameters.cpp'):
                self.assertIn(rel, files)
                self.assertTrue(os.path.exists(os.path.join(d, rel)), rel)
            with open(os.path.join(d, 'source', 'Meter.cpp'), encoding='utf-8') as f:
                text = f.read()
            with open(os.path.join(d, 'source', 'Meter.h'), encoding='utf-8') as f:
                self.assertIn('#include "MeterCore.h"', f.read())
            self.assertIn('opType->setString("Meter")', text)
            lines = core.template_cmake_lines(meta, 'Meter')
            self.assertIn('td_plugin_optimize(Meter REALTIME)', lines)
            self.assertIn('td_plugin_add_test(Meter Meter_tests SOURCES tests/core_tests.cpp)', lines)
        finally:
            shutil.rmtree(d)

    def test_cuda_projects_enable_cuda_at_directory_scope(self):
        self.assertIn('project(X LANGUAGES CXX CUDA)', CMakeBlocks.assemble('X', 'TOP', '/pb', features=['cuda']))
        self.assertIn('project(X LANGUAGES CXX)', CMakeBlocks.assemble('X', 'TOP', '/pb'))


class VscodeTests(unittest.TestCase):
    """render_vscode_files produces serialisable launch/IntelliSense configs."""
    def test_render(self):
        files = core.render_vscode_files('Fft', 'C:/TD/TouchDesigner.exe', 'C:/p/x.toe', 'C:/p', 'C:/PB/include')
        self.assertIn('launch.json', files)
        json.dumps(files['launch.json'])
        self.assertEqual(files['launch.json']['configurations'][0]['request'], 'attach')
        self.assertIn('C:/PB/include', files['c_cpp_properties.json']['configurations'][0]['includePath'])


class LoaderParPersistenceTests(unittest.TestCase):
    """Pure logic for which loader parameters survive a fresh-node reload, and snapshot/restore.

    Feeds PluginBuilderExt._snapshot_loader_values / _restore_loader_values (v2.1.1
    hot-reload parameter-definition refresh). Classification must ignore ParMode entirely.
    """

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
        # headers never persisted (visual labels, no value)
        self.assertFalse(is_p('section_label', True, 'Header'))
        self.assertFalse(is_p('section_label', True, 'Header2'))
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
        # headers return None (visual labels, no value)
        self.assertIsNone(core.snapshot_par('header1', 'Header', core.PAR_CONSTANT, True, None, None))
        self.assertIsNone(core.snapshot_par('header2', 'Header2', core.PAR_CONSTANT, True, None, None))
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
    """dedupe_consecutive collapses only immediate repeats (MSVC warning blocks)."""
    def test_collapses_only_immediate_duplicates(self):
        src = ['a', 'a', 'b', 'a', 'c', 'c', 'c', '']
        self.assertEqual(list(core.dedupe_consecutive(src)), ['a', 'b', 'a', 'c', ''])
        # empty input is fine
        self.assertEqual(list(core.dedupe_consecutive([])), [])


if __name__ == '__main__':
    # Direct run: python tests/test_core.py -v  (same 29 tests as unittest discover)
    unittest.main()
