# CPUMemoryTOP

Source: `Samples/CPlusPlus/CPUMemoryTOP/CPUMemoryTOP.cpp/.h`, `FrameQueue.cpp/.h`
(TouchDesigner 2025.33230).

## What it demonstrates

- `TOP_ExecuteMode::CPUMem`: fill a `TOP_Buffer` from `TOP_Context::createOutputBuffer()` and
  upload it with `TOP_Output::uploadBuffer()`. Other color buffers, cube maps and 3D textures
  are shown in commented-out calls.
- Reading an input TOP into CPU memory with `downloadTexture()`. The result is used one frame
  later so the main thread does not stall.
- `THREADING_EXAMPLE` (off by default; uncomment the `#define`): a producer thread fills
  buffers through `FrameQueue` while `execute()` uploads them. `THREADING_SIGNALED_PRODUCER`
  switches the producer from free-running to one frame per cook.

## Real-time notes

- `OP_TOPDownloadResult::getData()` blocks until the GPU-to-CPU copy is done. Calling it in
  the same cook as `downloadTexture()` stalls the frame. The sample reads the previous
  frame's result instead, which costs one frame of latency. For device output, pass the
  result to a worker thread and call `getData()` there.
- Free-running producer: the sleep stands in for a device delivering frames at its own rate
  (about 64 fps here). It is not synced to TouchDesigner's frame rate. `execute()` always takes
  the newest frame and drops older ones. With a real device, block in the SDK's wait/read call
  instead of sleeping. For exactly one frame per cook, use `THREADING_SIGNALED_PRODUCER`.
- Keep the main-thread lock short. `FrameQueue` holds its mutex only while moving buffers in
  and out of the deque. Allocation (`createOutputBuffer`, which is thread-safe) happens
  outside the lock.
- `fillBuffer` writes 256x256 RGBA32F (1 MiB) per frame on the CPU. At larger sizes, use
  8-bit formats or do the work in the producer thread.

## Fixes relative to the official 2025.33230 sample

1. `myStep` data race: it is now `std::atomic<double>`. The producer increments it under
   `mySettingsLock` into a local variable and passes that local to `fillBuffer`. The sample
   passed the shared member outside the lock. Reset (`pulsePressed`) also takes the lock.
2. `FrameQueue::getBufferToUpdate`: `createOutputBuffer()` is called outside the queue lock.
   The sample held the lock during the allocation, which could block `execute()`.
3. `FrameQueue::getBufferToUpload`: latest wins. It returns the newest frame and hands older
   ones back with `returnBuffer()`. The sample returned the oldest frame, which added a frame
   of latency when the producer was ahead.
4. RAII locks (`std::lock_guard`) replace the manual `lock()`/`unlock()` in `FrameQueue` and
   `execute()`. Parameters are read before the lock is taken.
5. Producer: no negative `sleep_for` when a frame takes longer than 15.5 ms. The unused
   `std::random_device`/`mt19937`/distribution were removed (the "irregular interval" was
   never used).
6. `mySpeed` is initialized. `createOutputBuffer()` and `getData()` results are null-checked
   before `memcpy`. The cached download is released when the input is disconnected, so a stale
   frame is not shown when it is reconnected.
7. Header comment: `startMoreWork()` is also called by the destructor, not only in
   `THREADING_SIGNALED_PRODUCER` mode. Added `<mutex>`/`<condition_variable>` includes.
