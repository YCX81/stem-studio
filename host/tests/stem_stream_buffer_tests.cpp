#include "stem_stream_buffer.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

using stemstudio::BufferReadState;
using stemstudio::MutableStemBlockView;
using stemstudio::RealtimeStemMixer;
using stemstudio::StemBlockView;
using stemstudio::StemId;
using stemstudio::SynchronizedStemBuffer;
using stemstudio::default_live_buffer_capacity_hops;
using stemstudio::default_live_prebuffer_hops;

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        std::cerr << message << '\n';
        throw std::runtime_error(std::string{message});
    }
}

template <typename Function>
void require_invalid_argument(Function&& function, const std::string_view message) {
    bool rejected = false;
    try {
        function();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, message);
}

void test_two_hop_prebuffer_and_recovery() {
    const std::array active{StemId::vocals, StemId::instrumental};
    SynchronizedStemBuffer buffer{1, active, 16, 8};

    const std::array<std::int16_t, 4> vocals_first{0, 1, 2, 3};
    const std::array<std::int16_t, 4> instrumental_first{100, 101, 102, 103};
    const std::array first_hop{
        StemBlockView{StemId::vocals, vocals_first},
        StemBlockView{StemId::instrumental, instrumental_first},
    };
    require(buffer.try_push(first_hop), "first hop must fit");

    std::array<std::int16_t, 2> vocals_output{9, 9};
    std::array<std::int16_t, 2> instrumental_output{9, 9};
    const std::array output{
        MutableStemBlockView{StemId::vocals, vocals_output},
        MutableStemBlockView{StemId::instrumental, instrumental_output},
    };
    require(buffer.pop(output) == BufferReadState::prebuffering,
            "one hop must not start a two-hop buffer");
    require(vocals_output == std::array<std::int16_t, 2>{0, 0},
            "prebuffering output must be explicit silence");
    require(buffer.stats().buffered_frames == 4, "prebuffering must not consume audio");

    const std::array<std::int16_t, 4> vocals_second{4, 5, 6, 7};
    const std::array<std::int16_t, 4> instrumental_second{104, 105, 106, 107};
    const std::array second_hop{
        StemBlockView{StemId::vocals, vocals_second},
        StemBlockView{StemId::instrumental, instrumental_second},
    };
    require(buffer.try_push(second_hop), "second hop must fit");
    require(buffer.pop(output) == BufferReadState::audio,
            "two complete hops must release playback");
    require(vocals_output == std::array<std::int16_t, 2>{0, 1},
            "vocal timeline mismatch after prebuffer");
    require(instrumental_output == std::array<std::int16_t, 2>{100, 101},
            "stem timelines must remain synchronized");

    std::array<std::int16_t, 6> vocals_remaining{};
    std::array<std::int16_t, 6> instrumental_remaining{};
    const std::array remaining_output{
        MutableStemBlockView{StemId::vocals, vocals_remaining},
        MutableStemBlockView{StemId::instrumental, instrumental_remaining},
    };
    require(buffer.pop(remaining_output) == BufferReadState::audio,
            "remaining queued frames must play continuously");
    require(vocals_remaining == std::array<std::int16_t, 6>{2, 3, 4, 5, 6, 7},
            "ring buffer changed the frame order");

    vocals_output.fill(9);
    require(buffer.pop(output) == BufferReadState::underrun,
            "an empty active queue must report an underrun");
    const auto underrun_stats = buffer.stats();
    require(underrun_stats.underruns == 1, "first underrun must be counted");
    require(underrun_stats.last_underrun_system_time_ns > 0,
            "underrun must retain an observable wall-clock timestamp");
    require(underrun_stats.last_underrun_buffered_frames == 0,
            "underrun telemetry must retain the depleted queue depth");
    require(underrun_stats.last_underrun_total_read_frames == 8,
            "underrun telemetry must retain the consumer timeline position");
    require(vocals_output == std::array<std::int16_t, 2>{0, 0},
            "underrun output must be silence");
    require(buffer.pop(output) == BufferReadState::prebuffering,
            "one underrun episode must transition to rebuffering");
    require(buffer.stats().underruns == 1,
            "rebuffer polling must not count the same underrun repeatedly");
    require(buffer.stats().last_underrun_system_time_ns ==
                underrun_stats.last_underrun_system_time_ns,
            "rebuffer polling must not rewrite the underrun event timestamp");

    require(buffer.try_push(first_hop), "recovery hop one must fit");
    require(buffer.try_push(second_hop), "recovery hop two must fit");
    require(buffer.pop(output) == BufferReadState::audio,
            "playback must resume automatically after rebuffering");
}

