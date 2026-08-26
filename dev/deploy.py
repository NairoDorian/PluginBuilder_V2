"""
deploy.py — PluginBuilder release packaging (run from inside dev/dev.toe).

Resets the PluginBuilder COMP to a clean default state, verifies that the code DATs are
file-synced to ../source/*.py (so the .tox never ships stale embedded copies), strips
__pycache__ folders, and exports ../PluginBuilder.tox.
"""

import os
import shutil

print('[Deploy] Packaging PluginBuilder.tox release component...')

PluginBuilderComp = op('PluginBuilder')

# 1. Verify that the extension/support DATs are linked to the source files on disk.
expected = {'PluginBuilderExt': 'PluginBuilderExt.py', 'PluginBuilderCore': 'PluginBuilderCore.py', 'CMakeBlocks': 'CMakeBlocks.py'}
problems = []
for dat_name, file_name in expected.items():
    dat = PluginBuilderComp.op(dat_name) or PluginBuilderComp.op(f'builder/{dat_name}')
    if dat is None:
        problems.append(f'DAT {dat_name} not found in the COMP (create a Text DAT named {dat_name} linked to source/{file_name})')
        continue
    file_par = getattr(dat.par, 'file', None)
    if file_par is None or not str(file_par.eval()).replace('\\', '/').endswith(f'source/{file_name}'):
        problems.append(f'DAT {dat_name} is not linked to source/{file_name} (par.file = {file_par.eval() if file_par else None})')
    try:
        dat.par.syncfile = True
    except Exception:  # noqa: BLE001
        pass
for p in problems:
    print('[Deploy] WARNING:', p)

# 2. Reset creation parameters to defaults
PluginBuilderComp.EnableCreatePars()
PluginBuilderComp.par.Pluginname = ''
PluginBuilderComp.par.Plugintemplate.menuIndex = 0
PluginBuilderComp.par.Createinputop = False
PluginBuilderComp.par.Compileonupdate = True
PluginBuilderComp.par.Buildconfig = 'Release'
for name in ('Buildstatus', 'Lastbuild', 'Loadeddll'):
    if hasattr(PluginBuilderComp.par, name):
        setattr(PluginBuilderComp.par, name, '')
PluginBuilderComp.op('CMakeLists').text = ''
errors_dat = PluginBuilderComp.op('builder/build_errors')
if errors_dat is not None:
    errors_dat.clear()
PluginBuilderComp.unstore('PB_mirror_pages')

# 3. Strip bytecode caches next to the sources
root = os.path.abspath(os.path.join(project.folder, '..'))
for dirpath, dirnames, _files in os.walk(root):
    if '__pycache__' in dirnames:
        shutil.rmtree(os.path.join(dirpath, '__pycache__'), ignore_errors=True)

# 4. Export
PluginBuilderComp.save('../PluginBuilder.tox')
print('[Deploy] Successfully exported PluginBuilder.tox' + (' (with warnings)' if problems else ''))
