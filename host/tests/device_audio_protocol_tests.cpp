#include "device_audio_protocol.h"

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

void test_audio_packet_round_trip_has_explicit_wire_layout() {
    constexpr stemstudio::DeviceAudioPacketHeader header{
        .session_id = 0x11223344U,
        .sequence = 0x55667788U,
        .presentation_frame = 0x0102030405060708ULL,
        .sample_rate = 44'100U,
        .channels = 2,
    };
    constexpr std::array<std::int16_t, 4> pcm{0x1234, -2, 32'767, -32'768};
    std::array<std::byte, stemstudio::device_max_datagram_bytes> wire{};

    const auto encoded = stemstudio::encode_device_audio_packet(header, pcm, wire);
    require(encoded.error == stemstudio::DevicePacketError::none, "audio encode failed");
    require(
        encoded.bytes_written == stemstudio::device_audio_header_bytes + pcm.size() * 2,
        "audio wire size mismatch");
    require(wire[0] == std::byte{'S'} && wire[1] == std::byte{'S'} &&
                wire[2] == std::byte{'N'} && wire[3] == std::byte{'P'},
            "protocol magic mismatch");
    require(wire[4] == std::byte{1} && wire[5] == std::byte{1},
            "protocol version or packet type mismatch");
    require(wire[8] == std::byte{0x44} && wire[11] == std::byte{0x11},
            "session id must use little-endian wire order");
    require(wire[16] == std::byte{0x08} && wire[23] == std::byte{0x01},
            "presentation frame must use little-endian wire order");
    require(wire[40] == std::byte{0x34} && wire[41] == std::byte{0x12} &&
                wire[42] == std::byte{0xfe} && wire[43] == std::byte{0xff},
            "PCM16 payload must use little-endian wire order");

    const auto decoded = stemstudio::parse_device_audio_packet(
        std::span<const std::byte>{wire}.first(encoded.bytes_written));
    require(decoded.error == stemstudio::DevicePacketError::none, "audio parse failed");
    require(decoded.packet.has_value(), "parsed audio packet missing");
    require(decoded.packet->header.session_id == header.session_id, "session id changed");
    require(decoded.packet->header.sequence == header.sequence, "sequence changed");
    require(decoded.packet->header.presentation_frame == header.presentation_frame,
            "presentation frame changed");
    require(decoded.packet->frame_count == 2, "audio frame count mismatch");

    std::array<std::int16_t, pcm.size()> restored{};
    const auto pcm_error = stemstudio::decode_device_pcm16(decoded.packet->payload, restored);
    require(pcm_error == stemstudio::DevicePacketError::none, "PCM decode failed");
    require(restored == pcm, "PCM payload must be bit exact");
}

void test_audio_packet_rejects_corruption_and_unsafe_geometry() {
    constexpr stemstudio::DeviceAudioPacketHeader header{
        .session_id = 3,
        .sequence = 4,
        .presentation_frame = 960,
        .sample_rate = 48'000,
        .channels = 2,
    };
    constexpr std::array<std::int16_t, 4> pcm{1, 2, 3, 4};
    std::array<std::byte, stemstudio::device_max_datagram_bytes> wire{};
    const auto encoded = stemstudio::encode_device_audio_packet(header, pcm, wire);
    require(encoded.error == stemstudio::DevicePacketError::none, "fixture encode failed");

    wire[encoded.bytes_written - 1] ^= std::byte{0x01};
    const auto corrupted = stemstudio::parse_device_audio_packet(
        std::span<const std::byte>{wire}.first(encoded.bytes_written));
    require(corrupted.error == stemstudio::DevicePacketError::checksum_mismatch,
            "corrupted audio must fail CRC validation");

    auto invalid_header = header;
    invalid_header.channels = 1;
    require(
        stemstudio::encode_device_audio_packet(invalid_header, pcm, wire).error ==
            stemstudio::DevicePacketError::invalid_geometry,
        "v1 device audio must reject non-stereo PCM");

    invalid_header = header;
    invalid_header.sample_rate = 96'000;
    require(
        stemstudio::encode_device_audio_packet(invalid_header, pcm, wire).error ==
            stemstudio::DevicePacketError::invalid_geometry,
        "v1 device audio must reject unsupported sample rates");

    std::array<std::byte, 20> short_output{};
    require(
        stemstudio::encode_device_audio_packet(header, pcm, short_output).error ==
            stemstudio::DevicePacketError::output_too_small,
        "encoder must not overrun a short datagram buffer");

    const auto truncated = stemstudio::parse_device_audio_packet(
        std::span<const std::byte>{wire}.first(stemstudio::device_audio_header_bytes - 1));
    require(truncated.error == stemstudio::DevicePacketError::too_short,
            "truncated audio header must be rejected");
}

