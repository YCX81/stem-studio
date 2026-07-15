#include "stem_stream_buffer.h"

#include <algorithm>
#include <chrono>
#include <stdexcept>

namespace stemstudio {
namespace {
[[nodiscard]] std::uint64_t current_system_time_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count());
}
}  // namespace

std::size_t SynchronizedStemBuffer::index_for(const StemId id) {
    const auto index = static_cast<std::size_t>(id);
    if (index >= stem_id_count) {
        throw std::invalid_argument("unknown stem id");
    }
    return index;
}

SynchronizedStemBuffer::SynchronizedStemBuffer(
    const std::size_t channels,
    const std::span<const StemId> active_stems,
    const std::size_t capacity_frames,
    const std::size_t prebuffer_frames)
    : channels_{channels},
      active_stems_{active_stems.begin(), active_stems.end()},
      capacity_frames_{capacity_frames},
      prebuffer_frames_{prebuffer_frames},
      minimum_buffered_frames_{capacity_frames} {
    if (channels_ == 0 || active_stems_.empty() || capacity_frames_ == 0) {
        throw std::invalid_argument("stream buffer geometry must be positive");
    }
    if (prebuffer_frames_ == 0 || prebuffer_frames_ > capacity_frames_) {
        throw std::invalid_argument("prebuffer must fit inside stream capacity");
    }

    for (const auto id : active_stems_) {
        const auto index = index_for(id);
        if (active_.at(index)) {
            throw std::invalid_argument("active stems must be unique");
        }
        active_.at(index) = true;
        storage_.at(index).resize(capacity_frames_ * channels_);
    }
}

bool SynchronizedStemBuffer::try_push(const std::span<const StemBlockView> stems) {
    if (stems.size() != active_stems_.size()) {
        throw std::invalid_argument("push must contain every active stem exactly once");
    }

    const auto sample_count = stems.empty() ? 0 : stems.front().interleaved.size();
    if (sample_count == 0 || sample_count % channels_ != 0) {
        throw std::invalid_argument("push must contain complete non-empty PCM frames");
    }
    const auto frame_count = sample_count / channels_;

    std::array<const StemBlockView*, stem_id_count> by_id{};
    for (const auto& stem : stems) {
        const auto index = index_for(stem.id);
        if (!active_.at(index) || by_id.at(index) != nullptr) {
            throw std::invalid_argument("push stem set does not match active stems");
        }
        if (stem.interleaved.size() != sample_count) {
            throw std::invalid_argument("all pushed stems must have the same length");
        }
        by_id.at(index) = &stem;
    }
    for (const auto id : active_stems_) {
        if (by_id.at(index_for(id)) == nullptr) {
            throw std::invalid_argument("push omitted an active stem");
        }
    }

    const std::scoped_lock producer_lock{producer_mutex_};
    std::size_t reserved_write_frame = 0;
    {
        const std::scoped_lock state_lock{mutex_};
        if (frame_count > capacity_frames_ - buffered_frames_ - reserved_frames_) {
            return false;
        }
        reserved_write_frame = write_frame_;
        write_frame_ = (write_frame_ + frame_count) % capacity_frames_;
        reserved_frames_ += frame_count;
    }

    const auto first_frames = (std::min)(frame_count, capacity_frames_ - reserved_write_frame);
    const auto first_samples = first_frames * channels_;
    const auto remaining_samples = sample_count - first_samples;
    for (const auto id : active_stems_) {
        const auto index = index_for(id);
        const auto& source = by_id.at(index)->interleaved;
        auto& destination = storage_.at(index);
        std::copy_n(
            source.begin(),
            first_samples,
            destination.begin() + static_cast<std::ptrdiff_t>(reserved_write_frame * channels_));
        if (remaining_samples > 0) {
            std::copy_n(
                source.begin() + static_cast<std::ptrdiff_t>(first_samples),
                remaining_samples,
                destination.begin());
        }
    }
    {
        const std::scoped_lock state_lock{mutex_};
        reserved_frames_ -= frame_count;
        buffered_frames_ += frame_count;
        total_written_frames_ += frame_count;
    }
    return true;
}

