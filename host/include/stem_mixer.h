#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string_view>
#include <vector>

namespace stemstudio {

enum class StemId : std::uint8_t {
    vocals,
    instrumental,
    drums,
    bass,
    other,
    guitar,
    piano,
    count,
};

inline constexpr std::size_t stem_id_count = static_cast<std::size_t>(StemId::count);

[[nodiscard]] std::string_view stem_name(StemId id);
[[nodiscard]] std::optional<StemId> stem_id_from_name(std::string_view name) noexcept;
[[nodiscard]] std::span<const StemId> stems_for_profile(std::size_t track_count);

struct StemBlockView final {
    StemId id;
    std::span<const std::int16_t> interleaved;
};

class RealtimeStemMixer final {
public:
    explicit RealtimeStemMixer(std::size_t channels, std::size_t smoothing_frames);

    void set_gain(StemId id, float gain);
    [[nodiscard]] float current_gain(StemId id) const;
    [[nodiscard]] float target_gain(StemId id) const;

    void mix(
        std::span<const StemBlockView> stems,
        std::span<std::int16_t> output);

    [[nodiscard]] std::size_t channels() const noexcept { return channels_; }
    [[nodiscard]] std::size_t smoothing_frames() const noexcept { return smoothing_frames_; }

private:
    struct GainState final {
        float current{1.0F};
        float target{1.0F};
        std::size_t remaining_frames{0};
    };

    [[nodiscard]] static std::size_t index_for(StemId id);
    [[nodiscard]] float advance_gain(GainState& state) const noexcept;

    std::size_t channels_;
    std::size_t smoothing_frames_;
    std::array<GainState, stem_id_count> gains_{};
};

class OverlapStitcher final {
public:
    OverlapStitcher(
        std::size_t channels,
        std::size_t hop_frames,
        std::size_t overlap_frames);

    // Each chunk contains one hop followed by the overlap shared with the next
    // chunk. Exactly one hop is emitted on every call.
    [[nodiscard]] bool push(
        std::span<const std::int16_t> chunk,
        std::span<std::int16_t> output_hop);

    void reset() noexcept;
    [[nodiscard]] bool has_previous() const noexcept { return has_previous_; }
    [[nodiscard]] std::size_t channels() const noexcept { return channels_; }
    [[nodiscard]] std::size_t hop_frames() const noexcept { return hop_frames_; }
    [[nodiscard]] std::size_t overlap_frames() const noexcept { return overlap_frames_; }

private:
    std::size_t channels_;
    std::size_t hop_frames_;
    std::size_t overlap_frames_;
    std::vector<std::int16_t> previous_tail_;
    bool has_previous_{false};
};

}  // namespace stemstudio
