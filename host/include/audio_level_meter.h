#pragma once

#include <cstddef>
#include <span>
#include <vector>

namespace stemstudio {

struct AudioLevels {
    float peak_left{};
    float peak_right{};
    float rms_left{};
    float rms_right{};
    std::vector<float> waveform;
};

[[nodiscard]] AudioLevels measure_pcm16_stereo(
    std::span<const std::byte> pcm,
    std::size_t waveform_bins = 32);

}  // namespace stemstudio
