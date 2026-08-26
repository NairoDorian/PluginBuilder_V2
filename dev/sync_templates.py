"""
sync_templates.py — Regenerate PluginBuilder templates and SDK headers from an installed TouchDesigner.

Usage (any Python 3.8+, no TouchDesigner needed):
    python dev/sync_templates.py                       # auto-detect C:/Program Files/Derivative/TouchDesigner
    python dev/sync_templates.py --td "D:/TouchDesigner.2025.33070"
    python dev/sync_templates.py --headers-only
    python dev/sync_templates.py --dry-run

What it does:
  1. Copies the C++ SDK headers (CHOP/DAT/SOP/TOP/POP *_CPlusPlusBase.h, CPlusPlus_Common.h, GL_Extensions.h)
     from <TD>/Samples/CPlusPlus/* into PluginBuilder_V2/include/ (line endings normalized to LF).
  2. Rebuilds every template in templates/<Name>/source/ from the official sample sources, applying:
       - class / file rename   (e.g. CPlusPlusCHOPExample -> BasicCHOP)
       - metadata placeholders (#__OP_TYPE__#, #__OP_LABEL__#, #__OP_ICON__#, #__OP_AUTHOR__#,
                                #__OP_EMAIL__#, #__OP_HELPURL__#)
     and writes templates/<Name>/template.json describing the template (family, features, sources).

The PluginBuilder extension discovers templates through template.json, so adding a new template is a
matter of adding an entry to TEMPLATES below (or dropping a folder with a template.json into templates/).
"""

import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))

DEFAULT_TD = r'C:/Program Files/Derivative/TouchDesigner'

# template name -> definition
TEMPLATES = {
    'BasicCHOP': {
        'family': 'CHOP', 'sample_dir': 'CHOP', 'class': 'CPlusPlusCHOPExample',
        'features': [], 'description': 'Basic CHOP: timesliced generator/filter with custom parameters and dynamic menu.',
    },
    'CHOPWithPythonClass': {
        'family': 'CHOP', 'sample_dir': 'CHOPWithPythonClass', 'class': 'CHOPWithPythonClass',
        'features': ['python'], 'description': 'CHOP exposing a custom Python class/methods via the CPython C API.',
    },
    'BasicDAT': {
        'family': 'DAT', 'sample_dir': 'DAT', 'class': 'CPlusPlusDATExample',
        'features': [], 'description': 'Basic DAT: generates or filters table/text data.',
    },
    'CPUMemoryTOP': {
        'family': 'TOP', 'sample_dir': 'CPUMemoryTOP', 'class': 'CPUMemoryTOP',
        'features': [], 'description': 'TOP filled from CPU memory with a threaded frame queue.',
    },
    'CudaTOP': {
        'family': 'TOP', 'sample_dir': 'CudaTOP', 'class': 'CudaTOP',
        'features': ['cuda'], 'description': 'TOP processed by a CUDA kernel (requires CUDA Toolkit).',
    },
    'SimpleShapesSOP': {
        'family': 'SOP', 'sample_dir': 'SimpleShapesSOP', 'class': 'SimpleShapes',
        'features': [], 'description': 'SOP generating simple geometry with attributes.',
    },
    'SimpleShapesPOP': {
        'family': 'POP', 'sample_dir': 'SimpleShapesPOP', 'class': 'SimpleShapesPOP',
        'features': [], 'description': 'POP (point operator) generating simple shapes into GPU buffers.',
    },
    'CudaPOP': {
        'family': 'POP', 'sample_dir': 'CudaPOP', 'class': 'CudaPOP',
        'features': ['cuda'], 'description': 'POP processed by a CUDA kernel (requires CUDA Toolkit).',
    },
}

HEADER_FILES = {
    'CHOP/CHOP_CPlusPlusBase.h': 'CHOP_CPlusPlusBase.h',
    'CHOP/CPlusPlus_Common.h': 'CPlusPlus_Common.h',
    'DAT/DAT_CPlusPlusBase.h': 'DAT_CPlusPlusBase.h',
    'SimpleShapesSOP/SOP_CPlusPlusBase.h': 'SOP_CPlusPlusBase.h',
    'CudaTOP/TOP_CPlusPlusBase.h': 'TOP_CPlusPlusBase.h',
    'SimpleShapesPOP/POP_CPlusPlusBase.h': 'POP_CPlusPlusBase.h',
}

SKIP_SUFFIXES = ('_CPlusPlusBase.h', 'CPlusPlus_Common.h', 'GL_Extensions.h', '.sln', '.vcxproj', '.filters',
                 '.user', '.plist', '.xcodeproj')
SOURCE_EXT = ('.cpp', '.h', '.cu', '.cuh', '.hpp', '.c')

META_RULES = [
    (re.compile(r'(opType->setString\()"[^"]*"'), r'\1"#__OP_TYPE__#"'),
    (re.compile(r'(opLabel->setString\()"[^"]*"'), r'\1"#__OP_LABEL__#"'),
    (re.compile(r'(opIcon->setString\()"[^"]*"'), r'\1"#__OP_ICON__#"'),
    (re.compile(r'(authorName->setString\()"[^"]*"'), r'\1"#__OP_AUTHOR__#"'),
    (re.compile(r'(authorEmail->setString\()"[^"]*"'), r'\1"#__OP_EMAIL__#"'),
    (re.compile(r'(opHelpURL->setString\()"[^"]*"'), r'\1"#__OP_HELPURL__#"'),
]


