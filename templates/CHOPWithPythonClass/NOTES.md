# CHOPWithPythonClass

Source: `Samples/CPlusPlus/CHOPWithPythonClass/CHOPWithPythonClass.cpp/.h` (TouchDesigner 2025.33230).

## What it demonstrates

- Exposes a Python class on the Custom OP: a `reset()` method and the `speedMod` (read/write)
  and `executeCount` (read-only) members, through `PyMethodDef`/`PyGetSetDef` tables.
- Calls a Python function from C++: `getSpeedAdjust(op, curSpeed)` in the node's callbacks
  DAT, via `createArgumentsTuple()` and `callPythonCallback()`.
- Otherwise the same timesliced generator/filter as BasicCHOP. This only works when the plugin
  is installed as a Custom OP, not when it is loaded in a CPlusPlus CHOP.

## Real-time notes

- Without an input, every cook calls into Python: it builds an argument tuple, calls
  `getSpeedAdjust`, then releases the references. That work runs on the main thread under the
  GIL, and a slow callback slows the whole frame. Keep the callback trivial, or skip it (for
  example, only when a parameter changes).
- `pyGetExecuteCount` uses `autoCook = true`, so reading `executeCount` from Python can make
  the node cook right then.
- Python reference counting: every `PyObject*` returned as a new reference must be
  `Py_DECREF`'d on every path, including error paths.

## Fixes relative to the official 2025.33230 sample

1. `pySetSpeedMod`: the `PyNumber_Float` result is released on every path. The sample leaked it
   when `PyErr_Occurred()` was set. The error check now uses the `-1.0 && PyErr_Occurred()`
   idiom after `PyFloat_AsDouble`.
2. `getSpeedAdjust` may return an `int` as well as a `float` (`PyLong_Check`). A conversion
   error (such as an int that is too large) is cleared and the speed is left unchanged.
3. `execute()`: checks `createArgumentsTuple()` for null before using it.
4. `setParameterEnableStates()` replaces `enablePar()` in `execute()`. The sample never
   re-enabled Shape after the input was disconnected.
5. Input path: no division by zero when the input has 0 samples, and the read index restarts
   for each channel (same fixes as BasicCHOP).
6. Generator: the shape is evaluated for every sample, not once per channel per timeslice.
7. Comments: the Info CHOP channel count, and the header now says "multiply by Scale" instead
   of "divide by 2".
