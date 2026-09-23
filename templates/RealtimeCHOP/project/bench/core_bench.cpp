// Headless benchmark for RealtimeCHOPCore.h: median / p99 cost of one block, per channel count.
// Run: build/bin/Release/RealtimeCHOP_bench.exe   (the name follows the plugin name)
#include "RealtimeCHOPCore.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <vector>

int main()
{
    const size_t n = 735;                                   // one 60 fps block at 44.1 kHz
    for (size_t nch : { 1u, 2u, 8u, 64u }) {
        std::vector<std::vector<float>> data(nch, std::vector<float>(n));
        std::vector<const float*> ptrs(nch);
        for (size_t c = 0; c < nch; ++c) {
            for (size_t i = 0; i < n; ++i) data[c][i] = std::sin(static_cast<float>(i * (c + 1)) * 0.01f);
            ptrs[c] = data[c].data();
        }
        rt::LevelMeter m;
        m.resize(nch);
        rt::LevelParams p;
        std::vector<float> out(nch);
        for (int i = 0; i < 200; ++i) m.process(ptrs.data(), nch, n, 16.7, p, out.data());   // warm-up
        std::vector<double> t(5000);
        for (auto& v : t) {
            const auto t0 = std::chrono::steady_clock::now();
            m.process(ptrs.data(), nch, n, 16.7, p, out.data());
            v = std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count();
        }
        std::sort(t.begin(), t.end());
        std::printf("%3zu channel(s) x %zu samples: median %7.2f us  p99 %7.2f us  (%.3f%% of a 60 fps frame)\n",
                    nch, n, t[t.size() / 2], t[t.size() * 99 / 100], 100.0 * t[t.size() / 2] / 16666.7);
    }
    return 0;
}
