#include "stem_mixer.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace stemstudio {
namespace {
constexpr std::array<std::string_view, stem_id_count> stem_names{
    "vocals", "instrumental", "drums", "bass", "other", "guitar", "piano"};

[[nodiscard]] std::int16_t saturate_pcm16(const double sample) noexcept {
    constexpr auto minimum = static_cast<long>(std::numeric_limits<std::int16_t>::min());
    constexpr auto maximum = static_cast<long>(std::numeric_limits<std::int16_t>::max());
    const auto rounded = std::lround(sample);
    return static_cast<std::int16_t>(std::clamp(rounded, minimum, maximum));
}
}  // namespace

std::string_view stem_name(const StemId id) {
    const auto index = static_cast<std::size_t>(id);
    if (index >= stem_names.size()) {
        throw std::invalid_argument("unknown stem id");
    }
    return stem_names.at(index);
}

std::optional<StemId> stem_id_from_name(const std::string_view name) noexcept {
    const auto found = std::ranges::find(stem_names, name);
    if (found == stem_names.end()) {
        return std::nullopt;
    }
    return static_cast<StemId>(std::distance(stem_names.begin(), found));
}

std::span<const StemId> stems_for_profile(const std::size_t track_count) {
    static constexpr std::array two{StemId::vocals, StemId::instrumental};
    static constexpr std::array four{
        StemId::vocals, StemId::drums, StemId::bass, StemId::other};
    static constexpr std::array six{
        StemId::vocals,
        StemId::drums,
        StemId::bass,
        StemId::guitar,
        StemId::piano,
        StemId::other};
    switch (track_count) {
    case 2:
        return two;
    case 4:
        return four;
    case 6:
        return six;
    default:
        throw std::invalid_argument("track profile must contain 2, 4, or 6 stems");
    }
}

RealtimeStemMixer::RealtimeStemMixer(
    const std::size_t channels,
    const std::size_t smoothing_frames)
    : channels_{channels}, smoothing_frames_{smoothing_frames} {
    if (channels_ == 0) {
        throw std::invalid_argument("mixer channel count must be positive");
    }
}

std::size_t RealtimeStemMixer::index_for(const StemId id) {
    const auto index = static_cast<std::size_t>(id);
    if (index >= stem_id_count) {
        throw std::invalid_argument("unknown stem id");
    }
    return index;
}

void RealtimeStemMixer::set_gain(const StemId id, const float gain) {
    if (!std::isfinite(gain) || gain < 0.0F || gain > 1.0F) {
        throw std::invalid_argument("stem gain must be finite and between zero and one");
    }

    auto& state = gains_.at(index_for(id));
    state.target = gain;
    state.remaining_frames = smoothing_frames_;
    if (smoothing_frames_ == 0) {
        state.current = gain;
    }
}

float RealtimeStemMixer::current_gain(const StemId id) const {
    return gains_.at(index_for(id)).current;
}

float RealtimeStemMixer::target_gain(const StemId id) const {
    return gains_.at(index_for(id)).target;
}

float RealtimeStemMixer::advance_gain(GainState& state) const noexcept {
    if (state.remaining_frames == 0) {
        return state.current;
    }

    state.current += (state.target - state.current) /
                     static_cast<float>(state.remaining_frames);
    --state.remaining_frames;
    if (state.remaining_frames == 0) {
        state.current = state.target;
    }
    return state.current;
}

void RealtimeStemMixer::mix(
    const std::span<const StemBlockView> stems,
    const std::span<std::int16_t> output) {
    if (stems.empty()) {
        throw std::invalid_argument("at least one stem is required");
    }

    const auto sample_count = stems.front().interleaved.size();
    if (sample_count % channels_ != 0 || output.size() != sample_count) {
        throw std::invalid_argument("stem and output frame geometry mismatch");
    }

    std::array<bool, stem_id_count> seen{};
    for (const auto& stem : stems) {
        const auto index = index_for(stem.id);
        if (seen.at(index)) {
            throw std::invalid_argument("duplicate stem id");
        }
        seen.at(index) = true;
        if (stem.interleaved.size() != sample_count) {
            throw std::invalid_argument("all stem blocks must have the same length");
        }
    }

    const auto frame_count = sample_count / channels_;
    for (std::size_t frame = 0; frame < frame_count; ++frame) {
        std::array<float, stem_id_count> frame_gains{};
        for (std::size_t index = 0; index < gains_.size(); ++index) {
            frame_gains.at(index) = advance_gain(gains_.at(index));
        }

        for (std::size_t channel = 0; channel < channels_; ++channel) {
            const auto sample_index = frame * channels_ + channel;
            double mixed = 0.0;
            for (const auto& stem : stems) {
                mixed += static_cast<double>(stem.interleaved[sample_index]) *
                         static_cast<double>(frame_gains.at(index_for(stem.id)));
            }
            output[sample_index] = saturate_pcm16(mixed);
        }
    }
}

OverlapStitcher::OverlapStitcher(
    const std::size_t channels,
    const std::size_t hop_frames,
    const std::size_t overlap_frames)
    : channels_{channels},
      hop_frames_{hop_frames},
      overlap_frames_{overlap_frames},
      previous_tail_(channels * overlap_frames) {
    if (channels_ == 0 || hop_frames_ == 0 || overlap_frames_ == 0) {
        throw std::invalid_argument("overlap geometry values must be positive");
    }
    if (overlap_frames_ > hop_frames_) {
        throw std::invalid_argument("overlap cannot be longer than one hop");
    }
}

bool OverlapStitcher::push(
    const std::span<const std::int16_t> chunk,
    const std::span<std::int16_t> output_hop) {
    const auto hop_samples = hop_frames_ * channels_;
    const auto overlap_samples = overlap_frames_ * channels_;
    if (chunk.size() != hop_samples + overlap_samples || output_hop.size() != hop_samples) {
        throw std::invalid_argument("overlap chunk geometry mismatch");
    }

    const bool crossfaded = has_previous_;
    if (!crossfaded) {
        std::copy_n(chunk.begin(), hop_samples, output_hop.begin());
    } else {
        for (std::size_t frame = 0; frame < overlap_frames_; ++frame) {
            const double next_weight = overlap_frames_ == 1
                                           ? 0.5
                                           : static_cast<double>(frame) /
                                                 static_cast<double>(overlap_frames_ - 1);
            const double previous_weight = 1.0 - next_weight;
            for (std::size_t channel = 0; channel < channels_; ++channel) {
                const auto sample_index = frame * channels_ + channel;
                const double sample =
                    static_cast<double>(previous_tail_.at(sample_index)) * previous_weight +
                    static_cast<double>(chunk[sample_index]) * next_weight;
                output_hop[sample_index] = saturate_pcm16(sample);
            }
        }

        const auto non_overlap_samples = hop_samples - overlap_samples;
        std::copy_n(
            chunk.begin() + static_cast<std::ptrdiff_t>(overlap_samples),
            non_overlap_samples,
            output_hop.begin() + static_cast<std::ptrdiff_t>(overlap_samples));
    }

    std::copy_n(
        chunk.begin() + static_cast<std::ptrdiff_t>(hop_samples),
        overlap_samples,
        previous_tail_.begin());
    has_previous_ = true;
    return crossfaded;
}

void OverlapStitcher::reset() noexcept {
    has_previous_ = false;
}

}  // namespace stemstudio
