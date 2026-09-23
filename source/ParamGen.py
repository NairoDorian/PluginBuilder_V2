"""
ParamGen.py — generate a real-time-friendly Parameters.h / Parameters.cpp pair from parameters.json.

TouchDesigner-independent (unit-tested in tests/test_core.py; used by PluginBuilderExt's
"Generate Parameters" pulse and by dev/gen_parameters.py). The generated layout is the one Plugin_FFT
settled on after measuring the cook path:

  * constexpr <Name>Name / <Name>Label pairs (a Name is what a .toe stores - never rename one),
  * one enum class per menu, with a COUNT sentinel and a static_assert tying it to its name table,
  * a plain `Values` struct with the defaults, filled ONCE per cook by `eval(inputs)` (so the DSP never
    goes back to TouchDesigner, and a test can build a Values by hand),
  * `setup(manager)` registering pages, ranges and defaults,
  * `setEnableStates(inputs, state)` for C++ API Common 3's setParameterEnableStates callback, from
    per-parameter `enable_if` C++ expressions over `v` (the evaluated Values) - no enablePar() in execute.

Spec (JSON):
{
  "namespace": "Parameters",                       // optional, default "Parameters"
  "pages": [
    { "name": "Controls", "pars": [
      { "name": "Gain", "label": "Gain", "type": "float", "default": 1.0, "min": 0, "max": 4,
        "clamp": [0, 10], "field": "gain" },
      { "name": "Mode", "type": "menu", "enum": "Mode", "field": "mode",
        "items": [["Rms", "RMS"], ["Peak", "Peak"]], "default": "Rms" },
      { "name": "Smooth", "type": "toggle", "default": true, "field": "smooth" },
      { "name": "Attackms", "type": "float", "default": 10, "min": 0, "max": 500, "field": "attackMs",
        "enable_if": "v.smooth" },
      { "name": "Channels", "type": "int", "default": 2, "min": 1, "max": 64, "field": "channels" },
      { "name": "Reset", "type": "pulse" }
    ]}
  ]
}
Types: float, int, toggle, menu, pulse. `field` defaults to the name with a lower-case first letter.
`clamp` (optional) is enforced in eval(); `min`/`max` are slider ranges only (TouchDesigner convention).
"""

import json
import re

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_PARNAME = re.compile(r'^[A-Z][a-z0-9]*$')          # TouchDesigner custom parameter names: Capital + lower/digits
TYPES = ('float', 'int', 'toggle', 'menu', 'pulse')


class SpecError(ValueError):
    pass


def _field(p):
    return p.get('field') or (p['name'][0].lower() + p['name'][1:])


