#include "wasapi_recovery_policy.h"

#include <chrono>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string{message});
    }
}

constexpr std::int32_t as_hresult(const std::uint32_t value) noexcept {
    return static_cast<std::int32_t>(value);
}

void test_retries_endpoint_and_audio_service_failures() {
    require(
        stemstudio::should_retry_wasapi_failure(as_hresult(0x88890004U)),
        "device invalidation must reopen the default endpoint");
    require(
        stemstudio::should_retry_wasapi_failure(as_hresult(0x88890010U)),
        "stopped audio service must be retried");
    require(
        stemstudio::should_retry_wasapi_failure(as_hresult(0x80070490U)),
        "temporarily missing default endpoint must be retried");
}

void test_does_not_hide_configuration_or_programming_failures() {
    require(
        !stemstudio::should_retry_wasapi_failure(as_hresult(0x88890008U)),
        "unsupported PCM format must remain fatal");
    require(
        !stemstudio::should_retry_wasapi_failure(as_hresult(0x80070057U)),
        "invalid arguments must remain fatal");
}

void test_retry_delay_is_bounded() {
    using namespace std::chrono_literals;
    require(stemstudio::wasapi_retry_delay(0) == 100ms, "first retry delay mismatch");
    require(stemstudio::wasapi_retry_delay(1) == 200ms, "second retry delay mismatch");
    require(stemstudio::wasapi_retry_delay(2) == 400ms, "third retry delay mismatch");
    require(stemstudio::wasapi_retry_delay(20) == 1000ms, "retry delay must be capped");
}
}  // namespace

int main() {
    test_retries_endpoint_and_audio_service_failures();
    test_does_not_hide_configuration_or_programming_failures();
    test_retry_delay_is_bounded();
    return 0;
}
