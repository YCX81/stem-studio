#pragma once

#include <cstdint>
#include <filesystem>
#include <string_view>

namespace stemstudio {

enum class PlaybackAvailability {
    waiting,
    ready,
    skipped,
};

[[nodiscard]] bool is_supported_monitor_stem(std::wstring_view stem) noexcept;

[[nodiscard]] std::uint64_t next_capture_sequence(
    const std::filesystem::path& inbox,
    const std::filesystem::path& outbox);

[[nodiscard]] PlaybackAvailability probe_playback(
    const std::filesystem::path& outbox,
    std::uint64_t sequence,
    std::wstring_view stem);

}  // namespace stemstudio
