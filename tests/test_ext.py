"""
Headless smoke tests for source/PluginBuilderExt.py (the TouchDesigner adapter).

PluginBuilderExt only runs inside TouchDesigner, so until now nothing automated even compiled it -
which is how a "comments only" pass shipped a file that raised IndentationError, a module-level helper
folded into dead code, and a for/else that bound one mirrored parameter instead of all of them. These
tests import the module against a tiny fake of the TouchDesigner objects it touches and drive the
parameter-mirroring and reload-persistence paths.

    python -m unittest discover -s tests -v
"""

import os
import py_compile
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
SOURCE = os.path.join(ROOT, 'source')
sys.path.insert(0, SOURCE)

import PluginBuilderCore as core  # noqa: E402


# ------------------------------------------------------------------------------------------------
# Minimal fake of the TouchDesigner surface PluginBuilderExt uses in these paths
# ------------------------------------------------------------------------------------------------
class ParMode:
    CONSTANT, EXPRESSION, BIND = 'CONSTANT', 'EXPRESSION', 'BIND'


class FakePage:
    def __init__(self, owner, name):
        self.owner, self.name, self._pars = owner, name, []

    @property
    def pars(self):
        return list(self._pars)

    def _append(self, style, name, label='', size=1):
        comps = []
        for i in range(size):
            pname = name if size == 1 else f'{name}{"xyzw"[i]}'
            p = FakePar(pname, style, self, tuplet_name=name, vec_index=i, label=label, custom=True)
            comps.append(p)
            self._pars.append(p)
            self.owner._pars[pname] = p
        for p in comps:
            p.tuplet = comps
        return comps

    def __getattr__(self, attr):
        if attr.startswith('append'):
            style = attr[len('append'):]
            return lambda name, label='', size=1: self._append(style, name, label, size)
        raise AttributeError(attr)

    def destroy(self):
        self.owner._pages.remove(self)
        for p in list(self._pars):
            p.destroy()


class FakePar:
    def __init__(self, name, style, page, tuplet_name=None, vec_index=0, label='', custom=False, menu=()):
        self.name, self.style, self.page = name, style, page
        self.tupletName, self.vecIndex, self.label = tuplet_name or name, vec_index, label or name
        self.isCustom, self.menuNames, self.menuLabels = custom, tuple(menu), tuple(menu)
        self.val, self.expr, self.bindExpr, self.mode = 0, '', '', ParMode.CONSTANT
        self.default = self.min = self.max = self.normMin = self.normMax = 0
        self.clampMin = self.clampMax = False
        self.enable, self.readOnly, self.help = True, False, ''
        self.tuplet = [self]
        self.isPulse = style == 'Pulse'

    def destroy(self):
        owner = self.page.owner
        if self in self.page._pars:
            self.page._pars.remove(self)
        owner._pars.pop(self.name, None)

    def eval(self):
        return self.val


class ParAccessor:
    def __init__(self, owner):
        object.__setattr__(self, '_owner', owner)

    def __getattr__(self, name):
        try:
            return self._owner._pars[name]
        except KeyError:
            raise AttributeError(name)


class FakeOp:
    def __init__(self, name, parent=None):
        self.name, self.valid, self.viewer = name, True, False
        self._pars, self._pages, self._children, self._storage = {}, [], {}, {}
        self.par = ParAccessor(self)
        self.cooks = 0

    # ops
    def op(self, path):
        return self._children.get(path)

    def cook(self, force=False):
        self.cooks += 1

    def pars(self):
        return list(self._pars.values())

    # pages / storage
    @property
    def customPages(self):
        return list(self._pages)

    def appendCustomPage(self, name):
        pg = FakePage(self, name)
        self._pages.append(pg)
        return pg

    def fetch(self, key, default=None):
        return self._storage.get(key, default)

    def store(self, key, value):
        self._storage[key] = value


def make_loader():
    """A loader with a 'Spectrum' page holding a float, a menu, an XY pair and a pulse, plus a header."""
    loader = FakeOp('plugin_loader')
    pg = loader.appendCustomPage('Spectrum')
    pg.appendFloat('Gain', label='Gain')
    menu = pg.appendMenu('Scale', label='Scale')[0]
    menu.menuNames = menu.menuLabels = ('log', 'mel')
    menu.val = 'mel'
    pg.appendFloat('Pos', label='Position', size=2)
    pg.appendPulse('Reset', label='Reset')
    pg.appendHeader('Hdr', label='Section')
    loader.par.Gain.val = 0.5
    loader.par.Posx.mode, loader.par.Posx.expr = ParMode.EXPRESSION, 'absTime.frame'
    # built-in value parameter (persisted) and reload-control parameter (not persisted)
    loader._pars['timeslice'] = FakePar('timeslice', 'Toggle', pg)
    loader._pars['plugin'] = FakePar('plugin', 'File', pg)
    loader.par.plugin.val = 'old.dll'
    return loader


def import_ext():
    """Import PluginBuilderExt as a fresh module with the fake TD globals injected."""
    path = os.path.join(SOURCE, 'PluginBuilderExt.py')
    mod = types.ModuleType('PluginBuilderExt_under_test')
    mod.__file__ = path
    mod.ParMode = ParMode
    mod.run = lambda *a, **k: None
    mod.runs = []
    with open(path, 'r', encoding='utf-8') as f:
        code = compile(f.read(), path, 'exec')
    exec(code, mod.__dict__)
    return mod


