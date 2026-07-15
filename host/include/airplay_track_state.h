#pragma once

#include <cstdint>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace stemstudio {

struct AirPlayTrackSnapshot final {
    std::uint64_t revision{};
    std::uint64_t metadata_revision{};
    bool has_progress{};
    std::uint32_t start_rtp{};
    std::uint32_t current_rtp{};
    std::uint32_t end_rtp{};
    std::uint64_t anchor_stream_frame{};
    std::string title;
    std::string artist;
    std::string album;

    [[nodiscard]] double position_seconds() const noexcept;
    [[nodiscard]] double position_seconds_at(std::uint64_t stream_frame) const noexcept;
    [[nodiscard]] double duration_seconds() const noexcept;
};

class AirPlayTrackState final {
public:
    [[nodiscard]] bool set_metadata(
        std::string_view dmap_tag,
        std::string_view value,
        std::uint64_t stream_frame = 0);
    [[nodiscard]] bool update_progress(
        std::uint32_t start_rtp,
        std::uint32_t current_rtp,
        std::uint32_t end_rtp,
        std::uint64_t stream_frame = 0);
    [[nodiscard]] AirPlayTrackSnapshot snapshot() const;
    [[nodiscard]] std::vector<AirPlayTrackSnapshot> anchors_for_window(
        std::uint64_t stream_start_frame,
        std::uint64_t stream_end_frame) const;

private:
    mutable std::mutex mutex_;
    AirPlayTrackSnapshot state_;
    bool progress_received_{};
    std::vector<AirPlayTrackSnapshot> anchors_;
};

[[nodiscard]] std::string serialize_airplay_capture_annotation(
    std::uint64_t sequence,
    std::uint64_t stream_start_frame,
    std::uint64_t stream_end_frame,
    const AirPlayTrackSnapshot& current_track,
    std::span<const AirPlayTrackSnapshot> anchors);

}  // namespace stemstudio
