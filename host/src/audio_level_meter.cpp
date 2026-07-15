#include "audio_level_meter.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace stemstudio {
namespace {
constexpr std::size_t bytes_per_frame = 4;

std::int16_t read_pcm16_le(const std::byte low, const std::byte high) noexcept {
    const auto value = static_cast<std::uint16_t>(std::to_integer<unsigned int>(low)) |
                       static_cast<std::uint16_t>(std::to_integer<unsigned int>(high) << 8U);
    return static_cast<std::int16_t>(value);
}

float magnitude(const std::int16_t sample) noexcept {
    return std::min(1.0F, std::abs(static_cast<float>(sample)) / 32'768.0F);
}
}  // namespace

AudioLevels measure_pcm16_stereo(
    const std::span<const std::byte> pcm,
    const std::size_t waveform_bins) {
    if (pcm.size() % bytes_per_frame != 0) {
        throw std::invalid_argument("PCM level input must contain complete stereo frames");
    }
    if (waveform_bins == 0) {
        throw std::invalid_argument("PCM waveform requires at least one bin");
    }

    AudioLevels levels;
    levels.waveform.assign(waveform_bins, 0.0F);
    const auto frames = pcm.size() / bytes_per_frame;
    if (frames == 0) {
        return levels;
    }

    double left_square_sum = 0.0;
    double right_square_sum = 0.0;
    for (std::size_t frame = 0; frame < frames; ++frame) {
        const auto offset = frame * bytes_per_frame;
        const float left = magnitude(read_pcm16_le(pcm[offset], pcm[offset + 1]));
        const float right = magnitude(read_pcm16_le(pcm[offset + 2], pcm[offset + 3]));
        levels.peak_left = std::max(levels.peak_left, left);
        levels.peak_right = std::max(levels.peak_right, right);
        left_square_sum += static_cast<double>(left) * left;
        right_square_sum += static_cast<double>(right) * right;
        const auto bin = std::min(waveform_bins - 1, frame * waveform_bins / frames);
        levels.waveform[bin] = std::max(levels.waveform[bin], (left + right) * 0.5F);
    }
    levels.rms_left = static_cast<float>(std::sqrt(left_square_sum / frames));
    levels.rms_right = static_cast<float>(std::sqrt(right_square_sum / frames));
    return levels;
}

}  // namespace stemstudio