def make_ext(mod, owner):
    ext = object.__new__(mod.PluginBuilderExt)
    ext.ownerComp = owner
    ext.core = core
    ext.loader_op = owner.op('plugin_loader')
    ext.mirror_pages = True
    ext.verbose = False
    ext._last_synced_sig = None
    ext._last_synced_pars = None
    ext._enable_sync_pending = False
    ext.on_par_value_change_map = {}
    ext.on_par_pulse_map = {}
    ext._log = lambda *a, **k: None
    return ext


class ExtCompileTests(unittest.TestCase):
    def test_every_source_file_compiles(self):
        """py_compile every .py under source/ and dev/ (catches IndentationError / SyntaxError)."""
        for folder in ('source', 'dev', 'tests'):
            for fn in os.listdir(os.path.join(ROOT, folder)):
                if fn.endswith('.py'):
                    py_compile.compile(os.path.join(ROOT, folder, fn), doraise=True,
                                       cfile=os.devnull if os.name != 'nt' else None)

    def test_module_level_helpers_exist(self):
        """_par_mode must be a module-level function (the reload paths call it by bare name)."""
        mod = import_ext()
        self.assertTrue(callable(getattr(mod, '_par_mode', None)))
        self.assertIs(mod._par_mode(), ParMode)


class ExtMirrorTests(unittest.TestCase):
    def setUp(self):
        self.mod = import_ext()
        self.owner = FakeOp('PluginBuilder')
        self.owner.appendCustomPage('Builder')           # the builder's own UI page
        self.owner._children['plugin_loader'] = make_loader()
        self.ext = make_ext(self.mod, self.owner)

    def test_rebuild_binds_every_mirrored_parameter(self):
        """The rebuild path must bind ALL value parameters (a for/else once bound only the last)."""
        self.ext.sync_custom_parameters(force_rebuild=True)
        loader = self.owner.op('plugin_loader')
        for name in ('Gain', 'Scale', 'Posx', 'Posy'):
            p = getattr(self.owner.par, name)
            self.assertEqual(p.mode, ParMode.BIND, name)
            self.assertEqual(p.bindExpr, f"me.op('{loader.name}').par.{name}", name)
        # pulses and headers stay unbound
        self.assertEqual(self.owner.par.Reset.bindExpr, '')
        self.assertEqual(self.owner.par.Hdr.bindExpr, '')
        # pages preserved
        self.assertEqual(self.owner.par.Gain.page.name, 'Spectrum')

    def test_labels_refreshed_for_existing_parameters(self):
        self.ext.sync_custom_parameters(force_rebuild=False)
        self.owner.op('plugin_loader').par.Gain.label = 'Gain (dB)'
        self.ext._last_synced_sig = None                 # force the rebuild path with existing mirror pars
        self.ext.sync_custom_parameters(force_rebuild=False)
        self.assertEqual(self.owner.par.Gain.label, 'Gain (dB)')

    def test_enable_states_follow_the_loader_after_an_edit(self):
        """X2: an edit of a mirrored parameter queues ONE deferred pass that copies enable/readOnly."""
        self.ext.sync_custom_parameters(force_rebuild=True)
        loader = self.owner.op('plugin_loader')
        queued = []
        self.mod.run = lambda *a, **k: queued.append((a, k))
        self.ext._enable_sync_pending = False
        self.owner.par.Gain.mode = ParMode.BIND
        self.ext.OnParValueChange(self.owner.par.Gain, 0.0)
        self.ext.OnParValueChange(self.owner.par.Gain, 0.1)   # coalesced: still one queued pass
        self.assertEqual(len(queued), 1)
        self.assertGreaterEqual(queued[0][1].get('delayFrames', 0), 1)
        # the plugin greyed Scale out and made Gain read-only on the loader
        loader.par.Scale.enable = False
        loader.par.Gain.readOnly = True
        self.assertEqual(self.ext._sync_enable_states(), 2)
        self.assertFalse(self.owner.par.Scale.enable)
        self.assertTrue(self.owner.par.Gain.readOnly)
        self.assertFalse(self.ext._enable_sync_pending)
        self.assertEqual(self.ext._sync_enable_states(), 0)  # steady state writes nothing

    def test_snapshot_restore_round_trip(self):
        """_snapshot_loader_values / _restore_loader_values work (they call the module-level _par_mode)."""
        old = self.owner.op('plugin_loader')
        snap = self.ext._snapshot_loader_values(old)
        kinds = {n: k for n, k, _v in snap}
        self.assertEqual(kinds.get('Gain'), 'val')
        self.assertEqual(kinds.get('Posx'), 'expr')
        self.assertIn('timeslice', kinds)
        self.assertNotIn('plugin', kinds)               # reload-control parameter
        self.assertNotIn('Reset', kinds)                # pulse
        fresh = make_loader()
        fresh.par.Gain.val = 0.0
        fresh.par.Scale.menuNames = ('log',)             # 'mel' vanished from the menu
        restored, skipped = self.ext._restore_loader_values(snap, target=fresh)
        self.assertEqual(fresh.par.Gain.val, 0.5)
        self.assertEqual(fresh.par.Posx.mode, ParMode.EXPRESSION)
        self.assertTrue(any(s.startswith("Scale='mel'") for s in skipped), skipped)
        self.assertGreater(restored, 0)


if __name__ == '__main__':
    unittest.main()
