"""
deploy.py — PluginBuilder Release Packaging & Deployment Script

This script resets the PluginBuilder COMP to a clean default state
and exports it as PluginBuilder.tox for release distribution.
"""

import os

print('[Deploy] Packaging PluginBuilder.tox release component...')

# 1. Obtain reference to the PluginBuilder COMP
PluginBuilderComp = op('PluginBuilder')

# 2. Unlock component parameters for pristine creation state
PluginBuilderComp.EnableCreatePars()

# 3. Reset parameters to default initial values
PluginBuilderComp.par.Pluginname = ''
PluginBuilderComp.par.Plugintemplate.menuIndex = 0
PluginBuilderComp.par.Createinputop = False
PluginBuilderComp.par.Compileonupdate = True
PluginBuilderComp.par.Buildconfig = 'Release'

# 4. Clear transient CMakeLists text viewer content
PluginBuilderComp.op('CMakeLists').text = ''

# 5. Export sanitized COMP as PluginBuilder.tox
PluginBuilderComp.save('../PluginBuilder.tox')
print('[Deploy] Successfully exported PluginBuilder.tox')