void SynchronizedStemBuffer::clear_outputs(
    const std::span<const MutableStemBlockView> outputs) const noexcept {
    for (const auto& output : outputs) {
        std::ranges::fill(output.interleaved, std::int16_t{0});
    }
}

BufferReadState SynchronizedStemBuffer::pop(
    const std::span<const MutableStemBlockView> outputs) {
    if (outputs.size() != active_stems_.size()) {
        throw std::invalid_argument("pop must contain every active stem exactly once");
    }
    const auto sample_count = outputs.empty() ? 0 : outputs.front().interleaved.size();
    if (sample_count == 0 || sample_count % channels_ != 0) {
        throw std::invalid_argument("pop must request complete non-empty PCM frames");
    }
    const auto frame_count = sample_count / channels_;
    if (frame_count > capacity_frames_) {
        throw std::invalid_argument("pop request exceeds stream capacity");
    }

    std::array<const MutableStemBlockView*, stem_id_count> by_id{};
    for (const auto& output : outputs) {
        const auto index = index_for(output.id);
        if (!active_.at(index) || by_id.at(index) != nullptr) {
            throw std::invalid_argument("pop stem set does not match active stems");
        }
        if (output.interleaved.size() != sample_count) {
            throw std::invalid_argument("all popped stems must have the same length");
        }
        by_id.at(index) = &output;
    }

    const std::scoped_lock lock{mutex_};
    if (!playing_) {
        if (buffered_frames_ < prebuffer_frames_ || buffered_frames_ < frame_count) {
            clear_outputs(outputs);
            return BufferReadState::prebuffering;
        }
        playing_ = true;
    }
    if (buffered_frames_ < frame_count) {
        minimum_buffered_frames_ = has_rendered_audio_
                                       ? (std::min)(minimum_buffered_frames_, buffered_frames_)
                                       : buffered_frames_;
        ++underruns_;
        last_underrun_system_time_ns_ = current_system_time_nanoseconds();
        last_underrun_buffered_frames_ = buffered_frames_;
        last_underrun_total_read_frames_ = total_read_frames_;
        playing_ = false;
        clear_outputs(outputs);
        return BufferReadState::underrun;
    }

    const auto first_frames = (std::min)(frame_count, capacity_frames_ - read_frame_);
    const auto first_samples = first_frames * channels_;
    const auto remaining_samples = sample_count - first_samples;
    for (const auto id : active_stems_) {
        const auto index = index_for(id);
        const auto& source = storage_.at(index);
        auto destination = by_id.at(index)->interleaved;
        std::copy_n(
            source.begin() + static_cast<std::ptrdiff_t>(read_frame_ * channels_),
            first_samples,
            destination.begin());
        if (remaining_samples > 0) {
            std::copy_n(
                source.begin(),
                remaining_samples,
                destination.begin() + static_cast<std::ptrdiff_t>(first_samples));
        }
    }
    read_frame_ = (read_frame_ + frame_count) % capacity_frames_;
    buffered_frames_ -= frame_count;
    total_read_frames_ += frame_count;
    minimum_buffered_frames_ = has_rendered_audio_
                                   ? (std::min)(minimum_buffered_frames_, buffered_frames_)
                                   : buffered_frames_;
    has_rendered_audio_ = true;
    return BufferReadState::audio;
}

StemBufferStats SynchronizedStemBuffer::stats() const {
    const std::scoped_lock lock{mutex_};
    return StemBufferStats{
        .buffered_frames = buffered_frames_,
        .capacity_frames = capacity_frames_,
        .prebuffer_frames = prebuffer_frames_,
        .minimum_buffered_frames = has_rendered_audio_ ? minimum_buffered_frames_ : 0,
        .total_written_frames = total_written_frames_,
        .total_read_frames = total_read_frames_,
        .underruns = underruns_,
        .last_underrun_system_time_ns = last_underrun_system_time_ns_,
        .last_underrun_buffered_frames = last_underrun_buffered_frames_,
        .last_underrun_total_read_frames = last_underrun_total_read_frames_,
        .prebuffering = !playing_,
    };
}

}  // namespace stemstudio
