#include "stem_sequence_loader.h"

#include "wav_reader.h"

#include <algorithm>
#include <format>
#include <stdexcept>

namespace stemstudio {

StemSequenceStitcher::StemSequenceStitcher(
    const std::size_t channels,
    const std::span<const StemId> active_stems,
    const std::size_t hop_frames,
    const std::size_t overlap_frames)
    : channels_{channels},
      hop_frames_{hop_frames},
      overlap_frames_{overlap_frames},
      active_stems_{active_stems.begin(), active_stems.end()} {
    if (active_stems_.empty()) {
        throw std::invalid_argument("at least one active stem is required for stitching");
    }
    for (std::size_t index = 0; index < active_stems_.size(); ++index) {
        if (std::find(active_stems_.begin(), active_stems_.begin() + static_cast<std::ptrdiff_t>(index),
                      active_stems_[index]) != active_stems_.begin() + static_cast<std::ptrdiff_t>(index)) {
            throw std::invalid_argument("duplicate active stem in stitcher");
        }
        stitchers_.emplace_back(channels_, hop_frames_, overlap_frames_);
    }
}

LoadedStemSequence StemSequenceStitcher::stitch(LoadedStemSequence sequence) {
    const auto expected_frames = hop_frames_ + overlap_frames_;
    const auto expected_samples = expected_frames * channels_;
    if (sequence.sequence == 0 || sequence.frames != expected_frames ||
        sequence.stems.size() != active_stems_.size()) {
        throw std::invalid_argument("overlap stem sequence geometry mismatch");
    }
    for (std::size_t index = 0; index < active_stems_.size(); ++index) {
        if (sequence.stems[index].id != active_stems_[index] ||
            sequence.stems[index].interleaved.size() != expected_samples) {
            throw std::invalid_argument("overlap stem sequence order or sample count mismatch");
        }
    }

    for (std::size_t index = 0; index < active_stems_.size(); ++index) {
        std::vector<std::int16_t> output(hop_frames_ * channels_);
        static_cast<void>(stitchers_[index].push(sequence.stems[index].interleaved, output));
        sequence.stems[index].interleaved = std::move(output);
    }
    sequence.frames = hop_frames_;
    return sequence;
}

void StemSequenceStitcher::reset() noexcept {
    for (auto& stitcher : stitchers_) {
        stitcher.reset();
    }
}

StemSequenceLoadResult load_stem_sequence(
    const std::filesystem::path& outbox,
    const std::uint64_t sequence,
    const std::span<const StemId> active_stems,
    const std::uint32_t expected_sample_rate,
    const std::uint16_t expected_channels) {
    if (sequence == 0 || active_stems.empty() || expected_sample_rate == 0 || expected_channels == 0) {
        throw std::invalid_argument("invalid separated sequence request");
    }

    const auto manifest = outbox / std::format("result-{:08}.json", sequence);
    if (!std::filesystem::is_regular_file(manifest)) {
        return {.state = StemSequenceState::waiting, .sequence = std::nullopt};
    }

    std::vector<std::filesystem::path> paths;
    paths.reserve(active_stems.size());
    for (const auto id : active_stems) {
        const auto path = outbox / std::format("result-{:08}-{}.wav", sequence, stem_name(id));
        if (!std::filesystem::is_regular_file(path)) {
            return {.state = StemSequenceState::skipped, .sequence = std::nullopt};
        }
        paths.push_back(path);
    }

    LoadedStemSequence loaded{
        .sequence = sequence,
        .frames = 0,
        .stems = {},
    };
    loaded.stems.reserve(active_stems.size());
    for (std::size_t index = 0; index < active_stems.size(); ++index) {
        auto wave = read_pcm16_wav(paths.at(index));
        if (wave.sample_rate != expected_sample_rate || wave.channels != expected_channels ||
            wave.interleaved.empty()) {
            throw std::runtime_error("separated stem WAV geometry mismatch");
        }
        if (loaded.frames == 0) {
            loaded.frames = wave.frames();
        } else if (wave.frames() != loaded.frames) {
            throw std::runtime_error("separated stems do not share the same frame count");
        }
        loaded.stems.push_back(LoadedStem{
            .id = active_stems[index],
            .interleaved = std::move(wave.interleaved),
        });
    }
    return {
        .state = StemSequenceState::ready,
        .sequence = std::move(loaded),
    };
}

}  // namespace stemstudio
