#include "device_udp_sender.h"

#include <WinSock2.h>
#include <WS2tcpip.h>

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>

namespace stemstudio {
namespace {
[[nodiscard]] bool valid_ipv4_address(const std::wstring_view address) noexcept {
    std::size_t octet_count = 0;
    std::size_t offset = 0;
    while (offset < address.size()) {
        if (octet_count == 4) {
            return false;
        }
        std::uint32_t value = 0;
        std::size_t digit_count = 0;
        const std::size_t octet_start = offset;
        while (offset < address.size() && address[offset] != L'.') {
            const wchar_t character = address[offset++];
            if (character < L'0' || character > L'9' || digit_count == 3) {
                return false;
            }
            value = value * 10U + static_cast<std::uint32_t>(character - L'0');
            ++digit_count;
        }
        if (digit_count == 0 || value > 255U ||
            (digit_count > 1 && address[octet_start] == L'0')) {
            return false;
        }
        ++octet_count;
        if (offset < address.size()) {
            ++offset;
            if (offset == address.size()) {
                return false;
            }
        }
    }
    return octet_count == 4;
}

class WinsockSession final {
public:
    WinsockSession() {
        WSADATA data{};
        const int result = WSAStartup(MAKEWORD(2, 2), &data);
        if (result != 0) {
            throw std::runtime_error("WSAStartup failed for device audio sender");
        }
    }
    ~WinsockSession() { WSACleanup(); }
    WinsockSession(const WinsockSession&) = delete;
    WinsockSession& operator=(const WinsockSession&) = delete;
};

class SocketHandle final {
public:
    SocketHandle() : value_{socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)} {
        if (value_ == INVALID_SOCKET) {
            throw std::runtime_error("device audio UDP socket creation failed");
        }
    }
    ~SocketHandle() { closesocket(value_); }
    SocketHandle(const SocketHandle&) = delete;
    SocketHandle& operator=(const SocketHandle&) = delete;
    [[nodiscard]] SOCKET get() const noexcept { return value_; }

private:
    SOCKET value_;
};
}  // namespace

DeviceUdpEndpoint parse_device_udp_endpoint(const std::wstring_view value) {
    const auto separator = value.rfind(L':');
    if (separator == std::wstring_view::npos || separator == 0 ||
        separator + 1 >= value.size() || value.find(L':') != separator) {
        throw std::invalid_argument("device endpoint must be IPv4:port");
    }
    const auto address = value.substr(0, separator);
    const auto port_text = value.substr(separator + 1);
    if (!valid_ipv4_address(address)) {
        throw std::invalid_argument("device endpoint contains an invalid IPv4 address");
    }
    std::uint32_t port = 0;
    for (const wchar_t character : port_text) {
        if (character < L'0' || character > L'9') {
            throw std::invalid_argument("device endpoint contains an invalid UDP port");
        }
        port = port * 10U + static_cast<std::uint32_t>(character - L'0');
        if (port > std::numeric_limits<std::uint16_t>::max()) {
            throw std::invalid_argument("device endpoint UDP port is out of range");
        }
    }
    if (port == 0) {
        throw std::invalid_argument("device endpoint UDP port must be non-zero");
    }
    return {
        .ipv4_address = std::wstring{address},
        .port = static_cast<std::uint16_t>(port),
    };
}

class DeviceUdpSender::Impl final {
public:
    Impl(
        const std::wstring_view ipv4_address,
        const std::uint16_t port,
        DeviceAudioPacketQueue& queue)
        : queue_{queue} {
        if (port == 0) {
            throw std::invalid_argument("device audio UDP port must be non-zero");
        }
        sockaddr_in destination{};
        destination.sin_family = AF_INET;
        destination.sin_port = htons(port);
        const std::wstring address{ipv4_address};
        const int parsed = InetPtonW(AF_INET, address.c_str(), &destination.sin_addr);
        if (parsed != 1) {
            throw std::invalid_argument("device audio destination must be an IPv4 address");
        }
        if (connect(
                socket_.get(),
                reinterpret_cast<const sockaddr*>(&destination),
                sizeof(destination)) != 0) {
            throw std::runtime_error("device audio UDP connect failed");
        }
        constexpr int send_buffer_bytes = 256 * 1024;
        if (setsockopt(
                socket_.get(),
                SOL_SOCKET,
                SO_SNDBUF,
                reinterpret_cast<const char*>(&send_buffer_bytes),
                sizeof(send_buffer_bytes)) != 0) {
            throw std::runtime_error("device audio UDP send buffer setup failed");
        }
    }

    [[nodiscard]] DeviceSendResult send_next() noexcept {
        const auto popped = queue_.try_pop(wire_);
        if (!popped.packet_available) {
            return DeviceSendResult::empty;
        }
        if (popped.error != DevicePacketError::none) {
            send_errors_.fetch_add(1, std::memory_order_relaxed);
            last_socket_error_.store(WSAEMSGSIZE, std::memory_order_relaxed);
            return DeviceSendResult::error;
        }
        const int sent = send(
            socket_.get(),
            reinterpret_cast<const char*>(wire_.data()),
            static_cast<int>(popped.bytes_read),
            0);
        if (sent == SOCKET_ERROR || sent != static_cast<int>(popped.bytes_read)) {
            send_errors_.fetch_add(1, std::memory_order_relaxed);
            last_socket_error_.store(
                sent == SOCKET_ERROR ? WSAGetLastError() : WSAEMSGSIZE,
                std::memory_order_relaxed);
            return DeviceSendResult::error;
        }
        sent_packets_.fetch_add(1, std::memory_order_relaxed);
        sent_bytes_.fetch_add(popped.bytes_read, std::memory_order_relaxed);
        return DeviceSendResult::sent;
    }

    void run(const std::atomic_bool& stop_requested) noexcept {
        using namespace std::chrono_literals;
        while (!stop_requested.load(std::memory_order_acquire)) {
            const auto result = send_next();
            if (result == DeviceSendResult::empty) {
                std::this_thread::sleep_for(1ms);
            } else if (result == DeviceSendResult::error) {
                std::this_thread::sleep_for(5ms);
            }
        }
    }

    [[nodiscard]] DeviceUdpSenderStats stats() const noexcept {
        return {
            .sent_packets = sent_packets_.load(std::memory_order_relaxed),
            .sent_bytes = sent_bytes_.load(std::memory_order_relaxed),
            .send_errors = send_errors_.load(std::memory_order_relaxed),
            .last_socket_error = last_socket_error_.load(std::memory_order_relaxed),
        };
    }

private:
    WinsockSession winsock_;
    SocketHandle socket_;
    DeviceAudioPacketQueue& queue_;
    std::array<std::byte, device_max_datagram_bytes> wire_{};
    std::atomic<std::uint64_t> sent_packets_{0};
    std::atomic<std::uint64_t> sent_bytes_{0};
    std::atomic<std::uint64_t> send_errors_{0};
    std::atomic<std::int32_t> last_socket_error_{0};
};

DeviceUdpSender::DeviceUdpSender(
    const std::wstring_view ipv4_address,
    const std::uint16_t port,
    DeviceAudioPacketQueue& queue)
    : impl_{std::make_unique<Impl>(ipv4_address, port, queue)} {}

DeviceUdpSender::~DeviceUdpSender() = default;

DeviceSendResult DeviceUdpSender::send_next() noexcept {
    return impl_->send_next();
}

void DeviceUdpSender::run(const std::atomic_bool& stop_requested) noexcept {
    impl_->run(stop_requested);
}

DeviceUdpSenderStats DeviceUdpSender::stats() const noexcept {
    return impl_->stats();
}

}  // namespace stemstudio
