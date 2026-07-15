#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <vector>

namespace stemstudio {

struct AudioGeometry {
    std::uint32_t sample_rate{44'100};
    std::uint16_t channels{2};
    std::uint16_t bits_per_sample{16};
    std::uint32_t window_seconds{12};
    std::uint32_t hop_seconds{6};

    [[nodiscard]] std::size_t bytes_per_frame() const;
    [[nodiscard]] std::size_t window_bytes() const;
    [[nodiscard]] std::size_t hop_bytes() const;
    void validate() const;
};

class AudioWindowBuffer final {
public:
    using WindowCallback = std::function<void(std::uint64_t, std::span<const std::byte>)>;

    AudioWindowBuffer(AudioGeometry geometry, WindowCallback callback, std::uint64_t initial_sequence = 1);
    void append(std::span<const std::byte> pcm);
    [[nodiscard]] std::size_t buffered_bytes() const noexcept;

private:
    AudioGeometry geometry_;
    WindowCallback callback_;
    std::vector<std::byte> pending_;
    std::uint64_t next_sequence_;
};

}  // namespace stemstudio
