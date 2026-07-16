#include "device_audio_queue.h"

#include <algorithm>
#include <atomic>
#include <limits>
#include <stdexcept>

namespace stemstudio {
namespace {
constexpr std::size_t pcm16_bytes_per_stereo_frame = 4;
constexpr std::size_t maximum_frames_per_packet =
    (device_max_datagram_bytes - device_audio_header_bytes) /
    pcm16_bytes_per_stereo_frame;
}  // namespace

DeviceAudioPacketQueue::DeviceAudioPacketQueue(
    const std::uint32_t session_id,
    const std::uint32_t sample_rate,
    const std::size_t channels,
    const std::size_t capacity_packets,
    const std::size_t frames_per_packet)
    : session_id_{session_id},
      sample_rate_{sample_rate},
      channels_{static_cast<std::uint8_t>(channels)},
      frames_per_packet_{frames_per_packet},
      slots_(capacity_packets) {
    if ((sample_rate != 44'100U && sample_rate != 48'000U) || channels != 2 ||
        capacity_packets == 0 || frames_per_packet == 0 ||
        frames_per_packet > maximum_frames_per_packet) {
        throw std::invalid_argument("invalid device audio queue geometry");
    }
}

void DeviceAudioPacketQueue::record_drop(
    const std::size_t packet_count,
    const std::size_t frame_count) noexcept {
    dropped_packets_.fetch_add(packet_count, std::memory_order_relaxed);
    dropped_frames_.fetch_add(frame_count, std::memory_order_relaxed);
}

bool DeviceAudioPacketQueue::try_submit(
    const std::span<const std::int16_t> interleaved_pcm,
    const bool silence) noexcept {
    if (interleaved_pcm.empty() || interleaved_pcm.size() % channels_ != 0) {
        return false;
    }
    const std::size_t frame_count = interleaved_pcm.size() / channels_;
    const std::size_t packet_count =
        (frame_count + frames_per_packet_ - 1) / frames_per_packet_;
    if (packet_count > std::numeric_limits<std::uint32_t>::max()) {
        record_drop(packet_count, frame_count);
        next_presentation_frame_ += frame_count;
        return false;
    }

    const std::uint32_t first_sequence = next_sequence_;
    const std::uint64_t first_presentation_frame = next_presentation_frame_;
    next_sequence_ += static_cast<std::uint32_t>(packet_count);
    next_presentation_frame_ += frame_count;

    std::unique_lock lock{mutex_, std::try_to_lock};
    if (!lock.owns_lock() || packet_count > slots_.size() - depth_packets_) {
        record_drop(packet_count, frame_count);
        return false;
    }

    std::size_t consumed_frames = 0;
    for (std::size_t packet_index = 0; packet_index < packet_count; ++packet_index) {
        const std::size_t chunk_frames =
            std::min(frames_per_packet_, frame_count - consumed_frames);
        auto& slot = slots_.at((write_index_ + packet_index) % slots_.size());
        const DeviceAudioPacketHeader header{
            .session_id = session_id_,
            .sequence = first_sequence + static_cast<std::uint32_t>(packet_index),
            .presentation_frame = first_presentation_frame + consumed_frames,
            .sample_rate = sample_rate_,
            .channels = channels_,
            .flags = silence ? device_audio_flag_silence : std::uint16_t{0},
        };
        const auto samples = interleaved_pcm.subspan(
            consumed_frames * channels_, chunk_frames * channels_);
        const auto encoded = encode_device_audio_packet(header, samples, slot.bytes);
        if (encoded.error != DevicePacketError::none) {
            record_drop(packet_count, frame_count);
            return false;
        }
        slot.size = encoded.bytes_written;
        consumed_frames += chunk_frames;
    }

    write_index_ = (write_index_ + packet_count) % slots_.size();
    depth_packets_ += packet_count;
    enqueued_packets_ += packet_count;
    return true;
}

DeviceQueuePopResult DeviceAudioPacketQueue::try_pop(
    const std::span<std::byte> output) noexcept {
    const std::scoped_lock lock{mutex_};
    if (depth_packets_ == 0) {
        return {};
    }
    const auto& slot = slots_.at(read_index_);
    if (output.size() < slot.size) {
        return {
            .packet_available = true,
            .error = DevicePacketError::output_too_small,
        };
    }
    std::ranges::copy(
        std::span<const std::byte>{slot.bytes}.first(slot.size),
        output.begin());
    const std::size_t bytes_read = slot.size;
    read_index_ = (read_index_ + 1) % slots_.size();
    --depth_packets_;
    ++dequeued_packets_;
    return {
        .bytes_read = bytes_read,
        .packet_available = true,
    };
}

DeviceAudioQueueStats DeviceAudioPacketQueue::stats() const noexcept {
    const std::scoped_lock lock{mutex_};
    return {
        .enqueued_packets = enqueued_packets_,
        .dequeued_packets = dequeued_packets_,
        .dropped_packets = dropped_packets_.load(std::memory_order_relaxed),
        .dropped_frames = dropped_frames_.load(std::memory_order_relaxed),
        .depth_packets = depth_packets_,
        .capacity_packets = slots_.size(),
    };
}

}  // namespace stemstudio
