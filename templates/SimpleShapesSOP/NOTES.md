# SimpleShapesSOP

Source: `Samples/CPlusPlus/SimpleShapesSOP/SimpleShapes.cpp/.h` (TouchDesigner 2025.33230),
class `SimpleShapes` renamed to `SimpleShapesSOP`.

## What it demonstrates

- CPU path (`execute`, `SOP_Output`): points, normals, colors, texcoords, triangles, lines,
  point/primitive groups and a bounding box. With an input connected, the input geometry and
  its custom attributes are copied through.
- GPU Direct path (`executeVBO`, enabled by the `Gpudirect` toggle): VBOs are written directly,
  with custom attributes and a bounding box. The result can be rendered but not used by other
  SOPs. The shape is chosen at compile time (`CUBE_VBO`/`LINE_VBO`/`PARTICLE_VBO`).
- A CHOP parameter whose first sample scales the shape.

## Real-time notes

- Both paths rebuild all geometry every time the SOP cooks. `cookEveryFrameIfAsked` is false,
  so that only happens when a parameter or input changes. If you make it animate every frame,
  prefer the GPU Direct path and `VBOBufferMode::Dynamic`.
- The CPU pass-through calls `addPoint`/`setNormal`/`setColor` once per point: fine for small
  inputs, slow for millions of points.
- VBO memory is not initialized. Every enabled attribute (normals, colors, texcoords, custom
  attributes) must be written for every vertex you use.

## Fixes relative to the official 2025.33230 sample

1. GPU Direct texcoords: `fillFaceVBO`/`fillLineVBO`/`fillParticleVBO` looped
   `numTexLayers + 1` times per vertex and read `inTexCoord[k * 2 + t]`. That read past the
   end of the 24-, 9- and 27-element source arrays, and wrote 2 texcoords per vertex into a
   buffer sized `numVertices * numTexLayers` (48 writes for 36 allocated in the cube case).
   Now each vertex's single TexCoord is written once per enabled layer.
2. GPU Direct custom attributes (`customColor`, `customVert`) were declared and never written,
   so their contents were undefined. They are zero-filled right after `allocVBO()`
   (`zeroCustomAttributeVBO`).
3. Input pass-through: the sample called `addTriangle()` on every primitive, which reads past
   the index array for primitives with fewer than 3 vertices and drops the extra vertices of
   larger polygons. Closed polygons are now fan-triangulated (exact for triangles and convex
   polygons), open ones become `addLine()`, and degenerate ones are skipped.
4. Input pass-through: `getTextures()` returns null when there are no texture layers. The
   sample dereferenced it unconditionally. Also a null check on `getInputSOP(0)`.
5. Chop parameter (both paths): read only when the CHOP has at least one channel and one
   sample.
6. `triangleGeometryVBO` (unused by default): the texture layer count was passed as the
   triangle count. Arguments fixed.
7. Reset is enabled again when the input is disconnected (the sample only re-enabled Shape and
   Scale). `myNumVBOTexLayers` is initialized. Info DAT row 2 no longer prints "offset" on
   macOS.
8. Comments: the `cookEveryFrameIfAsked` description and the Info CHOP channel count.
