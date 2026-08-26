"""
ci.py — headless integration check for PluginBuilder_V2 (no TouchDesigner needed).

    python dev/ci.py                 # unit tests + scaffold every template + configure/build BasicCHOP
    python dev/ci.py --all           # build every non-CUDA template
    python dev/ci.py --project <dir> # additionally configure+build an existing plugin project (e.g. Plugin_FFT/PluginProjects/FFT)
    python dev/ci.py --no-build      # scaffold only (no compiler required)

Uses PluginBuilderCore exactly like the TouchDesigner extension does: toolchain discovery via
settings.ini / vswhere, MSVC environment capture, BuildRunner jobs with exit codes and diagnostics.
Exit code is non-zero on any failure, so it can run in GitHub Actions (windows-latest) as-is.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'source'))

import PluginBuilderCore as core  # noqa: E402
import CMakeBlocks  # noqa: E402


def log(msg):
    print(f'[ci] {msg}', flush=True)


def load_settings():
    appdata = os.environ.get('APPDATA', '')
    ini = os.path.join(appdata, 'IntentDev', 'PluginBuilder', 'settings.ini')
    text = ''
    if os.path.exists(ini):
        with open(ini, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    s = core.parse_settings(text, os.environ.get('USERPROFILE', os.path.expanduser('~')))
    s['paths']['PluginBuilderDir'] = ROOT
    return s


def scaffold(template_name, meta, dest_root, settings):
    name = f'Ci{template_name}'
    project_dir = os.path.join(dest_root, 'PluginProjects', name)
    os.makedirs(os.path.join(project_dir, 'source'))
    text = CMakeBlocks.assemble(name, meta['family'], ROOT.replace('\\', '/'), features=meta.get('features', []))
    with open(os.path.join(project_dir, 'CMakeLists.txt'), 'w', encoding='utf-8') as f:
        f.write(text)
    core.write_plugin_manifest(project_dir, {'name': name, 'family': meta['family'], 'template': template_name})
    rep = core.template_replacements(name, core.sanitize_op_type(name), name, core.make_op_icon(name),
                                     settings['plugininfo']['Author'], settings['plugininfo']['Email'])
    src_dir = os.path.join(meta['dir'], 'source')
    for fn in os.listdir(src_dir):
        with open(os.path.join(src_dir, fn), 'r', encoding='utf-8', errors='replace') as f:
            t = f.read()
        t = core.render_source(t, meta.get('replace', template_name), name, rep)
        with open(os.path.join(project_dir, 'source', fn.replace(meta.get('replace', template_name), name)), 'w', encoding='utf-8') as f:
            f.write(t)
    return name, project_dir


def run_job(runner, job, timeout=900):
    done = {}
    job.on_done = lambda j: done.setdefault('job', j)
    runner.submit(job)
    t0 = time.time()
    while 'job' not in done and time.time() - t0 < timeout:
        for ev in runner.poll():
            if ev[0] == 'line':
                sys.stdout.write(ev[2])
            elif ev[0] == 'env' and not ev[1]:
                raise RuntimeError(f'MSVC environment failed: {ev[2]}')
        time.sleep(0.02)
    if 'job' not in done:
        runner.kill_current()
        raise RuntimeError(f'{job.label} timed out')
    return done['job']


def build_project(runner, project_dir, name, family):
    samples = os.environ.get('TD_SAMPLES_DIR', 'C:/Program Files/Derivative/TouchDesigner/Samples/CPlusPlus')
    cfg_args = ['cmake', '-B', 'build', '-G', 'Ninja', f'-DPLUGIN_BUILDER_DIR={ROOT}',
                f'-DPLUGIN_DIR={project_dir}/__deploy__', '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    if os.path.isdir(samples):
        cfg_args.append(f'-DTD_SAMPLES_DIR={samples}')
    j = run_job(runner, core.BuildJob('configure', cfg_args, project_dir, label=f'configure {name}'))
    if not j.ok:
        for d in core.parse_diagnostics(j.output):
            log(f"  {d['severity']}: {d['file']}({d['line']}): {d['message']}")
        raise RuntimeError(f'configure failed for {name} (exit {j.returncode})')
    j = run_job(runner, core.BuildJob('build', ['ninja', '-C', 'build'], project_dir, label=f'build {name}'))
    diags = core.parse_diagnostics(j.output)
    errors, warnings = core.summarize_diagnostics(diags)
    for d in diags:
        log(f"  {d['severity']}: {d['file']}({d['line']}): {d['code']} {d['message']}")
    if not j.ok:
        raise RuntimeError(f'build failed for {name} (exit {j.returncode}, {errors} errors)')
    dll = os.path.join(project_dir, 'build', 'bin', 'Release', f'{name}.dll')
    if not os.path.exists(dll):
        raise RuntimeError(f'{dll} not produced')
    log(f'OK {name}: {os.path.getsize(dll)} bytes in {j.duration_ms:.0f} ms ({warnings} warnings)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='build every non-CUDA template')
    ap.add_argument('--no-build', action='store_true')
    ap.add_argument('--project', action='append', default=[], help='existing plugin project dir to build')
    ap.add_argument('--keep', action='store_true', help='keep the temporary scaffold directory')
    args = ap.parse_args()

    log('unit tests')
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, 'tests'))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    if not res.wasSuccessful():
        return 1

    settings = load_settings()
    templates = core.load_templates(os.path.join(ROOT, 'templates'))
    log(f'{len(templates)} templates: {", ".join(templates)}')
    tmp = tempfile.mkdtemp(prefix='pluginbuilder_ci_')
    log(f'scaffolding into {tmp}')
    scaffolded = []
    for name, meta in templates.items():
        pname, pdir = scaffold(name, meta, tmp, settings)
        scaffolded.append((name, pname, pdir, meta))
        log(f'  scaffolded {pname} ({meta["family"]}, features={meta.get("features")})')

    rc = 0
    if not args.no_build:
        tc = core.discover_toolchain(settings['paths'], extra_search_dirs=[os.path.join(os.path.expanduser('~'), 'ninja')])
        if tc['errors']:
            for e in tc['errors']:
                log(f'ERROR: {e}')
            return 2
        log(f"toolchain: {tc['sources']}")
        runner = core.BuildRunner(lambda: core.capture_vc_env(tc['vcvarsall'], 'x64',
                                                              extra_path_dirs=[tc['ninja_dir'], tc['cmake_dir']],
                                                              extra_env={'PLUGINBUILDER_BUILD': '1', 'PLUGIN_BUILDER_DIR': ROOT}),
                                  capture_output=True)
        runner.start()
        try:
            targets = [s for s in scaffolded if ('cuda' not in s[3].get('features', []))]
            if not args.all:
                targets = [s for s in targets if s[0] == 'BasicCHOP']
            for tname, pname, pdir, meta in targets:
                try:
                    build_project(runner, pdir, pname, meta['family'])
                except Exception as e:  # noqa: BLE001
                    log(f'FAILED {pname}: {e}')
                    rc = 1
            for pdir in args.project:
                pdir = os.path.abspath(pdir)
                m = core.read_plugin_manifest(pdir) or {'name': os.path.basename(pdir), 'family': '?'}
                try:
                    build_project(runner, pdir, m['name'], m['family'])
                except Exception as e:  # noqa: BLE001
                    log(f'FAILED {pdir}: {e}')
                    rc = 1
        finally:
            runner.shutdown()
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    log('DONE' if rc == 0 else 'FAILED')
    return rc


if __name__ == '__main__':
    sys.exit(main())
