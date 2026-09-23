# SimpleShapesPOP

Source: `Samples/CPlusPlus/SimpleShapesPOP/SimpleShapesPOP.cpp/.h` (TouchDesigner 2025.33230).

## What it demonstrates

- Building POP geometry in CPU buffers from `POP_Context::createBuffer()`: attribute buffers
  (P, N, Color, ...), an index buffer, and the info buffers (topology, point info, line
  strips, grid) for a cube, mixed primitives, complex mixed primitives and a grid.
- With an input connected: downloading every attribute, the index buffer and the info
  buffers to the CPU and passing them through.

## Real-time notes

- The input path is a download demonstration. `getData()` on a CPU-located input buffer waits
  for its GPU-to-CPU copy, once per attribute, every cook. Don't use that pattern for
  pass-through or GPU processing (see CudaPOP), or hold the buffers until the next cook.
- The generated shapes are rebuilt and uploaded on every cook. For static geometry, cook
  only when a parameter changes.
- `enablePar()` is used, and it is correct in both directions. `setParameterEnableStates()`
  (see BasicCHOP) is the newer pattern.

## Fixes relative to the official 2025.33230 sample

1. Index buffers are created with `indexBufCreateInfo` (`POP_BufferUsage::IndexBuffer`). The
   sample built that struct and then passed `attBufCreateInfo`, which has the wrong usage and
   the size of the last attribute buffer, not the index size. With the sample's shapes that
   size happened to be large enough (for example, 192 bytes of cube colors for 144 bytes of
   indices). Any edit that made the index data larger than the last attribute buffer would
   have overflowed the following `memcpy`. Fixed in `cubeGeometry`,
   `mixedPrimitives`, `mixedPrimitivesComplex` and `gridGeometry`.
2. The Shape menu default is `"cube"` (a menu name). The sample used the label `"Cube"`.
3. Input path: `getIndexBuffer()` is null-checked before use, and null attributes or buffers
   are skipped.
4. The Info DAT declares 1 row, which is what it fills (the sample declared 3).
5. Comment: the Info CHOP comment said "4 channels" (it outputs 1).
