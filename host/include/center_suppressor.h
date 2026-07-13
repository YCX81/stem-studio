#pragma once

#include <cstdint>
#include <span>
#include <vector>

namespace stemstudio {

class CenterSuppressor final {
public:
    explicit CenterSuppressor(
        std::uint32_t sample_rate = 44'100,
        float strength = 0.88F,
        float bass_preserve_hz = 140.0F);

    [[nodiscard]] std::vector<std::int16_t> process_instrumental(
        std::span<const std::int16_t> interleaved_stereo);

private:
    float strength_;
    float lowpass_coefficient_;
    float center_lowpass_{0.0F};
};

}  // namespace stemstudio