void test_mixer_command_round_trip_preserves_all_stem_controls() {
    stemstudio::DeviceMixerCommand command{
        .session_id = 7,
        .sequence = 99,
        .issued_at_milliseconds = 12'345,
        .valid_stem_mask = 0b01111101,
        .gains_q15 = {32'768, 0, 16'384, 8'192, 24'576, 4'096, 32'000},
    };
    std::array<std::byte, stemstudio::device_control_packet_bytes> wire{};

    const auto encoded = stemstudio::encode_device_mixer_command(command, wire);
    require(encoded.error == stemstudio::DevicePacketError::none, "control encode failed");
    require(encoded.bytes_written == wire.size(), "control packet size must be fixed");
    require(wire[5] == std::byte{2}, "control packet type mismatch");

    const auto decoded = stemstudio::parse_device_mixer_command(wire);
    require(decoded.error == stemstudio::DevicePacketError::none, "control parse failed");
    require(decoded.command.has_value(), "parsed control command missing");
    require(decoded.command->session_id == command.session_id, "control session changed");
    require(decoded.command->sequence == command.sequence, "control sequence changed");
    require(decoded.command->issued_at_milliseconds == command.issued_at_milliseconds,
            "control timestamp changed");
    require(decoded.command->valid_stem_mask == command.valid_stem_mask,
            "control stem mask changed");
    require(decoded.command->gains_q15 == command.gains_q15, "control gains changed");
}

void test_mixer_command_rejects_invalid_mask_gain_and_checksum() {
    stemstudio::DeviceMixerCommand command{
        .session_id = 7,
        .sequence = 100,
        .issued_at_milliseconds = 12'346,
        .valid_stem_mask = 0x80,
        .gains_q15 = {},
    };
    std::array<std::byte, stemstudio::device_control_packet_bytes> wire{};
    require(
        stemstudio::encode_device_mixer_command(command, wire).error ==
            stemstudio::DevicePacketError::invalid_control,
        "unknown stem-mask bits must be rejected");

    command.valid_stem_mask = 1;
    command.gains_q15[0] = 32'769;
    require(
        stemstudio::encode_device_mixer_command(command, wire).error ==
            stemstudio::DevicePacketError::invalid_control,
        "gain above unity must be rejected");

    command.gains_q15[0] = 32'768;
    const auto encoded = stemstudio::encode_device_mixer_command(command, wire);
    require(encoded.error == stemstudio::DevicePacketError::none, "fixture control encode failed");
    wire[20] ^= std::byte{0x40};
    require(
        stemstudio::parse_device_mixer_command(wire).error ==
            stemstudio::DevicePacketError::checksum_mismatch,
        "corrupted control command must fail CRC validation");
}
}  // namespace

int main() {
    test_audio_packet_round_trip_has_explicit_wire_layout();
    test_audio_packet_rejects_corruption_and_unsafe_geometry();
    test_mixer_command_round_trip_preserves_all_stem_controls();
    test_mixer_command_rejects_invalid_mask_gain_and_checksum();
    return 0;
}
