#include "audio_window_buffer.h"

#include <algorithm>
#include <stdexcept>

namespace stemstudio {

std::size_t AudioGeometry::bytes_per_frame() const {
    return static_cast<std::size_t>(channels) * bits_per_sample / 8U;
}

std::size_t AudioGeometry::window_bytes() const {
    return static_cast<std::size_t>(sample_rate) * window_seconds * bytes_per_frame();
}

std::size_t AudioGeometry::hop_bytes() const {
    return static_cast<std::size_t>(sample_rate) * hop_seconds * bytes_per_frame();
}

void AudioGeometry::validate() const {
    if (sample_rate == 0 || channels != 2 || bits_per_sample != 16) {
        throw std::invalid_argument("live capture requires stereo PCM16 audio");
    }
    if (window_seconds < 8 || hop_seconds == 0 || window_seconds % hop_seconds != 0) {
        throw std::invalid_argument("invalid live window geometry");
    }
}

AudioWindowBuffer::AudioWindowBuffer(AudioGeometry geometry, WindowCallback callback, const std::uint64_t initial_sequence)
    : geometry_(geometry), callback_(std::move(callback)), next_sequence_(initial_sequence) {
    geometry_.validate();
    if (!callback_ || initial_sequence == 0) {
        throw std::invalid_argument("window callback is required");
    }
    pending_.reserve(geometry_.window_bytes() + geometry_.hop_bytes());
}

void AudioWindowBuffer::append(const std::span<const std::byte> pcm) {
    if (pcm.size() % geometry_.bytes_per_frame() != 0) {
        throw std::invalid_argument("PCM payload must contain complete frames");
    }
    pending_.insert(pending_.end(), pcm.begin(), pcm.end());
    while (pending_.size() >= geometry_.window_bytes()) {
        callback_(next_sequence_++, std::span<const std::byte>(pending_.data(), geometry_.window_bytes()));
        const auto hop = static_cast<std::ptrdiff_t>(geometry_.hop_bytes());
        pending_.erase(pending_.begin(), pending_.begin() + hop);
    }
}

std::size_t AudioWindowBuffer::buffered_bytes() const noexcept {
    return pending_.size();
}

}  // namespace stemstudio
