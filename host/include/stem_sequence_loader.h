#pragma once

#include "stem_mixer.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <vector>

namespace stemstudio {

enum class StemSequenceState {
    waiting,
    ready,
    skipped,
};

struct LoadedStem final {
    StemId id;
    std::vector<std::int16_t> interleaved;
};

struct LoadedStemSequence final {
    std::uint64_t sequence{0};
    std::size_t frames{0};
    std::vector<LoadedStem> stems;
};

struct StemSequenceLoadResult final {
    StemSequenceState state{StemSequenceState::waiting};
    std::optional<LoadedStemSequence> sequence;
};

class StemSequenceStitcher final {
public:
    StemSequenceStitcher(
        std::size_t channels,
        std::span<const StemId> active_stems,
        std::size_t hop_frames,
        std::size_t overlap_frames);

    [[nodiscard]] LoadedStemSequence stitch(LoadedStemSequence sequence);
    void reset() noexcept;

private:
    std::size_t channels_;
    std::size_t hop_frames_;
    std::size_t overlap_frames_;
    std::vector<StemId> active_stems_;
    std::vector<OverlapStitcher> stitchers_;
};

[[nodiscard]] StemSequenceLoadResult load_stem_sequence(
    const std::filesystem::path& outbox,
    std::uint64_t sequence,
    std::span<const StemId> active_stems,
    std::uint32_t expected_sample_rate,
    std::uint16_t expected_channels);

}  // namespace stemstudio