void test_default_three_hop_prebuffer_survives_one_hop_source_gap() {
    constexpr std::size_t hop_frames = 4;
    require(default_live_buffer_capacity_hops == 4,
            "live buffer must retain four hops of capacity");
    require(default_live_prebuffer_hops == 3,
            "live playback must hold three hops before starting");
    const std::array active{StemId::vocals};
    SynchronizedStemBuffer buffer{
        1,
        active,
        hop_frames * default_live_buffer_capacity_hops,
        hop_frames * default_live_prebuffer_hops,
    };
    const std::array<std::int16_t, hop_frames> hop_one{1, 2, 3, 4};
    const std::array<std::int16_t, hop_frames> hop_two{5, 6, 7, 8};
    const std::array<std::int16_t, hop_frames> hop_three{9, 10, 11, 12};
    const std::array<std::int16_t, hop_frames> hop_four{13, 14, 15, 16};
    std::array<std::int16_t, hop_frames> rendered{};
    const std::array output{MutableStemBlockView{StemId::vocals, rendered}};

    require(buffer.try_push(std::array{StemBlockView{StemId::vocals, hop_one}}),
            "first live hop must fit");
    require(buffer.pop(output) == BufferReadState::prebuffering,
            "one hop must not start the default live buffer");
    require(buffer.try_push(std::array{StemBlockView{StemId::vocals, hop_two}}),
            "second live hop must fit");
    require(buffer.pop(output) == BufferReadState::prebuffering,
            "two hops must remain buffered for cross-track protection");
    require(buffer.try_push(std::array{StemBlockView{StemId::vocals, hop_three}}),
            "third live hop must fit");
    require(buffer.pop(output) == BufferReadState::audio,
            "three hops must release live playback");
    require(buffer.pop(output) == BufferReadState::audio,
            "one missing producer hop must be absorbed without an underrun");
    require(buffer.try_push(std::array{StemBlockView{StemId::vocals, hop_four}}),
            "producer must resume after the protected gap");
    require(buffer.pop(output) == BufferReadState::audio,
            "playback must remain continuous when the producer resumes");
    require(buffer.stats().underruns == 0,
            "one-hop AirPlay source gap must not increment underruns");
}

