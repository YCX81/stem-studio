#include "airplay_track_state.h"

#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string{message});
    }
}

void test_exports_metadata_independently_from_cover_art() {
    stemstudio::AirPlayTrackState state;
    require(state.set_metadata("minm", "Song Title"), "title tag must be accepted");
    require(state.set_metadata("asar", "Artist"), "artist tag must be accepted");
    require(state.set_metadata("asal", "Album"), "album tag must be accepted");
    require(!state.set_metadata("asgn", "Genre"), "untracked metadata must be ignored");

    const auto snapshot = state.snapshot();
    require(snapshot.title == "Song Title", "title was not retained");
    require(snapshot.artist == "Artist", "artist was not retained");
    require(snapshot.album == "Album", "album was not retained");
    require(snapshot.metadata_revision == 3, "metadata revision mismatch");
}

void test_detects_track_change_and_reports_progress() {
    stemstudio::AirPlayTrackState state;
    require(state.update_progress(100, 441'100, 882'100, 50'000), "first progress must start a track");
    auto snapshot = state.snapshot();
    require(snapshot.revision == 1, "first track revision mismatch");
    require(snapshot.has_progress, "valid progress must be reported");
    require(std::abs(snapshot.position_seconds() - 10.0) < 0.001, "position conversion mismatch");
    require(
        std::abs(snapshot.position_seconds_at(94'100) - 11.0) < 0.001,
        "live position must advance from the stream anchor");
    require(
        std::abs(snapshot.position_seconds_at(1'000'000) - 20.0) < 0.001,
        "live position must not advance beyond the track duration");
    require(std::abs(snapshot.duration_seconds() - 20.0) < 0.001, "duration conversion mismatch");
    require(snapshot.anchor_stream_frame == 50'000, "progress stream anchor mismatch");

    require(!state.update_progress(100, 485'200, 882'100, 94'100), "ordinary progress must retain the track");
    require(state.snapshot().revision == 1, "ordinary progress changed track revision");
    require(state.update_progress(900, 900, 442'800, 100'000), "new RTP bounds must start a new track");
    require(state.snapshot().revision == 2, "new track revision mismatch");
}

void test_returns_window_timeline_with_preceding_anchor_and_track_changes() {
    stemstudio::AirPlayTrackState state;
    state.update_progress(100, 100, 1'000, 10);
    state.set_metadata("minm", "First");
    state.update_progress(100, 200, 1'000, 20);
    state.update_progress(2'000, 2'000, 3'000, 30);
    state.set_metadata("minm", "Second");
    state.update_progress(2'000, 2'100, 3'000, 40);

    const auto anchors = state.anchors_for_window(25, 35);
    require(anchors.size() == 2, "window timeline must contain preceding and in-window anchors");
    require(anchors.front().anchor_stream_frame == 20, "preceding anchor mismatch");
    require(anchors.front().revision == 1, "preceding track revision mismatch");
    require(anchors.back().anchor_stream_frame == 30, "track boundary anchor mismatch");
    require(anchors.back().revision == 2, "track boundary revision mismatch");
    require(anchors.back().title == "Second", "metadata must update the current boundary anchor");
}

void test_upcoming_metadata_does_not_rewrite_a_nearly_finished_track() {
    stemstudio::AirPlayTrackState state;
    state.update_progress(100, 100, 200'100, 10);
    state.set_metadata("minm", "First", 10);
    state.update_progress(100, 190'100, 200'100, 190'010);

    state.set_metadata("minm", "Upcoming", 195'000);
    const auto old_anchors = state.anchors_for_window(190'000, 195'050);
    require(!old_anchors.empty(), "finished-track anchors are missing");
    require(
        old_anchors.back().title == "First",
        "upcoming metadata rewrote the previous track anchor");

    state.update_progress(300'000, 300'000, 400'000, 195'100);
    const auto new_anchors = state.anchors_for_window(195'050, 195'150);
    require(!new_anchors.empty(), "upcoming-track anchor is missing");
    require(
        new_anchors.back().title == "Upcoming",
        "new progress boundary did not adopt pending metadata");
}

void test_progress_math_handles_rtp_wraparound() {
    stemstudio::AirPlayTrackState state;
    constexpr auto start = std::numeric_limits<std::uint32_t>::max() - 44'099U;
    const auto current = static_cast<std::uint32_t>(start + 44'100U);
    const auto end = static_cast<std::uint32_t>(start + 88'200U);
    state.update_progress(start, current, end);

    const auto snapshot = state.snapshot();
    require(std::abs(snapshot.position_seconds() - 1.0) < 0.001, "wrapped position mismatch");
    require(std::abs(snapshot.duration_seconds() - 2.0) < 0.001, "wrapped duration mismatch");
}

void test_capture_annotation_serializes_timeline_as_valid_json_shape() {
    stemstudio::AirPlayTrackState state;
    state.update_progress(1'000, 45'100, 89'200, 123'000);
    state.set_metadata("minm", "A \"quoted\" title\nline");
    state.set_metadata("asar", "Artist");
    const auto snapshot = state.snapshot();
    const auto anchors = state.anchors_for_window(120'000, 130'000);

    const auto json = stemstudio::serialize_airplay_capture_annotation(
        17,
        120'000,
        130'000,
        snapshot,
        anchors);
    require(json.starts_with("{\"version\":1,\"source\":\"airplay\""), "annotation JSON prefix mismatch");
    require(json.find("\"sequence\":17") != std::string::npos, "annotation sequence missing");
    require(json.find("\"stream_start_frame\":120000") != std::string::npos, "annotation start missing");
    require(json.find("\"track_position_frame\":44100") != std::string::npos, "annotation track position missing");
    require(json.find("A \\\"quoted\\\" title\\nline") != std::string::npos, "annotation text was not escaped");
    require(json.find("\"anchors\":[{") != std::string::npos, "annotation anchors missing");
    require(json.ends_with("]}"), "annotation JSON suffix mismatch");
}
}  // namespace

int main() {
    test_exports_metadata_independently_from_cover_art();
    test_detects_track_change_and_reports_progress();
    test_returns_window_timeline_with_preceding_anchor_and_track_changes();
    test_upcoming_metadata_does_not_rewrite_a_nearly_finished_track();
    test_progress_math_handles_rtp_wraparound();
    test_capture_annotation_serializes_timeline_as_valid_json_shape();
    return 0;
}
