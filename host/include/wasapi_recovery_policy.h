#pragma once

#include <chrono>
#include <cstdint>

namespace stemstudio {

[[nodiscard]] bool should_retry_wasapi_failure(std::int32_t hresult) noexcept;
[[nodiscard]] std::chrono::milliseconds wasapi_retry_delay(
    std::uint32_t consecutive_failures) noexcept;

}  // namespace stemstudio
