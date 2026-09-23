// RealtimeCHOPCore.h - the TouchDesigner-free core of this plugin.
//
// Everything that computes lives here, so tests/core_tests.cpp and bench/core_bench.cpp can drive it
// without TouchDesigner. RealtimeCHOP.cpp only moves data between TouchDesigner and this file.
//
// Real-time rules this file keeps (and that a change should keep):
//   * no allocation, lock or system call in process() / the cook-side handoff after warm-up
//     (buffers grow only when a size grows, never shrink, never re-allocate at a steady size);
//   * the cook <-> worker handoff is two wait-free single-producer/single-consumer triple buffers
//     ("latest wins": a job or result that is overwritten before it is read is simply dropped);
//   * flush-to-zero/denormals-are-zero around every loop that can decay into denormals.
#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#if defined(_M_X64) || defined(__x86_64__)
#include <immintrin.h>
#endif

namespace rt {

// Flush-to-zero + denormals-are-zero for the current thread, restored on scope exit. A decaying
// envelope reaches denormal floats, and every operation on one costs ~100 cycles.
struct DenormalGuard {
#if defined(_M_X64) || defined(__x86_64__)
    unsigned int saved;
    DenormalGuard() noexcept : saved(_mm_getcsr()) { _mm_setcsr(saved | 0x8040u); }
    ~DenormalGuard() noexcept { _mm_setcsr(saved); }
#endif
    DenormalGuard(const DenormalGuard&) = delete;
    DenormalGuard& operator=(const DenormalGuard&) = delete;
};

// Wait-free single-producer / single-consumer handoff. Producer: fill back(), publish(). Consumer:
// if (acquire()) use(front()). Exactly one producer thread and one consumer thread.
template <typename T>
class TripleBuffer {
public:
    T& back() noexcept { return m_slots[m_back]; }
    bool publish() noexcept {                          // true if an unread slot was overwritten (dropped)
        const uint32_t prev = m_mid.exchange(m_back | kDirty, std::memory_order_acq_rel);
        m_back = prev & kIndexMask;
        return (prev & kDirty) != 0;
    }
    bool acquire() noexcept {
        if ((m_mid.load(std::memory_order_acquire) & kDirty) == 0) return false;
        m_front = m_mid.exchange(m_front, std::memory_order_acq_rel) & kIndexMask;
        return true;
    }
    const T& front() const noexcept { return m_slots[m_front]; }
private:
    static constexpr uint32_t kIndexMask = 3u, kDirty = 4u;
    T m_slots[3];
    alignas(64) std::atomic<uint32_t> m_mid{ 1u };   // one cache line each: no false sharing between threads
    alignas(64) uint32_t m_back{ 2u };
    alignas(64) uint32_t m_front{ 0u };
};

// ---- the example DSP: a multichannel level meter ------------------------------------------------
struct LevelParams {
    bool peak{ false };        // false: RMS of the block, true: absolute peak
    double gain{ 1.0 };
    bool smooth{ true };
    double attackMs{ 10.0 }, releaseMs{ 250.0 };
};

// Per-frame smoothing coefficient for a time constant, frame-rate independent: exp(-dt / tau).
inline float coefFromMs(double ms, double dtMs) noexcept
{
    return (ms <= 0.0 || dtMs <= 0.0) ? 0.0f : static_cast<float>(std::exp(-dtMs / ms));
}

class LevelMeter {
public:
    // Setup-time: sizes the per-channel state. Not for the hot path (it may allocate).
    void resize(size_t channels) { if (m_state.size() != channels) m_state.assign(channels, 0.0f); }
    void reset() noexcept { std::fill(m_state.begin(), m_state.end(), 0.0f); }

