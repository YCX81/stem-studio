#pragma once

#include "device_audio_protocol.h"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <span>
#include <vector>

namespace stemstudio {

struct DeviceAudioQueueStats final {
    std::uint64_t enqueued_packets{0};
    std::uint64_t dequeued_packets{0};
    std::uint64_t dropped_packets{0};
    std::uint64_t dropped_frames{0};
    std::size_t depth_packets{0};
    std::size_t capacity_packets{0};
};

struct DeviceQueuePopResult final {
    std::size_t bytes_read{0};
    bool packet_available{false};
    DevicePacketError error{DevicePacketError::none};
};

class DeviceAudioPacketQueue final {
public:
    DeviceAudioPacketQueue(
        std::uint32_t session_id,
        std::uint32_t sample_rate,
        std::size_t channels,
        std::size_t capacity_packets,
        std::size_t frames_per_packet);

    [[nodiscard]] bool try_submit(
        std::span<const std::int16_t> interleaved_pcm,
        bool silence) noexcept;

    [[nodiscard]] DeviceQueuePopResult try_pop(std::span<std::byte> output) noexcept;
    [[nodiscard]] DeviceAudioQueueStats stats() const noexcept;

private:
    struct Slot final {
        std::array<std::byte, device_max_datagram_bytes> bytes{};
        std::size_t size{0};
    };

    void record_drop(std::size_t packet_count, std::size_t frame_count) noexcept;

    std::uint32_t session_id_;
    std::uint32_t sample_rate_;
    std::uint8_t channels_;
    std::size_t frames_per_packet_;
    std::vector<Slot> slots_;

    mutable std::mutex mutex_;
    std::size_t read_index_{0};
    std::size_t write_index_{0};
    std::size_t depth_packets_{0};
    std::uint64_t enqueued_packets_{0};
    std::uint64_t dequeued_packets_{0};
    std::atomic<std::uint64_t> dropped_packets_{0};
    std::atomic<std::uint64_t> dropped_frames_{0};

    std::uint32_t next_sequence_{0};
    std::uint64_t next_presentation_frame_{0};
};

}  // namespace stemstudio
