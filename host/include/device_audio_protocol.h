#pragma once

#include "stem_mixer.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>

namespace stemstudio {

inline constexpr std::uint8_t device_protocol_version = 1;
inline constexpr std::size_t device_max_datagram_bytes = 1'400;
inline constexpr std::size_t device_audio_header_bytes = 40;
inline constexpr std::size_t device_control_packet_bytes = 48;
inline constexpr std::uint16_t device_unity_gain_q15 = 32'768;
inline constexpr std::uint16_t device_audio_flag_silence = 0x0001;

enum class DevicePacketError : std::uint8_t {
    none,
    too_short,
    wrong_magic,
    unsupported_version,
    wrong_packet_type,
    invalid_header,
    invalid_geometry,
    invalid_length,
    output_too_small,
    checksum_mismatch,
    invalid_control,
};

struct DeviceEncodeResult final {
    std::size_t bytes_written{0};
    DevicePacketError error{DevicePacketError::none};
};

struct DeviceAudioPacketHeader final {
    std::uint32_t session_id{0};
    std::uint32_t sequence{0};
    std::uint64_t presentation_frame{0};
    std::uint32_t sample_rate{44'100};
    std::uint8_t channels{2};
    std::uint16_t flags{0};
};

struct ParsedDeviceAudioPacket final {
    DeviceAudioPacketHeader header{};
    std::uint16_t frame_count{0};
    std::span<const std::byte> payload{};
};

struct DeviceAudioParseResult final {
    std::optional<ParsedDeviceAudioPacket> packet{};
    DevicePacketError error{DevicePacketError::none};
};

struct DeviceMixerCommand final {
    std::uint32_t session_id{0};
    std::uint32_t sequence{0};
    std::uint64_t issued_at_milliseconds{0};
    std::uint8_t valid_stem_mask{0};
    std::array<std::uint16_t, stem_id_count> gains_q15{};
};

struct DeviceMixerParseResult final {
    std::optional<DeviceMixerCommand> command{};
    DevicePacketError error{DevicePacketError::none};
};

[[nodiscard]] DeviceEncodeResult encode_device_audio_packet(
    const DeviceAudioPacketHeader& header,
    std::span<const std::int16_t> interleaved_pcm,
    std::span<std::byte> output) noexcept;

[[nodiscard]] DeviceAudioParseResult parse_device_audio_packet(
    std::span<const std::byte> packet) noexcept;

[[nodiscard]] DevicePacketError decode_device_pcm16(
    std::span<const std::byte> payload,
    std::span<std::int16_t> output) noexcept;

[[nodiscard]] DeviceEncodeResult encode_device_mixer_command(
    const DeviceMixerCommand& command,
    std::span<std::byte> output) noexcept;

[[nodiscard]] DeviceMixerParseResult parse_device_mixer_command(
    std::span<const std::byte> packet) noexcept;

}  // namespace stemstudio
