#include "center_suppressor.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace stemstudio {

CenterSuppressor::CenterSuppressor(
    const std::uint32_t sample_rate,
    const float strength,
    const float bass_preserve_hz)
    : strength_(strength) {
    if (sample_rate == 0 || strength < 0.0F || strength > 1.0F || bass_preserve_hz <= 0.0F) {
        throw std::invalid_argument("invalid center suppression parameters");
    }
    constexpr float pi = 3.14159265358979323846F;
    lowpass_coefficient_ = 1.0F - std::exp(-2.0F * pi * bass_preserve_hz / static_cast<float>(sample_rate));
}

std::vector<std::int16_t> CenterSuppressor::process_instrumental(
    const std::span<const std::int16_t> interleaved_stereo) {
    if (interleaved_stereo.size() % 2 != 0) {
        throw std::invalid_argument("stereo PCM must contain complete frames");
    }
    std::vector<std::int16_t> output(interleaved_stereo.size());
    for (std::size_t index = 0; index < interleaved_stereo.size(); index += 2) {
        const auto left = static_cast<float>(interleaved_stereo[index]);
        const auto right = static_cast<float>(interleaved_stereo[index + 1]);
        const auto center = (left + right) * 0.5F;
        center_lowpass_ += lowpass_coefficient_ * (center - center_lowpass_);
        const auto removable_center = center - center_lowpass_;
        const auto correction = strength_ * removable_center;
        output[index] = static_cast<std::int16_t>(std::clamp(left - correction, -32768.0F, 32767.0F));
        output[index + 1] = static_cast<std::int16_t>(std::clamp(right - correction, -32768.0F, 32767.0F));
    }
    return output;
}

}  // namespace stemstudio
