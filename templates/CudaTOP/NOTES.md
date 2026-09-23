# CudaTOP

Source: `Samples/CPlusPlus/CudaTOP/CudaTOP.cpp/.h`, `kernel.cu` (TouchDesigner 2025.33230).
Requires the CUDA Toolkit.

## What it demonstrates

- `TOP_ExecuteMode::CUDA`: request output `cudaArray`s with `createCUDAArray()` (primary, plus
  a 1280x720 aux output in color buffer 1) and the input with `getCUDAArray()`. Wrap them in
  surface objects and run kernels between `beginCUDAOperations()` and `endCUDAOperations()`.
- A per-node CUDA stream passed to TouchDesigner in `TOP_CUDAOutputInfo`/`OP_CUDAAcquireInfo`.
- Copy kernels for 2D, 3D, cube and 2D-array textures, and a fill kernel when no input is
  connected. The input must be BGRA8.

## Real-time notes

- Launches are asynchronous. Release builds never synchronize, so errors that happen while a
  kernel runs appear on a later CUDA call. Debug builds call `cudaStreamSynchronize(stream)`
  after each launch. That stalls the CPU, so don't ship it.
- The stream is created with `cudaStreamNonBlocking`, so it does not implicitly synchronize
  with the legacy default stream. TouchDesigner orders work through the stream you give it.
- Surface objects are cached and recreated only when the underlying `cudaArray` changes.
- All `inputs->getPar*` calls must happen before `beginCUDAOperations()`.

## Fixes relative to the official 2025.33230 sample

1. `kernel.cu`: `cudaGetLastError()` after the launches in every build. Errors are returned
   and shown in the node's error (`getErrorString`, now a `std::string`). The sample only
   checked in Debug builds and ignored the return value.
2. Debug: `cudaStreamSynchronize(stream)` instead of `cudaDeviceSynchronize()`. The latter
   also waits for TouchDesigner's own CUDA work.
3. The stream is created with `cudaStreamCreateWithFlags(..., cudaStreamNonBlocking)` (the
   sample used `cudaStreamCreate`). If creation fails, the default stream is used.
4. `cudaDestroySurfaceObject()` is no longer called on a 0 handle when there is no input.
5. Null check on `getInputTOP(0)`.
6. Comments: the second color parameter block was labelled "color 1", and the constructor
   comment claimed no CUDA work happens there (it creates the stream).
