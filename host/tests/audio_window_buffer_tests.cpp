#include "audio_window_buffer.h"
#include "audio_level_meter.h"
#include "center_suppressor.h"
#include "live_paths.h"
#include "pcm_window_publisher.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>
#include <cmath>
#include <filesystem>
#include <format>
#include <fstream>
#include <iostream>
#include <optional>
#include <string_view>

using stemstudio::AudioGeometry;
using stemstudio::AudioWindowBuffer;

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        std::cerr << message << '\n';
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
    const AudioGeometry default_geometry{};
    require(default_geometry.window_seconds == 12, "default analysis window must remain 12 seconds");
    require(default_geometry.hop_seconds == 6, "default capture hop must overlap by 50 percent");

    const std::vector<std::int16_t> meter_pcm{
        0, 0,
        16'384, -8'192,
        32'767, -16'384,
        -16'384, 8'192,
    };
    const auto levels = stemstudio::measure_pcm16_stereo(
        std::as_bytes(std::span{meter_pcm}), 4);
    require(levels.waveform.size() == 4, "level meter waveform bin count mismatch");
    require(levels.peak_left > 0.99F, "left peak must be normalized");
    require(levels.peak_right > 0.49F && levels.peak_right < 0.51F, "right peak must be normalized");
    require(levels.rms_left > levels.rms_right, "channel RMS levels must be independent");
    require(levels.waveform.back() > 0.3F, "waveform envelope must contain signal energy");

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

    const AudioGeometry airplay_geometry{10, 2, 16, 8, 2};
    std::vector<stemstudio::PcmWindowDescriptor> published_descriptors;
    stemstudio::PcmWindowPublisher publisher(
        temporary.path(),
        airplay_geometry,
        11,
        [&](const stemstudio::PcmWindowDescriptor& descriptor) -> std::optional<std::string> {
            published_descriptors.push_back(descriptor);
            return std::format(
                "{{\"sequence\":{},\"stream_start_frame\":{},\"stream_end_frame\":{}}}",
                descriptor.sequence,
                descriptor.stream_start_frame,
                descriptor.stream_end_frame);
        });
    const std::vector<std::byte> airplay_pcm(airplay_geometry.window_bytes(), std::byte{7});
    publisher.append(airplay_pcm);
    require(
        std::filesystem::is_regular_file(inbox / "capture-00000011.wav"),
        "AirPlay PCM must publish directly into the live inbox");
    const auto publisher_stats = publisher.stats();
    require(publisher_stats.pcm_frames == 80, "AirPlay PCM frame count mismatch");
    require(publisher_stats.published_windows == 1, "AirPlay published window count mismatch");
    require(publisher_stats.geometry.sample_rate == 10, "AirPlay status sample rate mismatch");
    require(publisher_stats.geometry.window_seconds == 8, "AirPlay status analysis window mismatch");
    require(publisher_stats.geometry.hop_seconds == 2, "AirPlay status hop mismatch");
    require(publisher_stats.last_published_sequence == 11, "AirPlay last sequence mismatch");
    require(published_descriptors.size() == 1, "AirPlay annotation callback count mismatch");
    require(
        published_descriptors.front().stream_start_frame == 0 &&
            published_descriptors.front().stream_end_frame == 80,
        "first AirPlay stream frame range mismatch");
    require(
        std::filesystem::is_regular_file(inbox / "capture-00000011.json"),
        "AirPlay capture annotation must commit with the WAV");

    const std::vector<std::byte> next_hop(airplay_geometry.hop_bytes(), std::byte{8});
    publisher.append(next_hop);
    require(published_descriptors.size() == 2, "second annotation was not published");
    require(
        published_descriptors.back().stream_start_frame == 20 &&
            published_descriptors.back().stream_end_frame == 100,
        "overlapping AirPlay stream frame range mismatch");

    TemporaryDirectory failed_publish;
    stemstudio::PcmWindowPublisher rejecting_publisher(
        failed_publish.path(),
        airplay_geometry,
        1,
        [](const stemstudio::PcmWindowDescriptor&) -> std::optional<std::string> {
            throw std::runtime_error("annotation failure");
        });
    bool annotation_rejected = false;
    try {
        rejecting_publisher.append(airplay_pcm);
    } catch (const std::runtime_error&) {
        annotation_rejected = true;
    }
    require(annotation_rejected, "annotation failure must fail the whole capture commit");
    require(
        !std::filesystem::is_regular_file(failed_publish.path() / "inbox" / "capture-00000001.wav"),
        "a WAV must never become visible without its annotation");
    require(
        !std::filesystem::is_regular_file(failed_publish.path() / "inbox" / "capture-00000001.wav.pending"),
        "failed capture staging file must be cleaned up");

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
