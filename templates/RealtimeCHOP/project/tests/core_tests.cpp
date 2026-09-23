// Headless tests for RealtimeCHOPCore.h (no TouchDesigner). Run: ctest --test-dir build -LE perf
#include "RealtimeCHOPCore.h"

#include <cmath>
#include <cstdio>
#include <thread>
#include <vector>

static int g_checks = 0, g_fail = 0;
#define CHECK(c) do { ++g_checks; if (!(c)) { ++g_fail; std::printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)
#define NEAR(a, b, t) CHECK(std::fabs(static_cast<double>(a) - static_cast<double>(b)) <= (t))

int main()
{
    const size_t n = 735;
    std::vector<float> sine(n), zero(n, 0.0f);
    for (size_t i = 0; i < n; ++i) sine[i] = 0.5f * std::sin(static_cast<float>(i) * 0.3f);
    const float* chans[2] = { sine.data(), zero.data() };

    // RMS and peak of a block, no smoothing
    rt::LevelMeter m;
    m.resize(2);
    rt::LevelParams p;
    p.smooth = false;
    float out[2];
    m.process(chans, 2, n, 16.7, p, out);
    NEAR(out[0], 0.5 / std::sqrt(2.0), 0.01);
    NEAR(out[1], 0.0, 1e-9);
    p.peak = true;
    m.process(chans, 2, n, 16.7, p, out);
    NEAR(out[0], 0.5, 0.01);

    // smoothing: a release of 100 ms at 60 fps decays by exp(-16.7/100) per frame
    p.peak = false; p.smooth = true; p.attackMs = 0.0; p.releaseMs = 100.0;
    m.reset();
    m.process(chans, 2, n, 16.7, p, out);                    // attack 0 ms: jumps straight up
    const float start = out[0];
    const float* silent[2] = { zero.data(), zero.data() };
    m.process(silent, 2, n, 16.7, p, out);
    NEAR(out[0], start * std::exp(-16.7 / 100.0), 1e-5);

    // triple buffer: latest wins, nothing torn, the last publish is visible
    rt::TripleBuffer<int> tb;
    for (int i = 1; i <= 10; ++i) { tb.back() = i; tb.publish(); }
    CHECK(tb.acquire() && tb.front() == 10);
    CHECK(!tb.acquire());

    // runner: sync processes inline; async hands over and delivers the same numbers
    rt::Runner r;
    auto submit = [&](uint64_t seq) {
        rt::Job& j = r.jobSlot();
        j.seq = seq; j.dtMs = 16.7; j.params = rt::LevelParams{}; j.params.smooth = false;
        j.fill(chans, 2, n);
        r.submit();
    };
    submit(1);
    CHECK(r.latest().seq == 1);
    NEAR(r.latest().levels[0], 0.5 / std::sqrt(2.0), 0.01);
    r.setAsync(true);
    submit(2);
    bool got = false;
    for (int i = 0; i < 2000 && !got; ++i) { got = r.latest().seq == 2; std::this_thread::yield(); std::this_thread::sleep_for(std::chrono::microseconds(500)); }
    CHECK(got);
    NEAR(r.latest().levels[0], 0.5 / std::sqrt(2.0), 0.01);
    r.setAsync(false);
    CHECK(!r.async());

    std::printf("%d checks, %d failures\n", g_checks, g_fail);
    return g_fail ? 1 : 0;
}
