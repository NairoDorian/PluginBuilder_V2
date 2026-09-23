"""
sync_templates.py — Resync PluginBuilder's SDK headers (and optionally its templates) from an installed TouchDesigner.

Usage (any Python 3.8+, no TouchDesigner needed):
    python dev/sync_templates.py                        # newest installed TD: sync include/, report template drift
    python dev/sync_templates.py --td "D:/TouchDesigner.2025.33230"
    python dev/sync_templates.py --dry-run
    python dev/sync_templates.py --overwrite-templates  # regenerate templates/<Name>/source from the samples

What it does:
  1. Finds the TouchDesigner install to sync from. TouchDesigner 2025+ installs into versioned folders
     (C:/Program Files/Derivative/TouchDesigner.2025.33230); the newest one that has Samples/CPlusPlus
     wins (PluginBuilderCore.find_td_installs). --td overrides.
  2. Copies the C++ SDK headers (every *_CPlusPlusBase.h, CPlusPlus_Common.h and the headers they
     include, e.g. BitMaskFlags.h since Common API 3) into include/ (LF line endings) and records
     {td_build, source, versions} in include/sdk_versions.json.
  3. Templates. The templates in templates/<Name>/source are OWNED by this repository: they start as
     the official samples but carry fixes for real bugs in those samples (see CHANGELOG 2.2.0), so a
     resync must not silently replace them. By default this script only REPORTS drift: for each
     template it compares the comment-stripped code of the template with the (renamed, templatized)
     official sample and prints how many code lines differ. --overwrite-templates regenerates them
     from the samples instead (and the fixes then have to be re-applied by hand from git).

PluginBuilderExt._check_sdk_versions warns at init when include/ and the running TD disagree.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'source'))

import PluginBuilderCore as core  # noqa: E402

# template name -> definition
# family must be in PluginBuilderCore.FAMILIES; sample_dir is relative to
# <TD>/Samples/CPlusPlus; class is the sample's C++ class to rename to `name`.
# Templates that are not generated from a sample (e.g. RealtimeCHOP) are simply not listed here.
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

# Sample-relative header paths -> include/<basename> (one canonical SDK header per family).
HEADER_FILES = {
    'CHOP/CHOP_CPlusPlusBase.h': 'CHOP_CPlusPlusBase.h',
    'CHOP/CPlusPlus_Common.h': 'CPlusPlus_Common.h',
    'DAT/DAT_CPlusPlusBase.h': 'DAT_CPlusPlusBase.h',
    'SimpleShapesSOP/SOP_CPlusPlusBase.h': 'SOP_CPlusPlusBase.h',
    'CudaTOP/TOP_CPlusPlusBase.h': 'TOP_CPlusPlusBase.h',
    'SimpleShapesPOP/POP_CPlusPlusBase.h': 'POP_CPlusPlusBase.h',
}
# Headers pulled in by the SDK headers themselves (Common API 3 added BitMaskFlags.h). Any local
# #include "X.h" found in a synced header is also copied when it exists in the sample folder.
EXTRA_HEADER_DIRS = ('CHOP', 'DAT', 'SimpleShapesSOP', 'CudaTOP', 'SimpleShapesPOP')

SKIP_SUFFIXES = ('_CPlusPlusBase.h', 'CPlusPlus_Common.h', 'GL_Extensions.h', 'BitMaskFlags.h', '.sln',
                 '.vcxproj', '.filters', '.user', '.plist', '.xcodeproj')
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
    """Read as UTF-8 (BOM-tolerant) and normalize all newlines to LF."""
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


def strip_code(text):
    """Comment-stripped, whitespace-normalized code lines (string-literal aware) for drift reports."""
    out, i, n = [], 0, len(text)
    buf = []
    while i < n:
        c = text[i]
        if c in '"\'':
            q = c
            buf.append(c)
            i += 1
            while i < n and text[i] != q:
                if text[i] == '\\' and i + 1 < n:
                    buf.append(text[i:i + 2])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            buf.append(q)
            i += 1
        elif text.startswith('//', i):
            while i < n and text[i] != '\n':
                i += 1
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            buf.append(c)
            i += 1
    for line in ''.join(buf).split('\n'):
        line = ' '.join(line.split())
        if line:
            out.append(line)
    return out


def sync_headers(td_samples, td_build, dry_run):
    include_dir = os.path.join(ROOT, 'include')
    print('Syncing SDK headers ->', os.path.relpath(include_dir, ROOT))
    versions = {}
    copied = set()
    canonical = set(HEADER_FILES.values())   # synced by their own entry, never via an #include
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
        copied.add(dst_name)
        print(f'  {dst_name}' + (f'  (API {ver})' if ver else ''))
        # headers included by this header that live next to it in the sample folder
        for inc in re.findall(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.M):
            if inc in copied or inc in canonical:
                continue
            for d in (os.path.dirname(src_rel),) + EXTRA_HEADER_DIRS:
                cand = os.path.join(td_samples, d, inc)
                if os.path.exists(cand):
                    write_text(os.path.join(include_dir, inc), read_text(cand), dry_run)
                    copied.add(inc)
                    print(f'  {inc}  (included by {dst_name})')
                    break
    gl = os.path.join(include_dir, 'GL_Extensions.h')
    if not os.path.exists(gl):
        write_text(gl, '#pragma once\n#include <gl/gl.h>\n', dry_run)
    common = read_text(os.path.join(td_samples, 'CHOP/CPlusPlus_Common.h'))
    m = re.search(r'const int OP_CommonAPIVersion\s*=\s*(\d+)', common)
    versions['Common'] = int(m.group(1)) if m else None
    record = {'td_build': td_build, 'source': td_samples.replace('\\', '/'), 'versions': versions}
    write_text(os.path.join(include_dir, 'sdk_versions.json'), json.dumps(record, indent=2) + '\n', dry_run)
    print('  sdk_versions.json:', record)
    return versions


def templatize(text, class_name, template_name):
    """Rename the sample class (and UPPER guards) to the template name, then apply META_RULES."""
    text = text.replace(class_name, template_name)
    text = text.replace(class_name.upper(), template_name.upper())
    for rx, repl in META_RULES:
        text = rx.sub(repl, text)
    if 'opIcon->setString(' not in text and 'opLabel->setString(' in text:
        text = re.sub(r'(^[ \t]*)(info->customOPInfo\.opLabel->setString\("#__OP_LABEL__#"\);)',
                      r'\1\2\n\n\1// Will be turned into a 3 letter icon on the nodes\n\1info->customOPInfo.opIcon->setString("#__OP_ICON__#");',
                      text, count=1, flags=re.M)
    if 'opHelpURL->setString(' not in text and 'maxInputs' in text:
        text = re.sub(r'(^[ \t]*)(info->customOPInfo\.maxInputs\s*=\s*\d+;)',
                      r'\1\2\n\n\1// Custom website URL that the Operator Help can point to\n\1info->customOPInfo.opHelpURL->setString("#__OP_HELPURL__#");',
                      text, count=1, flags=re.M)
    return text


def sample_sources(name, spec, td_samples):
    sample_dir = os.path.join(td_samples, spec['sample_dir'])
    if not os.path.isdir(sample_dir):
        return None
    out = {}
    for fn in sorted(os.listdir(sample_dir)):
        if fn.endswith(SKIP_SUFFIXES) or not fn.endswith(SOURCE_EXT):
            continue
        out[fn.replace(spec['class'], name)] = templatize(read_text(os.path.join(sample_dir, fn)), spec['class'], name)
    return out


def report_drift(name, spec, td_samples):
    srcs = sample_sources(name, spec, td_samples)
    if srcs is None:
        print(f'  {name:<22} sample dir missing')
        return
    tdir = os.path.join(ROOT, 'templates', name, 'source')
    parts = []
    for fn, text in srcs.items():
        path = os.path.join(tdir, fn)
        if not os.path.exists(path):
            parts.append(f'{fn}: missing in template')
            continue
        diff = [l for l in difflib.unified_diff(strip_code(text), strip_code(read_text(path)), lineterm='', n=0)
                if l[:1] in '+-' and not l.startswith(('+++', '---'))]
        if diff:
            parts.append(f'{fn}: {len(diff)} code line(s) differ')
    print(f'  {name:<22} ' + ('identical to the sample (code)' if not parts else '; '.join(parts)))


def overwrite_template(name, spec, td_samples, versions, dry_run):
    srcs = sample_sources(name, spec, td_samples)
    if srcs is None:
        print(f'  WARNING: sample dir for {name} missing, skipped')
        return
    out_dir = os.path.join(ROOT, 'templates', name, 'source')
    if not dry_run and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    for fn, text in srcs.items():
        write_text(os.path.join(out_dir, fn), text, dry_run)
    meta = {
        'name': name, 'family': spec['family'], 'replace': name, 'main_source': f'{name}.cpp',
        'features': spec['features'], 'description': spec['description'],
        'api_version': versions.get(spec['family']), 'sample_origin': spec['sample_dir'],
        'sources': sorted(srcs),
    }
    write_text(os.path.join(ROOT, 'templates', name, 'template.json'), json.dumps(meta, indent=2) + '\n', dry_run)
    print(f'  {name:<22} regenerated ({len(srcs)} files)')


def update_template_api_versions(versions, dry_run):
    """Keep template.json api_version in step with include/ without touching the sources."""
    for name in sorted(os.listdir(os.path.join(ROOT, 'templates'))):
        jpath = os.path.join(ROOT, 'templates', name, 'template.json')
        if not os.path.exists(jpath):
            continue
        with open(jpath, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        want = versions.get(meta.get('family'))
        if want is not None and meta.get('api_version') != want:
            meta['api_version'] = want
            write_text(jpath, json.dumps(meta, indent=2) + '\n', dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--td', default=None, help='TouchDesigner install folder (default: newest installed)')
    ap.add_argument('--headers-only', action='store_true', help='skip the template drift report')
    ap.add_argument('--overwrite-templates', action='store_true',
                    help='regenerate templates/<Name>/source from the official samples (discards local fixes)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.td:
        td_dir, build = args.td, (0, 0)
        m = re.search(r'(\d{4})\.(\d+)\s*$', os.path.basename(os.path.normpath(td_dir)))
        if m:
            build = (int(m.group(1)), int(m.group(2)))
    else:
        installs = core.find_td_installs()
        if not installs:
            print('ERROR: no TouchDesigner install with Samples/CPlusPlus found. Pass --td <folder>.')
            return 2
        build, td_dir = installs[0]
    td_samples = os.path.join(td_dir, 'Samples', 'CPlusPlus')
    if not os.path.isdir(td_samples):
        print(f'ERROR: {td_samples} not found. Pass --td <TouchDesigner install folder>.')
        return 2
    label = core.td_build_label(build)
    print(f'TouchDesigner {label}: {td_samples}')
    versions = sync_headers(td_samples, label, args.dry_run)
    update_template_api_versions(versions, args.dry_run)
    if args.headers_only:
        return 0
    if args.overwrite_templates:
        print('Regenerating templates from the samples (local fixes are discarded):')
        for name, spec in TEMPLATES.items():
            overwrite_template(name, spec, td_samples, versions, args.dry_run)
    else:
        print('Template drift against the official samples (code only; templates are not modified):')
        for name, spec in TEMPLATES.items():
            report_drift(name, spec, td_samples)
    print('Done.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
