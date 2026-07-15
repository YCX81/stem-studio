#include "stem_mixer.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

using stemstudio::OverlapStitcher;
using stemstudio::RealtimeStemMixer;
using stemstudio::StemBlockView;
using stemstudio::StemId;

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
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

void test_two_four_and_six_track_mix() {
    RealtimeStemMixer mixer{2, 0};

    const std::array<std::int16_t, 4> vocals{1'000, -1'000, 2'000, -2'000};
    const std::array<std::int16_t, 4> instrumental{500, -500, 1'000, -1'000};
    const std::array two_tracks{
        StemBlockView{StemId::vocals, vocals},
        StemBlockView{StemId::instrumental, instrumental},
    };
    std::array<std::int16_t, 4> output{};
    mixer.mix(two_tracks, output);
    require(output == std::array<std::int16_t, 4>{1'500, -1'500, 3'000, -3'000},
            "two-track samples must remain frame aligned");

    const std::array<std::int16_t, 4> drums{100, 100, 100, 100};
    const std::array<std::int16_t, 4> bass{200, 200, 200, 200};
    const std::array four_tracks{
        StemBlockView{StemId::vocals, vocals},
        StemBlockView{StemId::drums, drums},
        StemBlockView{StemId::bass, bass},
        StemBlockView{StemId::instrumental, instrumental},
    };
    mixer.mix(four_tracks, output);
    require(output == std::array<std::int16_t, 4>{1'800, -1'200, 3'300, -2'700},
            "four-track mix mismatch");

    const std::array<std::int16_t, 2> loud_positive{7'000, 7'000};
    const std::array<std::int16_t, 2> loud_negative{-7'000, -7'000};
    const std::array six_positive{
        StemBlockView{StemId::vocals, loud_positive},
        StemBlockView{StemId::drums, loud_positive},
        StemBlockView{StemId::bass, loud_positive},
        StemBlockView{StemId::guitar, loud_positive},
        StemBlockView{StemId::piano, loud_positive},
        StemBlockView{StemId::other, loud_positive},
    };
    const std::array six_negative{
        StemBlockView{StemId::vocals, loud_negative},
        StemBlockView{StemId::drums, loud_negative},
        StemBlockView{StemId::bass, loud_negative},
        StemBlockView{StemId::guitar, loud_negative},
        StemBlockView{StemId::piano, loud_negative},
        StemBlockView{StemId::other, loud_negative},
    };
    std::array<std::int16_t, 2> saturated{};
    mixer.mix(six_positive, saturated);
    require(saturated == std::array<std::int16_t, 2>{32'767, 32'767},
            "positive mix must saturate instead of wrapping");
    mixer.mix(six_negative, saturated);
    require(saturated == std::array<std::int16_t, 2>{-32'768, -32'768},
            "negative mix must saturate instead of wrapping");
}

void test_profile_stem_sets_are_explicit() {
    const auto two = stemstudio::stems_for_profile(2);
    require(two.size() == 2 && two[0] == StemId::vocals && two[1] == StemId::instrumental,
            "two-track profile mapping changed");
    const auto four = stemstudio::stems_for_profile(4);
    constexpr std::array expected_four{
        StemId::vocals, StemId::drums, StemId::bass, StemId::other};
    require(std::ranges::equal(four, expected_four),
            "four-track profile mapping changed");
    const auto six = stemstudio::stems_for_profile(6);
    constexpr std::array expected_six{
        StemId::vocals,
        StemId::drums,
        StemId::bass,
        StemId::guitar,
        StemId::piano,
        StemId::other};
    require(std::ranges::equal(six, expected_six),
            "six-track profile mapping changed");
    require(stemstudio::stem_name(StemId::guitar) == "guitar", "stem name mapping mismatch");
    require(stemstudio::stem_id_from_name("piano") == StemId::piano,
            "stem id mapping mismatch");
    require(!stemstudio::stem_id_from_name("unknown"), "unknown stem name must not be accepted");
    require_invalid_argument(
        [] { (void)stemstudio::stems_for_profile(3); },
        "only product profiles 2, 4, and 6 are valid");
}

