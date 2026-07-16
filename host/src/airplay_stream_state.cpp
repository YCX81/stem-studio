#include "airplay_stream_state.h"

namespace stemstudio {

std::string_view airplay_state_for_event(
    const AirPlayStreamEvent event,
    const std::size_t open_connections) noexcept {
    switch (event) {
    case AirPlayStreamEvent::receiver_started:
        return "waiting";
    case AirPlayStreamEvent::connection_opened:
        return "connected";
    case AirPlayStreamEvent::pcm_received:
        return "streaming";
    case AirPlayStreamEvent::flush_received:
        return "paused";
    case AirPlayStreamEvent::connection_closed:
        return open_connections == 0 ? "waiting" : "connected";
    case AirPlayStreamEvent::connection_reset:
        return "recovering";
    case AirPlayStreamEvent::receiver_stopped:
        return "stopped";
    }
    return "error";
}

}  // namespace stemstudio