void test_six_stems_wrap_without_drift() {
    const std::array active{
        StemId::vocals,
        StemId::drums,
        StemId::bass,
        StemId::guitar,
        StemId::piano,
        StemId::other,
    };
    SynchronizedStemBuffer buffer{2, active, 5, 2};

    std::array<std::array<std::int16_t, 6>, 6> source{};
    std::array<StemBlockView, 6> input{};
    for (std::size_t stem = 0; stem < active.size(); ++stem) {
        for (std::size_t sample = 0; sample < source.at(stem).size(); ++sample) {
            source.at(stem).at(sample) = static_cast<std::int16_t>(stem * 1'000 + sample);
        }
        input.at(stem) = StemBlockView{active.at(stem), source.at(stem)};
    }
    require(buffer.try_push(input), "six synchronized stems must fit");

    std::array<std::array<std::int16_t, 4>, 6> first_output{};
    std::array<MutableStemBlockView, 6> first_views{};
    for (std::size_t stem = 0; stem < active.size(); ++stem) {
        first_views.at(stem) = MutableStemBlockView{active.at(stem), first_output.at(stem)};
    }
    require(buffer.pop(first_views) == BufferReadState::audio, "six-stem playback must start");

    std::array<std::array<std::int16_t, 2>, 6> second_output{};
    std::array<MutableStemBlockView, 6> second_views{};
    for (std::size_t stem = 0; stem < active.size(); ++stem) {
        second_views.at(stem) = MutableStemBlockView{active.at(stem), second_output.at(stem)};
    }
    require(buffer.pop(second_views) == BufferReadState::audio, "wrapped tail must remain readable");
    for (std::size_t stem = 0; stem < active.size(); ++stem) {
        require(second_output.at(stem) == std::array<std::int16_t, 2>{
                    static_cast<std::int16_t>(stem * 1'000 + 4),
                    static_cast<std::int16_t>(stem * 1'000 + 5)},
                "a stem drifted relative to the shared read cursor");
    }

    require(buffer.try_push(input), "wrapped six-stem write must fit after reads");
}

void test_validation_and_capacity_are_atomic() {
    const std::array active{StemId::vocals, StemId::drums};
    SynchronizedStemBuffer buffer{1, active, 4, 2};
    const std::array<std::int16_t, 3> full{1, 2, 3};
    const std::array<std::int16_t, 2> short_block{1, 2};
    const std::array mismatched{
        StemBlockView{StemId::vocals, full},
        StemBlockView{StemId::drums, short_block},
    };
    require_invalid_argument(
        [&] { (void)buffer.try_push(mismatched); },
        "mismatched stem lengths must be rejected before writing");
    require(buffer.stats().buffered_frames == 0, "a rejected push must be atomic");

    const std::array valid{
        StemBlockView{StemId::vocals, full},
        StemBlockView{StemId::drums, full},
    };
    require(buffer.try_push(valid), "valid frames must fit");
    require(!buffer.try_push(valid), "capacity exhaustion must be reported without overwriting");
    require(buffer.stats().buffered_frames == 3, "failed capacity check must preserve queued frames");

    const std::array duplicate_active{StemId::vocals, StemId::vocals};
    require_invalid_argument(
        [&] { SynchronizedStemBuffer invalid{1, duplicate_active, 4, 2}; },
        "active stem configuration must not contain duplicates");
}

void test_scaled_thirty_minute_six_stem_mix_with_gain_changes_has_no_underrun() {
    constexpr std::size_t sample_rate = 100;
    constexpr std::size_t channels = 2;
    constexpr std::size_t hop_frames = sample_rate * 6;
    constexpr std::size_t render_frames = sample_rate / 50;
    constexpr std::size_t render_samples = render_frames * channels;
    constexpr std::size_t hop_count = 30 * 60 / 6;
    const std::array active{
        StemId::vocals,
        StemId::drums,
        StemId::bass,
        StemId::guitar,
        StemId::piano,
        StemId::other,
    };
    constexpr std::array gain_patterns{
        std::array{1.0F, 0.8F, 0.6F, 0.4F, 0.2F, 0.0F},
        std::array{0.0F, 0.2F, 0.4F, 0.6F, 0.8F, 1.0F},
        std::array{0.5F, 0.5F, 0.5F, 0.5F, 0.5F, 0.5F},
        std::array{1.0F, 0.0F, 1.0F, 0.0F, 1.0F, 0.0F},
    };
    SynchronizedStemBuffer buffer{channels, active, hop_frames * 4, hop_frames * 2};
    RealtimeStemMixer mixer{channels, render_frames};

    std::array<std::vector<std::int16_t>, 6> source;
    std::array<StemBlockView, 6> input;
    std::array<std::array<std::int16_t, render_samples>, 6> rendered{};
    std::array<MutableStemBlockView, 6> output;
    std::array<StemBlockView, 6> mixer_input;
    std::array<std::int16_t, render_samples> mixed{};
    for (std::size_t stem = 0; stem < active.size(); ++stem) {
        source.at(stem).resize(
            hop_frames * channels,
            static_cast<std::int16_t>((stem + 1) * 500));
        input.at(stem) = StemBlockView{active.at(stem), source.at(stem)};
        output.at(stem) = MutableStemBlockView{active.at(stem), rendered.at(stem)};
        mixer_input.at(stem) = StemBlockView{active.at(stem), rendered.at(stem)};
    }

    std::size_t rendered_frame_count = 0;
    std::size_t gain_update_count = 0;
    std::uint64_t mixed_checksum = 0;
    const auto render_hop = [&] {
        for (std::size_t frame = 0; frame < hop_frames; frame += render_frames) {
            const bool update_gains = rendered_frame_count % sample_rate == 0;
            const auto& pattern = gain_patterns.at(
                (rendered_frame_count / sample_rate) % gain_patterns.size());
            if (update_gains) {
                for (std::size_t stem = 0; stem < active.size(); ++stem) {
                    mixer.set_gain(active.at(stem), pattern.at(stem));
                    ++gain_update_count;
                }
            }
            require(buffer.pop(output) == BufferReadState::audio,
                    "steady six-stem playback must not enter rebuffering");
            mixer.mix(mixer_input, mixed);
            require(std::ranges::all_of(mixed, [](const std::int16_t sample) {
                        return sample > 0;
                    }),
                    "live gain changes must keep producing mixed PCM");
            if (update_gains) {
                for (std::size_t stem = 0; stem < active.size(); ++stem) {
                    require(mixer.current_gain(active.at(stem)) == pattern.at(stem),
                            "20 ms smoothing must reach every requested gain without pausing");
                }
            }
            for (const auto sample : mixed) {
                mixed_checksum += static_cast<std::uint64_t>(sample);
            }
            rendered_frame_count += render_frames;
        }
    };

    for (std::size_t hop = 0; hop < hop_count; ++hop) {
        require(buffer.try_push(input), "steady producer hop must fit the four-hop queue");
        if (hop == 0) {
            continue;
        }
        render_hop();
    }
    render_hop();

    const auto stats = buffer.stats();
    require(stats.total_read_frames == sample_rate * 30 * 60,
            "scaled long-run simulation must preserve the full 30-minute frame clock");
    require(rendered_frame_count == sample_rate * 30 * 60,
            "mixer must render the same full 30-minute frame clock");
    require(gain_update_count == 30 * 60 * active.size(),
            "all six live gains must update once per simulated second");
    require(mixed_checksum > 0, "30-minute mixed output must contain audio");
    require(stats.underruns == 0, "steady 30-minute simulation must not underrun");
}
}  // namespace

int main() {
    test_two_hop_prebuffer_and_recovery();
    test_default_three_hop_prebuffer_survives_one_hop_source_gap();
    test_six_stems_wrap_without_drift();
    test_validation_and_capacity_are_atomic();
    test_scaled_thirty_minute_six_stem_mix_with_gain_changes_has_no_underrun();
    return 0;
}
