#include "airplay_track_state.h"

#include <optional>
#include <sstream>
#include <stdexcept>

namespace stemstudio {
namespace {
constexpr double airplay_sample_rate = 44'100.0;
constexpr std::uint64_t metadata_anchor_update_frames = 2U * 44'100U;

double rtp_seconds(const std::uint32_t start, const std::uint32_t value) noexcept {
    const auto samples = static_cast<std::uint32_t>(value - start);
    return static_cast<double>(samples) / airplay_sample_rate;
}

std::string json_escape(const std::string_view value) {
    constexpr char hexadecimal[] = "0123456789abcdef";
    std::string escaped;
    escaped.reserve(value.size());
    for (const unsigned char character : value) {
        switch (character) {
        case '\\': escaped += "\\\\"; break;
        case '"': escaped += "\\\""; break;
        case '\b': escaped += "\\b"; break;
        case '\f': escaped += "\\f"; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default:
            if (character < 0x20U) {
                escaped += "\\u00";
                escaped.push_back(hexadecimal[(character >> 4U) & 0x0FU]);
                escaped.push_back(hexadecimal[character & 0x0FU]);
            } else {
                escaped.push_back(static_cast<char>(character));
            }
            break;
        }
    }
    return escaped;
}

void append_track_json(std::ostringstream& output, const AirPlayTrackSnapshot& track) {
    const auto position_frame = static_cast<std::uint32_t>(track.current_rtp - track.start_rtp);
    const auto duration_frame = static_cast<std::uint32_t>(track.end_rtp - track.start_rtp);
    output << "{\"revision\":" << track.revision
           << ",\"metadata_revision\":" << track.metadata_revision
           << ",\"has_progress\":" << (track.has_progress ? "true" : "false")
           << ",\"start_rtp\":" << track.start_rtp
           << ",\"current_rtp\":" << track.current_rtp
           << ",\"end_rtp\":" << track.end_rtp
           << ",\"anchor_stream_frame\":" << track.anchor_stream_frame
           << ",\"track_position_frame\":" << position_frame
           << ",\"track_duration_frame\":" << duration_frame
           << ",\"title\":\"" << json_escape(track.title)
           << "\",\"artist\":\"" << json_escape(track.artist)
           << "\",\"album\":\"" << json_escape(track.album) << "\"}";
}
}  // namespace

double AirPlayTrackSnapshot::position_seconds() const noexcept {
    return has_progress ? rtp_seconds(start_rtp, current_rtp) : 0.0;
}

double AirPlayTrackSnapshot::position_seconds_at(
    const std::uint64_t stream_frame) const noexcept {
    if (!has_progress) {
        return 0.0;
    }
    const auto anchor_position = static_cast<std::uint32_t>(current_rtp - start_rtp);
    const auto duration = static_cast<std::uint32_t>(end_rtp - start_rtp);
    const auto elapsed = stream_frame > anchor_stream_frame
        ? stream_frame - anchor_stream_frame
        : 0;
    const auto live_position = static_cast<std::uint64_t>(anchor_position) + elapsed;
    const auto bounded_position = live_position < duration ? live_position : duration;
    return static_cast<double>(bounded_position) / airplay_sample_rate;
}

double AirPlayTrackSnapshot::duration_seconds() const noexcept {
    return has_progress ? rtp_seconds(start_rtp, end_rtp) : 0.0;
}

bool AirPlayTrackState::set_metadata(
    const std::string_view dmap_tag,
    const std::string_view value,
    const std::uint64_t stream_frame) {
    std::scoped_lock lock(mutex_);
    std::string* destination = nullptr;
    if (dmap_tag == "minm") {
        destination = &state_.title;
    } else if (dmap_tag == "asar") {
        destination = &state_.artist;
    } else if (dmap_tag == "asal") {
        destination = &state_.album;
    } else {
        return false;
    }
    if (*destination != value) {
        destination->assign(value);
        ++state_.metadata_revision;
        bool update_current_track_anchors = stream_frame == 0;
        if (!update_current_track_anchors && progress_received_ && !anchors_.empty()) {
            const auto& latest = anchors_.back();
            const auto anchor_position =
                static_cast<std::uint32_t>(latest.current_rtp - latest.start_rtp);
            const auto duration =
                static_cast<std::uint32_t>(latest.end_rtp - latest.start_rtp);
            const auto elapsed = stream_frame > latest.anchor_stream_frame
                ? stream_frame - latest.anchor_stream_frame
                : 0;
            const auto projected_position =
                static_cast<std::uint64_t>(anchor_position) + elapsed;
            const auto bounded_position =
                projected_position < duration ? projected_position : duration;
            update_current_track_anchors =
                bounded_position <= metadata_anchor_update_frames;
        }
        if (update_current_track_anchors) {
            for (auto& anchor : anchors_) {
                if (anchor.revision == state_.revision) {
                    anchor.title = state_.title;
                    anchor.artist = state_.artist;
                    anchor.album = state_.album;
                    anchor.metadata_revision = state_.metadata_revision;
                }
            }
        }
    }
    return true;
}

bool AirPlayTrackState::update_progress(
    const std::uint32_t start_rtp,
    const std::uint32_t current_rtp,
    const std::uint32_t end_rtp,
    const std::uint64_t stream_frame) {
    std::scoped_lock lock(mutex_);
    const bool new_track =
        !progress_received_ || state_.start_rtp != start_rtp || state_.end_rtp != end_rtp;
    if (new_track) {
        ++state_.revision;
    }
    progress_received_ = true;
    state_.has_progress = start_rtp != end_rtp;
    state_.start_rtp = start_rtp;
    state_.current_rtp = current_rtp;
    state_.end_rtp = end_rtp;
    state_.anchor_stream_frame = stream_frame;
    anchors_.push_back(state_);
    constexpr std::size_t maximum_anchor_count = 4'096;
    if (anchors_.size() > maximum_anchor_count) {
        anchors_.erase(anchors_.begin(), anchors_.begin() + static_cast<std::ptrdiff_t>(anchors_.size() - maximum_anchor_count));
    }
    return new_track;
}

AirPlayTrackSnapshot AirPlayTrackState::snapshot() const {
    std::scoped_lock lock(mutex_);
    return state_;
}

std::vector<AirPlayTrackSnapshot> AirPlayTrackState::anchors_for_window(
    const std::uint64_t stream_start_frame,
    const std::uint64_t stream_end_frame) const {
    if (stream_end_frame < stream_start_frame) {
        throw std::invalid_argument("AirPlay window frame range is reversed");
    }
    std::scoped_lock lock(mutex_);
    std::optional<std::size_t> preceding_index;
    for (std::size_t index = 0; index < anchors_.size(); ++index) {
        if (anchors_[index].anchor_stream_frame <= stream_start_frame) {
            preceding_index = index;
        } else {
            break;
        }
    }

    std::vector<AirPlayTrackSnapshot> result;
    if (preceding_index) {
        result.push_back(anchors_[*preceding_index]);
    }
    for (const auto& anchor : anchors_) {
        if (anchor.anchor_stream_frame <= stream_start_frame ||
            anchor.anchor_stream_frame > stream_end_frame) {
            continue;
        }
        result.push_back(anchor);
    }
    return result;
}

std::string serialize_airplay_capture_annotation(
    const std::uint64_t sequence,
    const std::uint64_t stream_start_frame,
    const std::uint64_t stream_end_frame,
    const AirPlayTrackSnapshot& current_track,
    const std::span<const AirPlayTrackSnapshot> anchors) {
    if (sequence == 0 || stream_end_frame <= stream_start_frame) {
        throw std::invalid_argument("invalid AirPlay capture annotation range");
    }
    std::ostringstream output;
    output << "{\"version\":1,\"source\":\"airplay\""
           << ",\"sequence\":" << sequence
           << ",\"sample_rate\":44100"
           << ",\"stream_start_frame\":" << stream_start_frame
           << ",\"stream_end_frame\":" << stream_end_frame
           << ",\"track\":";
    append_track_json(output, current_track);
    output << ",\"anchors\":[";
    for (std::size_t index = 0; index < anchors.size(); ++index) {
        if (index != 0) {
            output << ',';
        }
        append_track_json(output, anchors[index]);
    }
    output << "]}";
    return output.str();
}

}  // namespace stemstudio