def _cstr(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _num(v, is_int=False):
    if is_int:
        return str(int(v))
    x = float(v)
    r = repr(x)
    return r if ('.' in r or 'e' in r or 'inf' in r or 'nan' in r) else r + '.0'


def validate(spec):
    """Raise SpecError for anything the generator cannot turn into correct C++. Returns the flat par list."""
    if not isinstance(spec, dict) or not isinstance(spec.get('pages'), list) or not spec['pages']:
        raise SpecError('spec needs a non-empty "pages" list')
    ns = spec.get('namespace', 'Parameters')
    if not _IDENT.match(ns):
        raise SpecError(f'invalid namespace {ns!r}')
    seen_names, seen_fields, seen_enums, flat = set(), set(), set(), []
    for page in spec['pages']:
        if not page.get('name'):
            raise SpecError('every page needs a "name"')
        for p in page.get('pars', []):
            name, typ = p.get('name', ''), p.get('type')
            if not _PARNAME.match(name):
                raise SpecError(f'parameter name {name!r} must be Capitalised letters/digits (TouchDesigner rule), e.g. "Gain"')
            if name in seen_names:
                raise SpecError(f'duplicate parameter name {name!r}')
            seen_names.add(name)
            if typ not in TYPES:
                raise SpecError(f'{name}: type must be one of {TYPES}')
            if typ != 'pulse':
                f = _field(p)
                if not _IDENT.match(f) or f in seen_fields:
                    raise SpecError(f'{name}: invalid or duplicate field {f!r}')
                seen_fields.add(f)
            if typ == 'menu':
                items = p.get('items') or []
                if not items or not all(isinstance(it, (list, tuple)) and len(it) == 2 for it in items):
                    raise SpecError(f'{name}: menu needs "items": [[Name, Label], ...]')
                for it in items:
                    if not _IDENT.match(it[0]):
                        raise SpecError(f'{name}: menu item name {it[0]!r} must be an identifier')
                enum = p.get('enum') or name
                if not _IDENT.match(enum) or enum in seen_enums:
                    raise SpecError(f'{name}: invalid or duplicate enum {enum!r}')
                seen_enums.add(enum)
                if 'default' in p and p['default'] not in [it[0] for it in items]:
                    raise SpecError(f'{name}: default {p["default"]!r} is not a menu item')
            if 'clamp' in p and (not isinstance(p['clamp'], (list, tuple)) or len(p['clamp']) != 2):
                raise SpecError(f'{name}: "clamp" must be [lo, hi]')
            flat.append((page['name'], p))
    return flat


def generate(spec, header_name='Parameters.h'):
    """Return (header_text, cpp_text) for the spec."""
    flat = validate(spec)
    ns = spec.get('namespace', 'Parameters')
    H, C = [], []
    H += ['// GENERATED by PluginBuilder (source/ParamGen.py) from parameters.json - edit the JSON, not this file.',
          '// Layout: constexpr names, enum-class menus, one eval() per cook into a plain Values struct,',
          '// setEnableStates() for setParameterEnableStates (C++ API Common 3).',
          '#pragma once', '', '#include "CPlusPlus_Common.h"', '', f'namespace {ns} {{', '']
    # enums
    for _page, p in flat:
        if p['type'] == 'menu':
            enum = p.get('enum') or p['name']
            items = ', '.join(f'{it[0]} = {i}' if i == 0 else it[0] for i, it in enumerate(p['items']))
            H.append(f'enum class {enum} : int {{ {items}, COUNT }};')
    H.append('')
    # names / labels
    for _page, p in flat:
        H.append(f'constexpr char {p["name"]}Name[] = {_cstr(p["name"])};  '
                 f'constexpr char {p["name"]}Label[] = {_cstr(p.get("label", p["name"]))};')
    H.append('')
    # Values
    H.append('// One cook\'s parameter values. The defaults are the registered defaults (setup() uses the same numbers).')
    H.append('struct Values {')
    for _page, p in flat:
        t = p['type']
        if t == 'pulse':
            continue
        f = _field(p)
        if t == 'float':
            H.append(f'    double {f} = {_num(p.get("default", 0.0))};')
        elif t == 'int':
            H.append(f'    int {f} = {_num(p.get("default", 0), True)};')
        elif t == 'toggle':
            H.append(f'    bool {f} = {"true" if p.get("default", False) else "false"};')
        elif t == 'menu':
            enum = p.get('enum') or p['name']
            d = p.get('default', p['items'][0][0])
            H.append(f'    {enum} {f} = {enum}::{d};')
    H.append('};')
    H.append('')
    H.append('// Reads every parameter once (call it from getOutputInfo, which precedes execute in every cook).')
    H.append('Values eval(const TD::OP_Inputs* inputs);')
    H.append('void setup(TD::OP_ParameterManager* manager);')
    H.append('// For setParameterEnableStates: greys out parameters whose enable_if is false. May run outside a cook.')
    H.append('void setEnableStates(const TD::OP_Inputs* inputs, TD::OP_ParEnableState* state);')
    H.append('')
    H.append(f'}} // namespace {ns}')
    H.append('')

    C += ['// GENERATED by PluginBuilder (source/ParamGen.py) from parameters.json - edit the JSON, not this file.',
          f'#include "{header_name}"', '', '#include <algorithm>', '', f'namespace {ns} {{', '', 'namespace {', '']
    for _page, p in flat:
        if p['type'] == 'menu':
            names = ', '.join(_cstr(it[0]) for it in p['items'])
            labels = ', '.join(_cstr(it[1]) for it in p['items'])
            enum = p.get('enum') or p['name']
            C.append(f'const char* k{p["name"]}Names[] = {{ {names} }};')
            C.append(f'const char* k{p["name"]}Labels[] = {{ {labels} }};')
            C.append(f'static_assert(sizeof(k{p["name"]}Names) / sizeof(k{p["name"]}Names[0]) == static_cast<size_t>({enum}::COUNT), '
                     f'"{p["name"]} menu/enum mismatch");')
    C += ['', '} // namespace', '', 'void setup(TD::OP_ParameterManager* manager)', '{']
    for page, p in flat:
        t = p['type']
        if t in ('float', 'int', 'toggle', 'pulse'):
            C.append('    {')
            C.append('        TD::OP_NumericParameter np;')
            C.append(f'        np.page = {_cstr(page)}; np.name = {p["name"]}Name; np.label = {p["name"]}Label;')
            if t == 'float' or t == 'int':
                C.append(f'        np.defaultValues[0] = {_num(p.get("default", 0))};')
                if 'min' in p:
                    C.append(f'        np.minSliders[0] = {_num(p["min"])};')
                if 'max' in p:
                    C.append(f'        np.maxSliders[0] = {_num(p["max"])};')
                if 'clamp' in p:
                    C.append(f'        np.minValues[0] = {_num(p["clamp"][0])}; np.maxValues[0] = {_num(p["clamp"][1])};')
                    C.append('        np.clampMins[0] = true; np.clampMaxes[0] = true;')
                C.append(f'        manager->{"appendFloat" if t == "float" else "appendInt"}(np);')
            elif t == 'toggle':
                C.append(f'        np.defaultValues[0] = {"1.0" if p.get("default", False) else "0.0"};')
                C.append('        manager->appendToggle(np);')
            else:
                C.append('        manager->appendPulse(np);')
            C.append('    }')
        elif t == 'menu':
            d = p.get('default', p['items'][0][0])
            C.append('    {')
            C.append('        TD::OP_StringParameter sp;')
            C.append(f'        sp.page = {_cstr(page)}; sp.name = {p["name"]}Name; sp.label = {p["name"]}Label; sp.defaultValue = {_cstr(d)};')
            C.append(f'        manager->appendMenu(sp, static_cast<int32_t>(sizeof(k{p["name"]}Names) / sizeof(k{p["name"]}Names[0])), '
                     f'k{p["name"]}Names, k{p["name"]}Labels);')
            C.append('    }')
    C += ['}', '', 'Values eval(const TD::OP_Inputs* inputs)', '{', '    Values v;', '    if (!inputs) return v;']
    for _page, p in flat:
        t = p['type']
        if t == 'pulse':
            continue
        f, n = _field(p), p['name']
        if t == 'float':
            expr = f'inputs->getParDouble({n}Name)'
            if 'clamp' in p:
                expr = f'std::clamp({expr}, {_num(p["clamp"][0])}, {_num(p["clamp"][1])})'
            C.append(f'    v.{f} = {expr};')
        elif t == 'int':
            expr = f'inputs->getParInt({n}Name)'
            if 'clamp' in p:
                expr = f'std::clamp({expr}, {_num(p["clamp"][0], True)}, {_num(p["clamp"][1], True)})'
            C.append(f'    v.{f} = {expr};')
        elif t == 'toggle':
            C.append(f'    v.{f} = inputs->getParInt({n}Name) != 0;')
        elif t == 'menu':
            enum = p.get('enum') or n
            C.append(f'    {{ const int m = inputs->getParInt({n}Name); '
                     f'v.{f} = (m < 0 || m >= static_cast<int>({enum}::COUNT)) ? Values{{}}.{f} : static_cast<{enum}>(m); }}')
    C += ['    return v;', '}', '', 'void setEnableStates(const TD::OP_Inputs* inputs, TD::OP_ParEnableState* state)', '{',
          '    if (!inputs || !state) return;']
    rules = [(p['name'], p['enable_if']) for _page, p in flat if p.get('enable_if')]
    if rules:
        C.append('    const Values v = eval(inputs);')
        for n, cond in rules:
            C.append(f'    state->setEnableState({n}Name, static_cast<bool>({cond}));')
    else:
        C.append('    (void)inputs; (void)state;')
    C += ['}', '', f'}} // namespace {ns}', '']
    return '\n'.join(H), '\n'.join(C)


def generate_files(spec_path, out_dir, basename='Parameters'):
    """Read parameters.json and write <out_dir>/<basename>.h/.cpp. Returns (header_path, cpp_path)."""
    import os
    with open(spec_path, 'r', encoding='utf-8') as f:
        spec = json.load(f)
    h, c = generate(spec, header_name=f'{basename}.h')
    hp, cp = os.path.join(out_dir, f'{basename}.h'), os.path.join(out_dir, f'{basename}.cpp')
    for path, text in ((hp, h), (cp, c)):
        old = None
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                old = f.read()
        if old != text:                      # untouched files keep their mtime: no needless rebuild
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
    return hp, cp
