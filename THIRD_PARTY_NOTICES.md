# Third-party material in PluginBuilder_V2

PluginBuilder's own code — `source/`, `cmake/`, `dev/`, `tests/`, the template metadata and notes
(`template.json`, `NOTES.md`) and the original templates' code (`templates/RealtimeCHOP/`) — is under the MIT
License in [`LICENSE`](LICENSE). The material below is **not** covered by that license; it keeps its own terms.

| Path | What it is | Terms |
|---|---|---|
| `include/` | TouchDesigner C++ SDK headers (CHOP API 10, Common API 3), synced from the installed TouchDesigner 2025.33230 `Samples/CPlusPlus` by `dev/sync_templates.py` | Derivative Inc.'s **Shared Use License** — the notice at the top of each header: use only in conjunction with TouchDesigner by a TouchDesigner licensee; modified versions may be redistributed if the notice is kept and Derivative's name is not used to endorse them |
| `templates/BasicCHOP`, `BasicDAT`, `CHOPWithPythonClass`, `CPUMemoryTOP`, `CudaTOP`, `CudaPOP`, `SimpleShapesSOP`, `SimpleShapesPOP` — the files under `source/` that carry the Shared Use notice | Derivative's C++ operator samples, adapted into templates | Derivative's Shared Use License (notice kept in each file). A plugin you create from one of these templates inherits that notice for those portions |
| `3rdParty/opencv/` | OpenCV 4.8.0 headers and Windows import libraries (inherited from upstream PluginBuilder; the same set TouchDesigner's samples use) | OpenCV's license: Apache License 2.0 for 4.5+ (<https://github.com/opencv/opencv/blob/4.8.0/LICENSE>); older headers carry their 3-clause BSD notice inline |
| `3rdParty/Python/` | CPython 3.11 headers and import libraries, for the `python` template feature (inherited from upstream) | Python Software Foundation License (<https://docs.python.org/3.11/license.html>) |

Generated projects: `td_add_plugin` compiles against `include/` and, with the `python` / `opencv` features, links
the libraries above. `cmake/TDPlugin.cmake` prefers the running TouchDesigner's own `Samples/CPlusPlus/3rdParty`
copies when they exist. Whatever license you choose for your plugin, these components keep their terms; a GPL
plugin needs an additional permission for the TouchDesigner SDK (see `../Plugin_FFT/NOTICE.md` for one in the
FSF's recommended wording).
