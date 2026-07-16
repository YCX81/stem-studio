#include "device_audio_queue.h"

#include <array>
#include <cstddef>
#include <cstdint>
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

void test_large_render_block_is_split_and_reassembles_bit_exactly() {
    constexpr std::size_t channels = 2;
    constexpr std::size_t total_frames = 1'036;
    constexpr std::size_t frames_per_packet = 220;
    std::vector<std::int16_t> source(total_frames * channels);
    for (std::size_t index = 0; index < source.size(); ++index) {
        source[index] = static_cast<std::int16_t>(
            static_cast<std::int32_t>(index % 60'000) - 30'000);
    }
    stemstudio::DeviceAudioPacketQueue queue{
        0x1234U, 44'100U, channels, 8, frames_per_packet};

    require(queue.try_submit(source, false), "render block must fit an empty queue");
    const auto after_submit = queue.stats();
    require(after_submit.depth_packets == 5, "1,036 frames must split into five packets");
    require(after_submit.enqueued_packets == 5, "enqueued packet count mismatch");
    require(after_submit.dropped_frames == 0, "successful submit must not drop frames");

    std::vector<std::int16_t> restored;
    restored.reserve(source.size());
    std::array<std::byte, stemstudio::device_max_datagram_bytes> wire{};
    std::uint64_t expected_presentation_frame = 0;
    for (std::uint32_t expected_sequence = 0; expected_sequence < 5; ++expected_sequence) {
        const auto popped = queue.try_pop(wire);
        require(popped.packet_available, "queued packet missing");
        require(popped.error == stemstudio::DevicePacketError::none, "queue pop failed");
        const auto parsed = stemstudio::parse_device_audio_packet(
            std::span<const std::byte>{wire}.first(popped.bytes_read));
        require(parsed.packet.has_value(), "queued datagram did not parse");
        require(parsed.packet->header.sequence == expected_sequence, "packet sequence gap");
        require(parsed.packet->header.presentation_frame == expected_presentation_frame,
                "packet presentation timeline gap");
        require(parsed.packet->header.flags == 0, "audio packet must not be marked silent");

        const auto sample_count = parsed.packet->payload.size() / sizeof(std::int16_t);
        std::vector<std::int16_t> decoded(sample_count);
        require(
            stemstudio::decode_device_pcm16(parsed.packet->payload, decoded) ==
                stemstudio::DevicePacketError::none,
            "queued PCM decode failed");
        restored.insert(restored.end(), decoded.begin(), decoded.end());
        expected_presentation_frame += parsed.packet->frame_count;
    }
    require(restored == source, "packet splitting must preserve every PCM sample");
    require(queue.stats().depth_packets == 0, "all packets must be consumed");
}

void test_full_queue_rejects_whole_block_and_exposes_timeline_gap() {
    constexpr std::array<std::int16_t, 8> four_frames{1, 2, 3, 4, 5, 6, 7, 8};
    stemstudio::DeviceAudioPacketQueue queue{9, 48'000, 2, 2, 2};
    require(queue.try_submit(four_frames, false), "first two-packet block must fit");
    require(!queue.try_submit(four_frames, false), "full queue must reject the whole block");
    auto stats = queue.stats();
    require(stats.depth_packets == 2, "rejected block must not partially enter queue");
    require(stats.dropped_packets == 2, "rejected packet count mismatch");
    require(stats.dropped_frames == 4, "rejected frame count mismatch");

    std::array<std::byte, stemstudio::device_max_datagram_bytes> wire{};
    require(queue.try_pop(wire).packet_available, "first packet missing");
    require(queue.try_pop(wire).packet_available, "second packet missing");
    require(queue.try_submit(four_frames, true), "queue must accept after drain");
    const auto popped = queue.try_pop(wire);
    const auto parsed = stemstudio::parse_device_audio_packet(
        std::span<const std::byte>{wire}.first(popped.bytes_read));
    require(parsed.packet.has_value(), "post-drop packet did not parse");
    require(parsed.packet->header.sequence == 4,
            "sequence must expose the two packets dropped by the producer");
    require(parsed.packet->header.presentation_frame == 8,
            "presentation frame must expose the four dropped frames");
    require(parsed.packet->header.flags == stemstudio::device_audio_flag_silence,
            "silence block must be marked for receiver diagnostics");
}

void test_short_consumer_buffer_does_not_discard_packet() {
    constexpr std::array<std::int16_t, 4> pcm{1, 2, 3, 4};
    stemstudio::DeviceAudioPacketQueue queue{1, 44'100, 2, 2, 2};
    require(queue.try_submit(pcm, false), "fixture submit failed");
    std::array<std::byte, 8> short_output{};
    const auto rejected = queue.try_pop(short_output);
    require(rejected.error == stemstudio::DevicePacketError::output_too_small,
            "short consumer output must be rejected");
    require(queue.stats().depth_packets == 1, "failed pop must preserve queued packet");
}
}  // namespace

int main() {
    test_large_render_block_is_split_and_reassembles_bit_exactly();
    test_full_queue_rejects_whole_block_and_exposes_timeline_gap();
    test_short_consumer_buffer_does_not_discard_packet();
    return 0;
}
