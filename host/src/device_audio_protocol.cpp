#include "device_audio_protocol.h"

#include <algorithm>
#include <array>
#include <bit>
#include <limits>

namespace stemstudio {
namespace {
constexpr std::array protocol_magic{
    std::byte{'S'}, std::byte{'S'}, std::byte{'N'}, std::byte{'P'}};
constexpr std::uint8_t audio_packet_type = 1;
constexpr std::uint8_t mixer_control_packet_type = 2;
constexpr std::uint8_t pcm_s16_le_format = 1;
constexpr std::size_t audio_checksum_offset = 36;
constexpr std::size_t control_checksum_offset = 44;
constexpr std::size_t checksum_bytes = 4;
constexpr std::size_t bytes_per_pcm16_sample = 2;
constexpr std::uint16_t known_audio_flags = device_audio_flag_silence;

[[nodiscard]] bool supported_sample_rate(const std::uint32_t sample_rate) noexcept {
    return sample_rate == 44'100U || sample_rate == 48'000U;
}

void write_u16(
    const std::span<std::byte> output,
    const std::size_t offset,
    const std::uint16_t value) noexcept {
    output[offset] = static_cast<std::byte>(value & 0xffU);
    output[offset + 1] = static_cast<std::byte>((value >> 8U) & 0xffU);
}

void write_u32(
    const std::span<std::byte> output,
    const std::size_t offset,
    const std::uint32_t value) noexcept {
    for (std::size_t index = 0; index < 4; ++index) {
        output[offset + index] =
            static_cast<std::byte>((value >> (index * 8U)) & 0xffU);
    }
}

void write_u64(
    const std::span<std::byte> output,
    const std::size_t offset,
    const std::uint64_t value) noexcept {
    for (std::size_t index = 0; index < 8; ++index) {
        output[offset + index] =
            static_cast<std::byte>((value >> (index * 8U)) & 0xffU);
    }
}

[[nodiscard]] std::uint16_t read_u16(
    const std::span<const std::byte> input,
    const std::size_t offset) noexcept {
    return static_cast<std::uint16_t>(std::to_integer<std::uint16_t>(input[offset])) |
           static_cast<std::uint16_t>(
               std::to_integer<std::uint16_t>(input[offset + 1]) << 8U);
}

[[nodiscard]] std::uint32_t read_u32(
    const std::span<const std::byte> input,
    const std::size_t offset) noexcept {
    std::uint32_t value{0};
    for (std::size_t index = 0; index < 4; ++index) {
        value |= std::to_integer<std::uint32_t>(input[offset + index]) << (index * 8U);
    }
    return value;
}

[[nodiscard]] std::uint64_t read_u64(
    const std::span<const std::byte> input,
    const std::size_t offset) noexcept {
    std::uint64_t value{0};
    for (std::size_t index = 0; index < 8; ++index) {
        value |= std::to_integer<std::uint64_t>(input[offset + index]) << (index * 8U);
    }
    return value;
}

[[nodiscard]] std::uint32_t packet_crc32(
    const std::span<const std::byte> packet,
    const std::size_t checksum_offset) noexcept {
    std::uint32_t crc{0xffffffffU};
    for (std::size_t index = 0; index < packet.size(); ++index) {
        const auto value = index >= checksum_offset && index < checksum_offset + checksum_bytes
                               ? std::uint8_t{0}
                               : std::to_integer<std::uint8_t>(packet[index]);
        crc ^= value;
        for (std::size_t bit = 0; bit < 8; ++bit) {
            const std::uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (0xedb88320U & mask);
        }
    }
    return crc ^ 0xffffffffU;
}

void write_common_header(
    const std::span<std::byte> output,
    const std::uint8_t packet_type,
    const std::uint16_t header_bytes) noexcept {
    std::ranges::copy(protocol_magic, output.begin());
    output[4] = static_cast<std::byte>(device_protocol_version);
    output[5] = static_cast<std::byte>(packet_type);
    write_u16(output, 6, header_bytes);
}

[[nodiscard]] DevicePacketError validate_common_header(
    const std::span<const std::byte> packet,
    const std::uint8_t expected_type,
    const std::uint16_t expected_header_bytes) noexcept {
    if (!std::ranges::equal(protocol_magic, packet.first(protocol_magic.size()))) {
        return DevicePacketError::wrong_magic;
    }
    if (std::to_integer<std::uint8_t>(packet[4]) != device_protocol_version) {
        return DevicePacketError::unsupported_version;
    }
    if (std::to_integer<std::uint8_t>(packet[5]) != expected_type) {
        return DevicePacketError::wrong_packet_type;
    }
    if (read_u16(packet, 6) != expected_header_bytes) {
        return DevicePacketError::invalid_header;
    }
    return DevicePacketError::none;
}

[[nodiscard]] bool valid_stem_mask(const std::uint8_t mask) noexcept {
    constexpr auto all_stems_mask = static_cast<std::uint8_t>((1U << stem_id_count) - 1U);
    return (mask & static_cast<std::uint8_t>(~all_stems_mask)) == 0;
}
}  // namespace

DeviceEncodeResult encode_device_audio_packet(
    const DeviceAudioPacketHeader& header,
    const std::span<const std::int16_t> interleaved_pcm,
    const std::span<std::byte> output) noexcept {
    if (header.channels != 2 || !supported_sample_rate(header.sample_rate) ||
        interleaved_pcm.empty() || interleaved_pcm.size() % header.channels != 0) {
        return {.error = DevicePacketError::invalid_geometry};
    }
    if ((header.flags & static_cast<std::uint16_t>(~known_audio_flags)) != 0) {
        return {.error = DevicePacketError::invalid_header};
    }

    const std::size_t frame_count = interleaved_pcm.size() / header.channels;
    const std::size_t payload_bytes = interleaved_pcm.size() * bytes_per_pcm16_sample;
    const std::size_t packet_bytes = device_audio_header_bytes + payload_bytes;
    if (frame_count > std::numeric_limits<std::uint16_t>::max() ||
        payload_bytes > std::numeric_limits<std::uint16_t>::max() ||
        packet_bytes > device_max_datagram_bytes) {
        return {.error = DevicePacketError::invalid_length};
    }
    if (output.size() < packet_bytes) {
        return {.error = DevicePacketError::output_too_small};
    }

    const auto packet = output.first(packet_bytes);
    std::ranges::fill(packet, std::byte{0});
    write_common_header(packet, audio_packet_type, device_audio_header_bytes);
    write_u32(packet, 8, header.session_id);
    write_u32(packet, 12, header.sequence);
    write_u64(packet, 16, header.presentation_frame);
    write_u32(packet, 24, header.sample_rate);
    write_u16(packet, 28, static_cast<std::uint16_t>(frame_count));
    packet[30] = static_cast<std::byte>(header.channels);
    packet[31] = static_cast<std::byte>(pcm_s16_le_format);
    write_u16(packet, 32, static_cast<std::uint16_t>(payload_bytes));
    write_u16(packet, 34, header.flags);

    for (std::size_t index = 0; index < interleaved_pcm.size(); ++index) {
        write_u16(
            packet,
            device_audio_header_bytes + index * bytes_per_pcm16_sample,
            static_cast<std::uint16_t>(interleaved_pcm[index]));
    }
    write_u32(packet, audio_checksum_offset, packet_crc32(packet, audio_checksum_offset));
    return {.bytes_written = packet_bytes};
}

DeviceAudioParseResult parse_device_audio_packet(
    const std::span<const std::byte> packet) noexcept {
    if (packet.size() < device_audio_header_bytes) {
        return {.error = DevicePacketError::too_short};
    }
    if (packet.size() > device_max_datagram_bytes) {
        return {.error = DevicePacketError::invalid_length};
    }
    if (const auto error = validate_common_header(
            packet, audio_packet_type, device_audio_header_bytes);
        error != DevicePacketError::none) {
        return {.error = error};
    }

    const auto sample_rate = read_u32(packet, 24);
    const auto frame_count = read_u16(packet, 28);
    const auto channels = std::to_integer<std::uint8_t>(packet[30]);
    const auto sample_format = std::to_integer<std::uint8_t>(packet[31]);
    const auto payload_bytes = read_u16(packet, 32);
    const auto flags = read_u16(packet, 34);
    if (channels != 2 || !supported_sample_rate(sample_rate) ||
        sample_format != pcm_s16_le_format || frame_count == 0) {
        return {.error = DevicePacketError::invalid_geometry};
    }
    const std::size_t expected_payload_bytes =
        static_cast<std::size_t>(frame_count) * channels * bytes_per_pcm16_sample;
    if ((flags & static_cast<std::uint16_t>(~known_audio_flags)) != 0) {
        return {.error = DevicePacketError::invalid_header};
    }
    if (payload_bytes != expected_payload_bytes ||
        packet.size() != device_audio_header_bytes + expected_payload_bytes) {
        return {.error = DevicePacketError::invalid_length};
    }
    if (read_u32(packet, audio_checksum_offset) !=
        packet_crc32(packet, audio_checksum_offset)) {
        return {.error = DevicePacketError::checksum_mismatch};
    }

    return {
        .packet = ParsedDeviceAudioPacket{
            .header = DeviceAudioPacketHeader{
                .session_id = read_u32(packet, 8),
                .sequence = read_u32(packet, 12),
                .presentation_frame = read_u64(packet, 16),
                .sample_rate = sample_rate,
                .channels = channels,
                .flags = flags,
            },
            .frame_count = frame_count,
            .payload = packet.subspan(device_audio_header_bytes, payload_bytes),
        },
    };
}

DevicePacketError decode_device_pcm16(
    const std::span<const std::byte> payload,
    const std::span<std::int16_t> output) noexcept {
    if (payload.size() != output.size() * bytes_per_pcm16_sample) {
        return DevicePacketError::invalid_length;
    }
    for (std::size_t index = 0; index < output.size(); ++index) {
        const auto encoded = read_u16(payload, index * bytes_per_pcm16_sample);
        const auto signed_value = encoded <= 0x7fffU
                                      ? static_cast<std::int32_t>(encoded)
                                      : static_cast<std::int32_t>(encoded) - 65'536;
        output[index] = static_cast<std::int16_t>(signed_value);
    }
    return DevicePacketError::none;
}

DeviceEncodeResult encode_device_mixer_command(
    const DeviceMixerCommand& command,
    const std::span<std::byte> output) noexcept {
    if (!valid_stem_mask(command.valid_stem_mask) ||
        std::ranges::any_of(command.gains_q15, [](const std::uint16_t gain) {
            return gain > device_unity_gain_q15;
        })) {
        return {.error = DevicePacketError::invalid_control};
    }
    if (output.size() < device_control_packet_bytes) {
        return {.error = DevicePacketError::output_too_small};
    }

    const auto packet = output.first(device_control_packet_bytes);
    std::ranges::fill(packet, std::byte{0});
    write_common_header(packet, mixer_control_packet_type, device_control_packet_bytes);
    write_u32(packet, 8, command.session_id);
    write_u32(packet, 12, command.sequence);
    write_u64(packet, 16, command.issued_at_milliseconds);
    packet[24] = static_cast<std::byte>(command.valid_stem_mask);
    for (std::size_t index = 0; index < command.gains_q15.size(); ++index) {
        write_u16(packet, 28 + index * 2, command.gains_q15[index]);
    }
    write_u32(packet, control_checksum_offset, packet_crc32(packet, control_checksum_offset));
    return {.bytes_written = device_control_packet_bytes};
}

DeviceMixerParseResult parse_device_mixer_command(
    const std::span<const std::byte> packet) noexcept {
    if (packet.size() < device_control_packet_bytes) {
        return {.error = DevicePacketError::too_short};
    }
    if (packet.size() != device_control_packet_bytes) {
        return {.error = DevicePacketError::invalid_length};
    }
    if (const auto error = validate_common_header(
            packet, mixer_control_packet_type, device_control_packet_bytes);
        error != DevicePacketError::none) {
        return {.error = error};
    }
    if (packet[25] != std::byte{0} || packet[26] != std::byte{0} ||
        packet[27] != std::byte{0} || read_u16(packet, 42) != 0) {
        return {.error = DevicePacketError::invalid_header};
    }
    if (read_u32(packet, control_checksum_offset) !=
        packet_crc32(packet, control_checksum_offset)) {
        return {.error = DevicePacketError::checksum_mismatch};
    }

    DeviceMixerCommand command{
        .session_id = read_u32(packet, 8),
        .sequence = read_u32(packet, 12),
        .issued_at_milliseconds = read_u64(packet, 16),
        .valid_stem_mask = std::to_integer<std::uint8_t>(packet[24]),
        .gains_q15 = {},
    };
    for (std::size_t index = 0; index < command.gains_q15.size(); ++index) {
        command.gains_q15[index] = read_u16(packet, 28 + index * 2);
    }
    if (!valid_stem_mask(command.valid_stem_mask) ||
        std::ranges::any_of(command.gains_q15, [](const std::uint16_t gain) {
            return gain > device_unity_gain_q15;
        })) {
        return {.error = DevicePacketError::invalid_control};
    }
    return {.command = command};
}

}  // namespace stemstudio
