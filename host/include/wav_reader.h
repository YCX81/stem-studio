#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <vector>

namespace stemstudio {

struct Pcm16Wave final {
    std::uint32_t sample_rate{0};
    std::uint16_t channels{0};
    std::vector<std::int16_t> interleaved;

    [[nodiscard]] std::size_t frames() const noexcept {
        return channels == 0 ? 0 : interleaved.size() / channels;
    }
};

[[nodiscard]] Pcm16Wave read_pcm16_wav(const std::filesystem::path& path);

}  // namespace stemstudio
