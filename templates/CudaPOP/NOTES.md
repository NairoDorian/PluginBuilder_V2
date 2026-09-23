# CudaPOP

Source: `Samples/CPlusPlus/CudaPOP/CudaPOP.cpp/.h`, `kernel.cu` (TouchDesigner 2025.33230).
Requires the CUDA Toolkit.

## What it demonstrates

- Getting every attribute of an input POP as CUDA device buffers (`POP_BufferLocation::CUDA`)
  on the node's stream. The info buffers use `CPUOrCUDA`.
- Modifying `P` in place with a kernel (+1 on Z) between `beginCUDAOperations()` and
  `endCUDAOperations()`, then passing every buffer through to the output.
- `COPY_EXAMPLE` (off by default): write into a new buffer from `POP_Context::createBuffer()`
  instead of in place.
- When the point count is only known on the GPU, the kernel is launched for `maxInfo.points`
  threads and reads the real count from the GPU `POP_PointInfo`.

## Real-time notes

- Nothing is downloaded to the CPU. All buffers stay on the GPU, so there is no readback
  stall. Don't switch the attribute requests to `CPU` in a real-time path.
- CUDA buffers from `createBuffer()` must be created before `beginCUDAOperations()`, on the
  main thread.
- Release builds never synchronize after the launch. Debug builds call
  `cudaStreamSynchronize(stream)`, which stalls.
- The stream is non-blocking (`cudaStreamNonBlocking`). TouchDesigner orders work through the
  stream given in `POP_GetBufferInfo`/`POP_SetBufferInfo`.

## Fixes relative to the official 2025.33230 sample

1. `kernel.cu`: `cudaGetLastError()` after the launch in every build. The error is returned and
   reported through `getErrorString` (now a `std::string`). The sample only checked in Debug.
2. Debug: `cudaStreamSynchronize(stream)` instead of `cudaDeviceSynchronize()`.
3. The stream is created with `cudaStreamCreateWithFlags(..., cudaStreamNonBlocking)`.
4. The return value of `beginCUDAOperations()` is checked. On failure no CUDA work is issued
   and the input passes through unchanged.
5. `getIndexBuffer()` is null-checked before `getBuffer()` and before `setIndexBuffer()`.
6. `shiftZPosition` returns early when the position or point-info buffer is missing (for
   example when `getAllInfoBuffers()` fails).
7. `COPY_EXAMPLE`: `createBuffer()` is null-checked, and the replaced attribute is found by name
   (`"P"`). The sample compared two `OP_SmartRef`s with `==`. `OP_SmartRef` has no
   `operator==`, so that compared two `operator bool()` results (always true) and replaced the
   first point attribute. The copy is only used when the kernel succeeded.