void test_input_validation() {
    RealtimeStemMixer mixer{2, 0};
    const std::array<std::int16_t, 4> full{};
    const std::array<std::int16_t, 2> short_block{};
    std::array<std::int16_t, 4> output{};

    const std::array mismatched{
        StemBlockView{StemId::vocals, full},
        StemBlockView{StemId::drums, short_block},
    };
    require_invalid_argument(
        [&] { mixer.mix(mismatched, output); },
        "mismatched stem lengths must be rejected");

    const std::array duplicate{
        StemBlockView{StemId::vocals, full},
        StemBlockView{StemId::vocals, full},
    };
    require_invalid_argument(
        [&] { mixer.mix(duplicate, output); },
        "duplicate stems must be rejected");

    const std::array valid{StemBlockView{StemId::vocals, full}};
    std::array<std::int16_t, 2> undersized_output{};
    require_invalid_argument(
        [&] { mixer.mix(valid, undersized_output); },
        "output span must match the input frame count");
}

void test_gain_ramp_is_frame_based() {
    RealtimeStemMixer mixer{1, 4};
    mixer.set_gain(StemId::vocals, 0.0F);

    const std::array<std::int16_t, 2> first_input{10'000, 10'000};
    const std::array first_block{StemBlockView{StemId::vocals, first_input}};
    std::array<std::int16_t, 2> first_output{};
    mixer.mix(first_block, first_output);
    require(first_output == std::array<std::int16_t, 2>{7'500, 5'000},
            "gain must begin changing in the first rendered frame");

    const std::array<std::int16_t, 1> third_input{10'000};
    const std::array third_block{StemBlockView{StemId::vocals, third_input}};
    std::array<std::int16_t, 1> third_output{};
    mixer.mix(third_block, third_output);
    require(third_output.front() == 2'500,
            "gain ramp duration must not depend on block size");

    std::array<std::int16_t, 1> fourth_output{};
    mixer.mix(third_block, fourth_output);
    require(fourth_output.front() == 0, "gain must reach the exact target frame");
    require(mixer.current_gain(StemId::vocals) == 0.0F, "stored gain must equal target");

    std::array<std::int16_t, 1> steady_output{};
    mixer.mix(third_block, steady_output);
    require(steady_output.front() == 0, "finished gain ramp must remain stable");
}

void test_overlap_preserves_the_timeline() {
    OverlapStitcher stitcher{1, 4, 2};
    const std::array<std::int16_t, 6> first_chunk{0, 1, 2, 3, 4, 5};
    std::array<std::int16_t, 4> first_hop{};
    const bool first_crossfaded = stitcher.push(first_chunk, first_hop);
    require(!first_crossfaded, "the first chunk has no predecessor to crossfade");
    require(first_hop == std::array<std::int16_t, 4>{0, 1, 2, 3},
            "the first chunk must publish exactly one hop");

    const std::array<std::int16_t, 6> aligned_next_chunk{4, 5, 6, 7, 8, 9};
    std::array<std::int16_t, 4> second_hop{};
    const bool second_crossfaded = stitcher.push(aligned_next_chunk, second_hop);
    require(second_crossfaded, "subsequent chunks must crossfade their shared timeline");
    require(second_hop == std::array<std::int16_t, 4>{4, 5, 6, 7},
            "crossfade must neither duplicate nor skip aligned frames");

    const std::array<std::int16_t, 7> invalid_chunk{};
    require_invalid_argument(
        [&] { (void)stitcher.push(invalid_chunk, second_hop); },
        "partial or oversized overlap chunks must be rejected");
}

void test_overlap_crossfade_is_linear() {
    OverlapStitcher stitcher{1, 4, 3};
    const std::array<std::int16_t, 7> first_chunk{0, 0, 0, 0, 0, 0, 0};
    std::array<std::int16_t, 4> output{};
    (void)stitcher.push(first_chunk, output);

    const std::array<std::int16_t, 7> next_chunk{10'000, 10'000, 10'000, 10'000, 0, 0, 0};
    (void)stitcher.push(next_chunk, output);
    require(output == std::array<std::int16_t, 4>{0, 5'000, 10'000, 10'000},
            "overlap endpoints and midpoint must form a linear crossfade");

    stitcher.reset();
    require(!stitcher.has_previous(), "reset must start a new playback session");
}
}  // namespace

int main() {
    test_two_four_and_six_track_mix();
    test_profile_stem_sets_are_explicit();
    test_input_validation();
    test_gain_ramp_is_frame_based();
    test_overlap_preserves_the_timeline();
    test_overlap_crossfade_is_linear();
    return 0;
}
