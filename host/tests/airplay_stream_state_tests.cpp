#include "airplay_stream_state.h"

#include <stdexcept>
#include <string>
#include <string_view>

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string{message});
    }
}

void test_pcm_and_lifecycle_events_publish_truthful_states() {
    using stemstudio::AirPlayStreamEvent;
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::receiver_started, 0) ==
                "waiting",
            "a ready receiver must wait for a phone");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::connection_opened, 1) ==
                "connected",
            "an admitted phone must be visible before PCM starts");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::pcm_received, 1) ==
                "streaming",
            "decoded PCM must mark active streaming");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::flush_received, 1) ==
                "paused",
            "AirPlay flush must not leave stale streaming state");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::connection_reset, 1) ==
                "recovering",
            "network reset must expose recovery state");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::receiver_stopped, 0) ==
                "stopped",
            "shutdown must expose stopped state");
}

void test_connection_close_distinguishes_waiting_from_remaining_session() {
    using stemstudio::AirPlayStreamEvent;
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::connection_closed, 0) ==
                "waiting",
            "last client close must return receiver to waiting");
    require(stemstudio::airplay_state_for_event(AirPlayStreamEvent::connection_closed, 1) ==
                "connected",
            "remaining client connection must not look disconnected");
}
}  // namespace

int main() {
    test_pcm_and_lifecycle_events_publish_truthful_states();
    test_connection_close_distinguishes_waiting_from_remaining_session();
    return 0;
}
