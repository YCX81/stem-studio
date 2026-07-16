#include "device_udp_sender.h"

#include <WinSock2.h>
#include <WS2tcpip.h>

#include <array>
#include <cstddef>
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

class Winsock final {
public:
    Winsock() {
        WSADATA data{};
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
            throw std::runtime_error("WSAStartup failed in UDP test");
        }
    }
    ~Winsock() { WSACleanup(); }
    Winsock(const Winsock&) = delete;
    Winsock& operator=(const Winsock&) = delete;
};

class Socket final {
public:
    explicit Socket(const SOCKET handle) : handle_{handle} {
        if (handle_ == INVALID_SOCKET) {
            throw std::runtime_error("socket creation failed in UDP test");
        }
    }
    ~Socket() { closesocket(handle_); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    [[nodiscard]] SOCKET get() const noexcept { return handle_; }

private:
    SOCKET handle_;
};

void test_sender_delivers_protocol_datagram_to_real_loopback_socket() {
    const Winsock winsock;
    const Socket receiver{socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)};
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    require(
        bind(receiver.get(), reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0,
        "loopback receiver bind failed");
    int address_size = sizeof(address);
    require(
        getsockname(
            receiver.get(), reinterpret_cast<sockaddr*>(&address), &address_size) == 0,
        "loopback receiver port lookup failed");
    const DWORD timeout_ms = 1'000;
    require(
        setsockopt(
            receiver.get(),
            SOL_SOCKET,
            SO_RCVTIMEO,
            reinterpret_cast<const char*>(&timeout_ms),
            sizeof(timeout_ms)) == 0,
        "loopback receive timeout setup failed");

    stemstudio::DeviceAudioPacketQueue queue{55, 44'100, 2, 4, 220};
    constexpr std::array<std::int16_t, 8> pcm{1, -1, 2, -2, 3, -3, 4, -4};
    require(queue.try_submit(pcm, false), "UDP fixture queue submit failed");
    stemstudio::DeviceUdpSender sender{
        L"127.0.0.1", ntohs(address.sin_port), queue};

    require(sender.send_next() == stemstudio::DeviceSendResult::sent,
            "sender did not transmit queued packet");
    std::array<std::byte, stemstudio::device_max_datagram_bytes> wire{};
    const int received = recv(
        receiver.get(),
        reinterpret_cast<char*>(wire.data()),
        static_cast<int>(wire.size()),
        0);
    require(received > 0, "loopback receiver did not get UDP packet");
    const auto parsed = stemstudio::parse_device_audio_packet(
        std::span<const std::byte>{wire}.first(static_cast<std::size_t>(received)));
    require(parsed.packet.has_value(), "received UDP packet failed protocol parsing");
    require(parsed.packet->header.session_id == 55, "UDP session id changed");
    require(parsed.packet->frame_count == 4, "UDP frame count changed");
    require(sender.send_next() == stemstudio::DeviceSendResult::empty,
            "empty queue must not send a datagram");
    const auto stats = sender.stats();
    require(stats.sent_packets == 1, "UDP sent counter mismatch");
    require(stats.send_errors == 0, "successful UDP send reported an error");
}

void test_sender_rejects_invalid_ipv4_destination() {
    const Winsock winsock;
    stemstudio::DeviceAudioPacketQueue queue{1, 44'100, 2, 2, 220};
    bool rejected = false;
    try {
        stemstudio::DeviceUdpSender sender{L"not-an-ip", 40'100, queue};
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "invalid IPv4 destination must be rejected");
}

void test_device_endpoint_parser_requires_ipv4_and_valid_port() {
    const auto endpoint = stemstudio::parse_device_udp_endpoint(L"192.168.31.88:40100");
    require(endpoint.ipv4_address == L"192.168.31.88", "endpoint IPv4 changed");
    require(endpoint.port == 40'100, "endpoint port changed");

    for (const std::wstring_view invalid : {
             L"192.168.31.88", L"192.168.31.88:0", L"192.168.31.88:65536",
             L"not-an-ip:40100", L"192.168.31.88:abc", L"192.168.031.88:40100"}) {
        bool rejected = false;
        try {
            (void)stemstudio::parse_device_udp_endpoint(invalid);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected, "invalid endpoint must be rejected");
    }
}
}  // namespace

int main() {
    test_sender_delivers_protocol_datagram_to_real_loopback_socket();
    test_sender_rejects_invalid_ipv4_destination();
    test_device_endpoint_parser_requires_ipv4_and_valid_port();
    return 0;
}
