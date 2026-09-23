# RealtimeCHOP

A PluginBuilder template, **not** a copy of a Derivative sample. It is the structure the Plugin_FFT
project arrived at after measuring its cook path, reduced to a small example: a multichannel level meter
(RMS or peak, with attack/release in ms). Replace the meter; keep the structure.

## What you get

| File | Role |
|---|---|
| `source/<Name>.h/.cpp` | The TouchDesigner side: parameter snapshot in `getOutputInfo`, `execute` that only copies data in and out, cached Info DAT strings, `setParameterEnableStates`, AVX2 CPU guard |
| `source/<Name>Core.h` | Everything that computes, TouchDesigner-free: `LevelMeter`, wait-free `TripleBuffer`, `Runner` (inline or worker thread), `DenormalGuard` |
| `parameters.json` | The parameter spec. **Generate Parameters** (Status page) or `python dev/gen_parameters.py <project>` regenerates `source/Parameters.h/.cpp` |
| `tests/core_tests.cpp` | Headless tests, `ctest --test-dir build -LE perf` (PluginBuilder's **Run Tests** pulse) |
| `bench/core_bench.cpp` | Per-block cost at 1/2/8/64 channels |

The generated `CMakeLists.txt` adds `td_plugin_optimize(<Name> REALTIME)` (AVX2, fast math, LTO, the
`td_cpu_guard.h` include path), the test and the bench.

## Real-time rules the template keeps

1. **Parameters are read once per cook**, in `getOutputInfo`, into a plain struct. `execute` and the core
   never call `getPar*`.
2. **No allocation, lock or system call in the steady state.** Buffers grow when a size grows and never
   shrink; the cook <-> worker handoff is two wait-free triple buffers ("latest wins").
3. **Info strings are formatted at most 4x per second.** TouchDesigner runs the whole info chain on every
   cook, whoever is looking.
4. **Enable states come from `setParameterEnableStates`** (C++ API Common 3), not `enablePar()` in `execute`.
5. **Denormals are flushed** around the processing loop.

## Caveats

* The worker polls with `sleep_for(1 ms)`. On Windows a sleep rounds up to the timer resolution (often
  15.6 ms) unless the process raised it, so the async result can lag by a frame or more. That is fine for a
  meter. For heavy analysis use a high-resolution waitable timer, as in Plugin_FFT's `WorkerSignal`.
* `cookEveryFrameIfAsked = true`: the node cooks only when something uses its output. Switch to
  `cookEveryFrame` if it must keep running unviewed.
