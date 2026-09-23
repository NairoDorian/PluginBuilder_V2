// td_cpu_guard.h — runtime CPU feature check for plugins compiled with /arch:AVX2 (or AVX-512).
//
// A plugin built by td_plugin_optimize(<name> AVX2|REALTIME) contains AVX2/FMA instructions everywhere
// the compiler chose to emit them, not only in hand-written intrinsics. On a CPU (or VM) without them
// the first such instruction raises an illegal-instruction fault inside TouchDesigner. The guard lets
// the plugin find out *before* touching any of that code and report an error string instead.
//
// Usage (once per process, e.g. in Fill*PluginInfo, then copied into each instance):
//     #include "td_cpu_guard.h"
//     static const bool g_cpuOk = td::cpuSupportsAVX2();
//     ... in execute(): if (!g_cpuOk) { zero the output; return; }  and report it in getErrorString().
//
// Keep the check itself free of AVX code: this header only uses CPUID/XGETBV and the OS query.
// On this toolchain td_plugin_optimize puts cmake/td_runtime on the include path automatically.
#pragma once

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <intrin.h>
#endif

namespace td {

// True when the CPU *and* the OS support AVX2 + FMA (OSXSAVE with XMM|YMM state enabled in XCR0).
inline bool cpuSupportsAVX2() noexcept
{
#if defined(_WIN32)
    // PF_AVX2_INSTRUCTIONS_AVAILABLE (40) is the OS's own answer and includes the XSAVE state; it
    // exists on Windows 10+. Fall through to CPUID when it is not available.
    if (IsProcessorFeaturePresent(40)) {
        int r[4] = { 0, 0, 0, 0 };
        __cpuid(r, 1);
        return (r[2] & (1 << 12)) != 0;   // FMA is a separate CPUID bit and is required too
    }
    int info[4] = { 0, 0, 0, 0 };
    __cpuid(info, 0);
    if (info[0] < 7) return false;
    __cpuidex(info, 7, 0);
    const bool avx2 = (info[1] & (1 << 5)) != 0;
    __cpuid(info, 1);
    const bool osxsave = (info[2] & (1 << 27)) != 0;
    const bool fma = (info[2] & (1 << 12)) != 0;
    if (!avx2 || !osxsave || !fma) return false;
    return (_xgetbv(0) & 0x6) == 0x6;
#else
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
}

// AVX-512F + the OS having enabled the opmask/ZMM state (XCR0 bits 5-7).
inline bool cpuSupportsAVX512F() noexcept
{
#if defined(_WIN32)
    int info[4] = { 0, 0, 0, 0 };
    __cpuid(info, 0);
    if (info[0] < 7) return false;
    __cpuidex(info, 7, 0);
    if ((info[1] & (1 << 16)) == 0) return false;
    __cpuid(info, 1);
    if ((info[2] & (1 << 27)) == 0) return false;
    return (_xgetbv(0) & 0xE6) == 0xE6;
#else
    return __builtin_cpu_supports("avx512f");
#endif
}

} // namespace td
