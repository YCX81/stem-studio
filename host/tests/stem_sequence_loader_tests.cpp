#include "stem_sequence_loader.h"
#include "wav_writer.h"

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <format>
#include <fstream>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string{message});
    }
}

class TemporaryDirectory final {
public:
    TemporaryDirectory() {
        const auto suffix = std::chrono::steady_clock::now().time_since_epoch().count();
        path_ = std::filesystem::temp_directory_path() / std::format("stem-sequence-tests-{}", suffix);
        std::filesystem::create_directories(path_);
    }
    ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};

void write_stem(
    const std::filesystem::path& outbox,
    const std::uint64_t sequence,
    const std::string_view name,
    const std::span<const std::int16_t> samples) {
    const stemstudio::AudioGeometry geometry{44'100, 2, 16, 8, 8};
    const auto path = outbox / std::format("result-{:08}-{}.wav", sequence, name);
    stemstudio::write_pcm16_wav_atomic(path, geometry, std::as_bytes(samples));
}

void test_manifest_commits_a_synchronized_sequence() {
    TemporaryDirectory temporary;
    const std::array active{stemstudio::StemId::vocals, stemstudio::StemId::instrumental};
    const auto waiting = stemstudio::load_stem_sequence(temporary.path(), 1, active, 44'100, 2);
    require(waiting.state == stemstudio::StemSequenceState::waiting,
            "a sequence without a manifest must still be treated as in progress");

    const std::array<std::int16_t, 4> vocals{1, 2, 3, 4};
    const std::array<std::int16_t, 4> instrumental{101, 102, 103, 104};
    write_stem(temporary.path(), 1, "vocals", vocals);
    write_stem(temporary.path(), 1, "instrumental", instrumental);
    std::ofstream{temporary.path() / "result-00000001.json"} << "{}";

    const auto ready = stemstudio::load_stem_sequence(temporary.path(), 1, active, 44'100, 2);
    require(ready.state == stemstudio::StemSequenceState::ready && ready.sequence.has_value(),
            "manifest plus every active stem must be ready");
    require(ready.sequence->frames == 2, "loaded sequence frame count mismatch");
    require(ready.sequence->stems.size() == 2, "loaded sequence stem count mismatch");
    require(ready.sequence->stems[0].interleaved == std::vector<std::int16_t>{1, 2, 3, 4},
            "loaded PCM did not preserve the first stem");
}

void test_error_manifest_skips_missing_stems() {
    TemporaryDirectory temporary;
    const std::array active{stemstudio::StemId::vocals, stemstudio::StemId::drums};
    std::ofstream{temporary.path() / "result-00000009.json"} << "{\"error\":\"failed\"}";
    const auto skipped = stemstudio::load_stem_sequence(temporary.path(), 9, active, 44'100, 2);
    require(skipped.state == stemstudio::StemSequenceState::skipped && !skipped.sequence,
            "a committed error result must advance instead of waiting forever");
}

stemstudio::LoadedStemSequence make_overlap_sequence(
    const std::uint64_t sequence,
    const std::int16_t offset) {
    std::vector<std::int16_t> vocals(12);
    std::vector<std::int16_t> instrumental(12);
    for (std::size_t index = 0; index < vocals.size(); ++index) {
        vocals[index] = static_cast<std::int16_t>(offset + index);
        instrumental[index] = static_cast<std::int16_t>(offset + 100 + index);
    }
    return {
        .sequence = sequence,
        .frames = 6,
        .stems = {
            {.id = stemstudio::StemId::vocals, .interleaved = std::move(vocals)},
            {.id = stemstudio::StemId::instrumental, .interleaved = std::move(instrumental)},
        },
    };
}

void test_sequence_stitcher_emits_one_crossfaded_hop_for_all_stems() {
    const std::array active{stemstudio::StemId::vocals, stemstudio::StemId::instrumental};
    stemstudio::StemSequenceStitcher stitcher{2, active, 4, 2};

    auto first = stitcher.stitch(make_overlap_sequence(1, 0));
    require(first.frames == 4 && first.stems.size() == 2,
            "stitcher must preserve synchronized stem geometry");
    require(first.stems[0].interleaved == std::vector<std::int16_t>({0, 1, 2, 3, 4, 5, 6, 7}),
            "first sequence must emit its first hop unchanged");

    auto second = stitcher.stitch(make_overlap_sequence(2, 20));
    require(
        second.stems[0].interleaved ==
            std::vector<std::int16_t>({8, 9, 22, 23, 24, 25, 26, 27}),
        "next sequence must crossfade the shared timeline without duplicate frames");
    require(second.stems[1].interleaved.front() == 108,
            "every active stem must use the same stitch transition");

    stitcher.reset();
    auto after_reset = stitcher.stitch(make_overlap_sequence(4, 40));
    require(after_reset.stems[0].interleaved.front() == 40,
            "reset must prevent a crossfade across a missing sequence");
}
}  // namespace

int main() {
    test_manifest_commits_a_synchronized_sequence();
    test_error_manifest_skips_missing_stems();
    test_sequence_stitcher_emits_one_crossfaded_hop_for_all_stems();
    return 0;
}