    // One block: chans[c][0..n) -> out[c] (the smoothed level). dtMs is the time since the previous block.
    void process(const float* const* chans, size_t nch, size_t n, double dtMs, const LevelParams& p, float* out) noexcept
    {
        DenormalGuard ftz;
        const float att = p.smooth ? coefFromMs(p.attackMs, dtMs) : 0.0f;
        const float rel = p.smooth ? coefFromMs(p.releaseMs, dtMs) : 0.0f;
        const size_t nc = std::min(nch, m_state.size());
        for (size_t c = 0; c < nc; ++c) {
            const float* x = chans[c];
            float level = 0.0f;
            if (x && n) {
                if (p.peak) {
                    float m = 0.0f;
                    for (size_t i = 0; i < n; ++i) m = std::max(m, std::fabs(x[i]));
                    level = m;
                } else {
                    double s2 = 0.0;
                    for (size_t i = 0; i < n; ++i) s2 += static_cast<double>(x[i]) * x[i];
                    level = static_cast<float>(std::sqrt(s2 / static_cast<double>(n)));
                }
            }
            level *= static_cast<float>(p.gain);
            float& st = m_state[c];
            const float k = (level > st) ? att : rel;         // attack when rising, release when falling
            st = level + k * (st - level);
            out[c] = st;
        }
    }
private:
    std::vector<float> m_state;
};

// ---- cook <-> worker plumbing ---------------------------------------------------------------------
struct Job {
    uint64_t seq{ 0 };
    size_t channels{ 0 }, samples{ 0 };
    double dtMs{ 1000.0 / 60.0 };
    bool reset{ false };
    LevelParams params;
    std::vector<float> data;                   // channels * samples, channel-major; grows, never shrinks
    void fill(const float* const* chans, size_t nch, size_t n) {
        channels = nch; samples = n;
        if (data.size() < nch * n) data.resize(nch * n);     // warm-up only
        for (size_t c = 0; c < nch; ++c) {
            if (chans[c]) std::memcpy(data.data() + c * n, chans[c], n * sizeof(float));
            else std::memset(data.data() + c * n, 0, n * sizeof(float));
        }
    }
};
struct Result {
    uint64_t seq{ 0 };
    double processUs{ 0.0 };
    std::vector<float> levels;                 // one per channel
};

// Runs LevelMeter either inline (sync) or on one worker thread (async). Cook side: submit() + latest().
class Runner {
public:
    ~Runner() { setAsync(false); }

    // Cook thread. Starting/stopping the worker is the one operation that may block (join).
    void setAsync(bool on) {
        if (on == m_running) return;
        if (on) {
            m_stop.store(false);
            m_running = true;
            m_thread = std::thread([this] { workerLoop(); });
        } else {
            m_stop.store(true);
            if (m_thread.joinable()) m_thread.join();
            m_running = false;
        }
    }
    bool async() const noexcept { return m_running; }

    // Cook thread: the job slot to fill, then submit(). Async: the worker picks it up (<= 1 ms);
    // sync: it is processed right here.
    Job& jobSlot() noexcept { return m_jobs.back(); }
    void submit() {
        if (m_jobs.publish()) m_dropped.fetch_add(1, std::memory_order_relaxed);
        if (!m_running && m_jobs.acquire()) run(m_jobs.front());
    }
    // Cook thread: the newest finished result (the previous one if nothing new arrived).
    const Result& latest() noexcept { m_results.acquire(); return m_results.front(); }
    uint64_t dropped() const noexcept { return m_dropped.load(std::memory_order_relaxed); }

private:
    void run(const Job& j) {
        const auto t0 = std::chrono::steady_clock::now();
        if (m_ptrs.size() < j.channels) m_ptrs.resize(j.channels);        // warm-up only
        for (size_t c = 0; c < j.channels; ++c) m_ptrs[c] = j.data.data() + c * j.samples;
        m_meter.resize(j.channels);                                        // no-op at a steady channel count
        if (j.reset) m_meter.reset();
        Result& r = m_results.back();
        if (r.levels.size() != j.channels) r.levels.resize(j.channels);    // warm-up only
        m_meter.process(m_ptrs.data(), j.channels, j.samples, j.dtMs, j.params, r.levels.data());
        r.seq = j.seq;
        r.processUs = std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count();
        m_results.publish();
    }
    void workerLoop() {
        while (!m_stop.load(std::memory_order_acquire)) {
            if (m_jobs.acquire()) { run(m_jobs.front()); continue; }
            // A short sleep keeps the cook side free of any wake-up call. Note: on Windows a
            // sleep_for rounds up to the timer resolution unless a high-resolution waitable timer is
            // used (see Plugin_FFT's WorkerSignal) - fine for a meter, worth upgrading for analysis.
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    TripleBuffer<Job> m_jobs;
    TripleBuffer<Result> m_results;
    LevelMeter m_meter;
    std::vector<const float*> m_ptrs;
    std::thread m_thread;
    std::atomic<bool> m_stop{ false };
    bool m_running{ false };
    std::atomic<uint64_t> m_dropped{ 0 };
};

} // namespace rt