def read_text(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8-sig', errors='replace').replace('\r\n', '\n').replace('\r', '\n')


def write_text(path, text, dry_run):
    if dry_run:
        print(f'  [dry-run] would write {os.path.relpath(path, ROOT)} ({len(text)} bytes)')
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def api_version_of(header_text):
    m = re.search(r'const int (\w+)CPlusPlusAPIVersion\s*=\s*(\d+)', header_text)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def sync_headers(td_samples, dry_run):
    include_dir = os.path.join(ROOT, 'include')
    print('Syncing SDK headers ->', os.path.relpath(include_dir, ROOT))
    versions = {}
    for src_rel, dst_name in HEADER_FILES.items():
        src = os.path.join(td_samples, src_rel)
        if not os.path.exists(src):
            print(f'  WARNING: {src} not found, skipped')
            continue
        text = read_text(src)
        fam, ver = api_version_of(text)
        if fam:
            versions[fam] = ver
        write_text(os.path.join(include_dir, dst_name), text, dry_run)
        print(f'  {dst_name}' + (f'  (API {ver})' if ver else ''))
    # GL_Extensions.h stub kept for older templates that include it
    gl = os.path.join(include_dir, 'GL_Extensions.h')
    if not os.path.exists(gl):
        write_text(gl, '#pragma once\n#include <gl/gl.h>\n', dry_run)
    common = read_text(os.path.join(td_samples, 'CHOP/CPlusPlus_Common.h'))
    m = re.search(r'const int OP_CommonAPIVersion\s*=\s*(\d+)', common)
    versions['Common'] = int(m.group(1)) if m else None
    write_text(os.path.join(include_dir, 'sdk_versions.json'),
               json.dumps({'source': td_samples, 'versions': versions}, indent=2) + '\n', dry_run)
    print('  sdk_versions.json:', versions)
    return versions


def templatize(text, class_name, template_name):
    text = text.replace(class_name, template_name)
    # uppercase guard variants (e.g. __SimpleShapes__ -> handled above; SIMPLESHAPES_EXPORTS style guards)
    text = text.replace(class_name.upper(), template_name.upper())
    for rx, repl in META_RULES:
        text = rx.sub(repl, text)
    # Ensure an opIcon placeholder exists right after opLabel when the sample omits it.
    if 'opIcon->setString(' not in text and 'opLabel->setString(' in text:
        text = re.sub(r'(^[ \t]*)(info->customOPInfo\.opLabel->setString\("#__OP_LABEL__#"\);)',
                      r'\1\2\n\n\1// Will be turned into a 3 letter icon on the nodes\n\1info->customOPInfo.opIcon->setString("#__OP_ICON__#");',
                      text, count=1, flags=re.M)
    # Ensure an opHelpURL placeholder exists (API 10) right after maxInputs when the sample omits it.
    if 'opHelpURL->setString(' not in text and 'maxInputs' in text:
        text = re.sub(r'(^[ \t]*)(info->customOPInfo\.maxInputs\s*=\s*\d+;)',
                      r'\1\2\n\n\1// Custom website URL that the Operator Help can point to\n\1info->customOPInfo.opHelpURL->setString("#__OP_HELPURL__#");',
                      text, count=1, flags=re.M)
    return text


def sync_template(name, spec, td_samples, versions, dry_run):
    sample_dir = os.path.join(td_samples, spec['sample_dir'])
    if not os.path.isdir(sample_dir):
        print(f'  WARNING: sample dir {sample_dir} missing, template {name} skipped')
        return None
    out_dir = os.path.join(ROOT, 'templates', name, 'source')
    if not dry_run and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    sources = []
    for fn in sorted(os.listdir(sample_dir)):
        if fn.endswith(SKIP_SUFFIXES) or not fn.endswith(SOURCE_EXT):
            continue
        text = templatize(read_text(os.path.join(sample_dir, fn)), spec['class'], name)
        out_name = fn.replace(spec['class'], name)
        write_text(os.path.join(out_dir, out_name), text, dry_run)
        sources.append(out_name)
    meta = {
        'name': name,
        'family': spec['family'],
        'replace': name,
        'main_source': f'{name}.cpp',
        'features': spec['features'],
        'description': spec['description'],
        'api_version': versions.get(spec['family']),
        'sample_origin': spec['sample_dir'],
        'sources': sources,
    }
    write_text(os.path.join(ROOT, 'templates', name, 'template.json'), json.dumps(meta, indent=2) + '\n', dry_run)
    print(f'  {name:<22} family={spec["family"]:<4} files={sources}')
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--td', default=DEFAULT_TD, help='TouchDesigner install folder')
    ap.add_argument('--headers-only', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    td_samples = os.path.join(args.td, 'Samples', 'CPlusPlus')
    if not os.path.isdir(td_samples):
        print(f'ERROR: {td_samples} not found. Pass --td <TouchDesigner install folder>.')
        return 2
    print('TouchDesigner samples:', td_samples)
    versions = sync_headers(td_samples, args.dry_run)
    if args.headers_only:
        return 0
    print('Syncing templates ->', os.path.relpath(os.path.join(ROOT, 'templates'), ROOT))
    for name, spec in TEMPLATES.items():
        sync_template(name, spec, td_samples, versions, args.dry_run)
    print('Done.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
