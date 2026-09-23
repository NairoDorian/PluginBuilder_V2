# BasicDAT

Source: `Samples/CPlusPlus/DAT/CPlusPlusDATExample.cpp/.h` (TouchDesigner 2025.33230), class
`CPlusPlusDATExample` renamed to `BasicDAT`.

## What it demonstrates

- With an input DAT connected, the table or text is copied through. With no input, a table
  (Rows x Cols) or a text is generated. For a table, one row per channel of the `Chop`
  parameter is appended (the channel name and its first sample).
- `DAT_Output`: `setOutputDataType`, `setTableSize`, `setCellString`, `setText`.
- Info CHOP/DAT output.

## Real-time notes

- `cookEveryFrameIfAsked` is false, so the DAT cooks only when an input or parameter changes.
- Each `setCellString` copies a string. For large tables, resize once and fill the cells in
  one pass. Don't resize inside a loop. That was one of the fixes below.
- `enablePar()` is still used here, and it is correct in both directions. The newer
  `setParameterEnableStates()` override (see BasicCHOP) is the recommended pattern.

## Fixes relative to the official 2025.33230 sample

1. `execute()`: `setTableSize` is called once for all Chop rows. The sample called it inside
   the per-channel loop.
2. The function-static `tempBuffer` is now a local buffer. A static buffer is shared by every
   instance of the DAT.
3. Bounds: the Chop rows write columns 0 and 1, so the table gets at least 2 columns. The
   sample only handled `Cols == 0`, so with `Cols == 1` it wrote column 1 out of range.
   Negative Rows/Cols are clamped to 0.
4. Chop rows are appended only for a Table output (`setTableSize` is not valid for Text) and
   only when the CHOP has at least one channel and one sample. The sample read sample 0 of an
   empty CHOP.
5. The Info DAT declares 2 columns, which is what it fills (the sample declared 3). Row 2
   shows the input DAT path (`myDat` is now set). The macOS branch printed "offset" and the
   offset value there.
6. Input path: null check on `getInputDAT(0)`. Text input sets the output type to Text
   explicitly and checks cell (0,0) before reading it.
7. Comments: `cookEveryFrameIfAsked = false` was described as "cook every frame", and the Info
   CHOP channel count was wrong.
