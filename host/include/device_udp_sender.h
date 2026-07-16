#pragma once

#include "device_audio_queue.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

namespace stemstudio {

enum class DeviceSendResult : std::uint8_t {
    empty,
    sent,
    error,
};

struct DeviceUdpSenderStats final {
    std::uint64_t sent_packets{0};
    std::uint64_t sent_bytes{0};
    std::uint64_t send_errors{0};
    std::int32_t last_socket_error{0};
};

struct DeviceUdpEndpoint final {
    std::wstring ipv4_address;
    std::uint16_t port{0};
};

[[nodiscard]] DeviceUdpEndpoint parse_device_udp_endpoint(std::wstring_view value);

class DeviceUdpSender final {
public:
    DeviceUdpSender(
        std::wstring_view ipv4_address,
        std::uint16_t port,
        DeviceAudioPacketQueue& queue);
    ~DeviceUdpSender();

    DeviceUdpSender(const DeviceUdpSender&) = delete;
    DeviceUdpSender& operator=(const DeviceUdpSender&) = delete;
    DeviceUdpSender(DeviceUdpSender&&) = delete;
    DeviceUdpSender& operator=(DeviceUdpSender&&) = delete;

    [[nodiscard]] DeviceSendResult send_next() noexcept;
    void run(const std::atomic_bool& stop_requested) noexcept;
    [[nodiscard]] DeviceUdpSenderStats stats() const noexcept;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace stemstudio
