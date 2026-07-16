#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace stemstudio {

enum class AirPlayStreamEvent : std::uint8_t {
    receiver_started,
    connection_opened,
    pcm_received,
    flush_received,
    connection_closed,
    connection_reset,
    receiver_stopped,
};

[[nodiscard]] std::string_view airplay_state_for_event(
    AirPlayStreamEvent event,
    std::size_t open_connections) noexcept;

}  // namespace stemstudio
