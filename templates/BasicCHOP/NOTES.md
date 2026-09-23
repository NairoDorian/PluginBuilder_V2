# BasicCHOP

Source: `Samples/CPlusPlus/CHOP/CPlusPlusCHOPExample.cpp/.h` (TouchDesigner 2025.33230), class
`CPlusPlusCHOPExample` renamed to `BasicCHOP`.

## What it demonstrates

- A timesliced CHOP with two modes. With an input connected, it copies input 0 multiplied by
  Scale. With no input, it generates a sine, square or ramp wave at 120 samples per second.
- Custom parameters: float, menu, dynamic menu (`buildDynamicMenu`), pulse, and a 4-component
  momentary parameter.
- `setParameterEnableStates()` (Common API 3). It replaces `inputs->enablePar()` and can
  enable or disable individual components of a parameter (`OP_ParmIndexFlagBits`).
- Info CHOP/DAT channels, `saveData`/`loadData` and input connector labels.

## Real-time notes

- `getParDouble`/`getParInt` are called on every cook. That is cheap, but for many parameters,
  cache them and re-read only when they change.
- `setParameterEnableStates()` can be called at any time, not only during a cook. Keep it to
  reading parameters/inputs and setting states. Don't touch cook state there.
- The waveform is evaluated with `sin`/`fmod` for every sample. Fine at 120 Hz. Use a phase
  accumulator or a table at audio rates.
- With an input, the output reads the first N input samples (N = the timeslice length). It
  does not resample. If the input is not timesliced, the result looks odd; the header comment
  explains why.

## Fixes relative to the official 2025.33230 sample

Re-apply these after regenerating the template from the sample (`dev/sync_templates.py --overwrite-templates`):

1. `execute()` no longer calls `enablePar()`. Speed, Reset and Shape are set in
   `setParameterEnableStates()`, in both directions. The sample never re-enabled Shape after
   the input was disconnected.
2. Input path: output 0 when the input has 0 samples or fewer channels. The sample's
   `ind % cinput->numSamples` divided by zero there.
3. Input path: the read index restarts at 0 for each channel. The sample carried `ind` from one
   channel into the next. The wrap now uses a compare instead of a modulo on every sample.
4. Generator: the shape is evaluated for every sample. The sample computed it once per channel,
   so each timeslice was a flat step even though `offset` advanced.
5. `loadData()`: skip entries whose `loadEntry()` returned null or a size of 0.
6. Comments: the Info CHOP comments said "one channel" (it outputs 2), and the header said
   "divide by 2" (it multiplies by Scale).
