#pragma once

#include "stem_mixer.h"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>

namespace stemstudio {

struct MixerControlSnapshot final {
    std::uint64_t sequence{0};
    std::array<float, stem_id_count> gains{};
    std::array<bool, stem_id_count> present{};

    [[nodiscard]] bool has_gain(StemId id) const;
    [[nodiscard]] float gain(StemId id) const;
};

struct MixerControlMetricsSnapshot final {
    std::uint64_t update_count{0};
    std::uint64_t last_latency_microseconds{0};
    std::uint64_t maximum_latency_microseconds{0};
};

class MixerControlMetrics final {
public:
    explicit MixerControlMetrics(std::uint64_t observation_start_nanoseconds = 0) noexcept;
    void record_applied(
        std::uint64_t command_time_nanoseconds,
        std::uint64_t applied_time_nanoseconds) noexcept;
    [[nodiscard]] MixerControlMetricsSnapshot snapshot() const noexcept;

private:
    std::uint64_t observation_start_nanoseconds_{0};
    std::atomic<std::uint64_t> update_count_{0};
    std::atomic<std::uint64_t> last_latency_microseconds_{0};
    std::atomic<std::uint64_t> maximum_latency_microseconds_{0};
};

[[nodiscard]] std::filesystem::path mixer_control_path(
    const std::filesystem::path& live_root,
    std::size_t track_count);

[[nodiscard]] std::optional<MixerControlSnapshot> read_mixer_control(
    const std::filesystem::path& path);

}  // namespace stemstudio
