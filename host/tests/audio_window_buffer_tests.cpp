#include "audio_window_buffer.h"
#include "center_suppressor.h"
#include "live_paths.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>
#include <cmath>
#include <filesystem>
#include <format>
#include <fstream>
#include <string_view>

using stemstudio::AudioGeometry;
using stemstudio::AudioWindowBuffer;

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
        path_ = std::filesystem::temp_directory_path() / std::format("stem-studio-tests-{}", suffix);
        std::filesystem::create_directories(path_);
    }
    ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
    TemporaryDirectory(const TemporaryDirectory&) = delete;
    TemporaryDirectory& operator=(const TemporaryDirectory&) = delete;
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};
}  // namespace

int main() {
    const AudioGeometry geometry{10, 2, 16, 8, 2};
    std::vector<std::uint64_t> sequences;
    std::vector<std::vector<std::byte>> windows;
    AudioWindowBuffer buffer(geometry, [&](const std::uint64_t sequence, const std::span<const std::byte> bytes) {
        sequences.push_back(sequence);
        windows.emplace_back(bytes.begin(), bytes.end());
    });

    std::vector<std::byte> first(geometry.window_bytes() - geometry.hop_bytes(), std::byte{1});
    buffer.append(first);
    require(windows.empty(), "partial window must not publish");

    std::vector<std::byte> hop(geometry.hop_bytes(), std::byte{2});
    buffer.append(hop);
    require(windows.size() == 1, "full window must publish once");
    require(windows.front().size() == geometry.window_bytes(), "published window size mismatch");
    require(sequences.front() == 1, "first sequence mismatch");

    buffer.append(hop);
    require(windows.size() == 2, "second hop must publish");
    require(sequences.back() == 2, "second sequence mismatch");
    require(buffer.buffered_bytes() == geometry.window_bytes() - geometry.hop_bytes(), "overlap retention mismatch");

    bool rejected = false;
    try {
        const std::vector<std::byte> partial_frame(3);
        buffer.append(partial_frame);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "partial PCM frame must be rejected");

    std::uint64_t resumed_sequence = 0;
    AudioWindowBuffer resumed(geometry, [&](const std::uint64_t sequence, const std::span<const std::byte>) {
        resumed_sequence = sequence;
    }, 42);
    std::vector<std::byte> full_window(geometry.window_bytes());
    resumed.append(full_window);
    require(resumed_sequence == 42, "sequence resume mismatch");

    constexpr std::size_t sample_rate = 44'100;
    std::vector<std::int16_t> centered(sample_rate * 2);
    std::vector<std::int16_t> side(sample_rate * 2);
    for (std::size_t frame = 0; frame < sample_rate; ++frame) {
        const auto tone = static_cast<std::int16_t>(std::sin(2.0 * 3.141592653589793 * 1'000.0 * frame / sample_rate) * 10'000);
        centered[frame * 2] = tone;
        centered[frame * 2 + 1] = tone;
        side[frame * 2] = tone;
        side[frame * 2 + 1] = static_cast<std::int16_t>(-tone);
    }
    stemstudio::CenterSuppressor centered_filter;
    const auto centered_result = centered_filter.process_instrumental(centered);
    stemstudio::CenterSuppressor side_filter;
    const auto side_result = side_filter.process_instrumental(side);
    std::int64_t centered_energy = 0;
    std::int64_t side_energy = 0;
    for (std::size_t index = sample_rate; index < centered_result.size(); ++index) {
        centered_energy += std::abs(centered_result[index]);
        side_energy += std::abs(side_result[index]);
    }
    require(centered_energy * 4 < side_energy, "center suppressor must preserve side energy");

    for (const std::wstring_view stem : {L"instrumental", L"vocals", L"drums", L"bass", L"other", L"guitar", L"piano"}) {
        require(stemstudio::is_supported_monitor_stem(stem), "known monitor stem rejected");
    }
    require(!stemstudio::is_supported_monitor_stem(L"malicious/path"), "unknown monitor stem accepted");

    TemporaryDirectory temporary;
    const auto inbox = temporary.path() / "inbox";
    const auto outbox = temporary.path() / "outbox";
    std::filesystem::create_directories(inbox);
    std::filesystem::create_directories(outbox);
    std::ofstream{inbox / "capture-00000003.wav"}.put('\0');
    std::ofstream{outbox / "result-00000008.json"}.put('\0');
    require(stemstudio::next_capture_sequence(inbox, outbox) == 9, "sequence must include published results");

    require(
        stemstudio::probe_playback(outbox, 9, L"guitar") == stemstudio::PlaybackAvailability::waiting,
        "missing result must wait");
    std::ofstream{outbox / "result-00000009.json"}.put('\0');
    require(
        stemstudio::probe_playback(outbox, 9, L"guitar") == stemstudio::PlaybackAvailability::skipped,
        "error manifest must skip missing stem");
    std::ofstream{outbox / "result-00000010-guitar.wav"}.put('\0');
    require(
        stemstudio::probe_playback(outbox, 10, L"guitar") == stemstudio::PlaybackAvailability::ready,
        "published stem must be ready");
    return 0;
}
