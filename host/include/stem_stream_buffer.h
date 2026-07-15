#pragma once

#include "stem_mixer.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <span>
#include <vector>

namespace stemstudio {

inline constexpr std::size_t default_live_buffer_capacity_hops = 4;
inline constexpr std::size_t default_live_prebuffer_hops = 3;

struct MutableStemBlockView final {
    StemId id;
    std::span<std::int16_t> interleaved;
};

enum class BufferReadState {
    prebuffering,
    audio,
    underrun,
};

struct StemBufferStats final {
    std::size_t buffered_frames{0};
    std::size_t capacity_frames{0};
    std::size_t prebuffer_frames{0};
    std::size_t minimum_buffered_frames{0};
    std::uint64_t total_written_frames{0};
    std::uint64_t total_read_frames{0};
    std::uint64_t underruns{0};
    std::uint64_t last_underrun_system_time_ns{0};
    std::size_t last_underrun_buffered_frames{0};
    std::uint64_t last_underrun_total_read_frames{0};
    bool prebuffering{true};
};

class SynchronizedStemBuffer final {
public:
    SynchronizedStemBuffer(
        std::size_t channels,
        std::span<const StemId> active_stems,
        std::size_t capacity_frames,
        std::size_t prebuffer_frames);

    // Returns false when the entire synchronized block cannot fit. No partial
    // stem or partial frame is written.
    [[nodiscard]] bool try_push(std::span<const StemBlockView> stems);

    // Every output block must describe the configured stems and the same frame
    // count. Non-audio states always clear every output sample to silence.
    [[nodiscard]] BufferReadState pop(std::span<const MutableStemBlockView> outputs);

    [[nodiscard]] StemBufferStats stats() const;
    [[nodiscard]] std::span<const StemId> active_stems() const noexcept { return active_stems_; }
    [[nodiscard]] std::size_t channels() const noexcept { return channels_; }

private:
    [[nodiscard]] static std::size_t index_for(StemId id);
    void clear_outputs(std::span<const MutableStemBlockView> outputs) const noexcept;

    std::size_t channels_;
    std::vector<StemId> active_stems_;
    std::array<bool, stem_id_count> active_{};
    std::array<std::vector<std::int16_t>, stem_id_count> storage_{};
    std::size_t capacity_frames_;
    std::size_t prebuffer_frames_;

    // Only one producer reserves and commits at a time. PCM copies happen
    // outside mutex_ so the real-time render thread never waits for a full
    // multi-second, multi-stem transfer.
    std::mutex producer_mutex_;
    mutable std::mutex mutex_;
    std::size_t read_frame_{0};
    std::size_t write_frame_{0};
    std::size_t buffered_frames_{0};
    std::size_t reserved_frames_{0};
    std::size_t minimum_buffered_frames_{0};
    std::uint64_t total_written_frames_{0};
    std::uint64_t total_read_frames_{0};
    std::uint64_t underruns_{0};
    std::uint64_t last_underrun_system_time_ns_{0};
    std::size_t last_underrun_buffered_frames_{0};
    std::uint64_t last_underrun_total_read_frames_{0};
    bool playing_{false};
    bool has_rendered_audio_{false};
};

}  // namespace stemstudio
