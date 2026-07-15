#pragma once

#include "audio_window_buffer.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <mutex>
#include <optional>
#include <span>
#include <string>

namespace stemstudio {

struct PcmWindowDescriptor final {
    std::uint64_t sequence{};
    std::uint64_t stream_start_frame{};
    std::uint64_t stream_end_frame{};
};

struct PcmPublisherStats final {
    AudioGeometry geometry{};
    std::uint64_t pcm_frames{};
    std::uint64_t published_windows{};
    std::uint64_t last_published_sequence{};
};

class PcmWindowPublisher final {
public:
    using AnnotationProvider =
        std::function<std::optional<std::string>(const PcmWindowDescriptor&)>;

    explicit PcmWindowPublisher(
        std::filesystem::path live_root,
        AudioGeometry geometry = {},
        std::uint64_t initial_sequence = 0,
        AnnotationProvider annotation_provider = {});

    void append(std::span<const std::byte> pcm);
    [[nodiscard]] PcmPublisherStats stats() const;

private:
    AudioGeometry geometry_;
    std::filesystem::path live_root_;
    std::filesystem::path inbox_;
    mutable std::mutex mutex_;
    PcmPublisherStats stats_;
    AnnotationProvider annotation_provider_;
    AudioWindowBuffer windows_;
};

}  // namespace stemstudio
