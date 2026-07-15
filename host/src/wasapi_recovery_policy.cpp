#include "wasapi_recovery_policy.h"

#include <Windows.h>
#include <audioclient.h>

#include <algorithm>
#include <chrono>
#include <cstdint>

namespace stemstudio {

bool should_retry_wasapi_failure(const std::int32_t hresult) noexcept {
    const auto result = static_cast<HRESULT>(hresult);
    return result == AUDCLNT_E_DEVICE_INVALIDATED ||
           result == AUDCLNT_E_SERVICE_NOT_RUNNING ||
           result == AUDCLNT_E_RESOURCES_INVALIDATED ||
           result == AUDCLNT_E_ENDPOINT_CREATE_FAILED ||
           result == AUDCLNT_E_DEVICE_IN_USE ||
           result == AUDCLNT_E_BUFFER_ERROR ||
           result == HRESULT_FROM_WIN32(ERROR_NOT_FOUND) ||
           result == HRESULT_FROM_WIN32(ERROR_SERVICE_NOT_ACTIVE);
}

std::chrono::milliseconds wasapi_retry_delay(
    const std::uint32_t consecutive_failures) noexcept {
    constexpr std::uint32_t maximum_shift = 4;
    const auto shift = std::min(consecutive_failures, maximum_shift);
    const auto delay = std::min<std::uint32_t>(100U << shift, 1'000U);
    return std::chrono::milliseconds{delay};
}

}  // namespace stemstudio
