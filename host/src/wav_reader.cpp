#include "wav_reader.h"

#include <array>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string_view>

namespace stemstudio {
namespace {
template <typename Value>
[[nodiscard]] Value read_value(std::ifstream& input) {
    Value value{};
    input.read(reinterpret_cast<char*>(&value), sizeof(value));
    if (!input) {
        throw std::runtime_error("truncated WAV file");
    }
    return value;
}

[[nodiscard]] std::array<char, 4> read_fourcc(std::ifstream& input) {
    std::array<char, 4> value{};
    input.read(value.data(), static_cast<std::streamsize>(value.size()));
    if (!input) {
        throw std::runtime_error("truncated WAV file");
    }
    return value;
}

[[nodiscard]] bool equals_fourcc(
    const std::array<char, 4>& value,
    const std::string_view expected) noexcept {
    return expected.size() == value.size() &&
           std::equal(value.begin(), value.end(), expected.begin());
}
}  // namespace

Pcm16Wave read_pcm16_wav(const std::filesystem::path& path) {
    const auto file_size = std::filesystem::file_size(path);
    if (file_size < 12) {
        throw std::runtime_error("truncated WAV header");
    }

    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("cannot open WAV file");
    }
    if (!equals_fourcc(read_fourcc(input), "RIFF")) {
        throw std::runtime_error("WAV file has no RIFF header");
    }
    const auto riff_size = read_value<std::uint32_t>(input);
    if (!equals_fourcc(read_fourcc(input), "WAVE")) {
        throw std::runtime_error("RIFF file is not WAVE audio");
    }
    const auto riff_end = static_cast<std::uint64_t>(riff_size) + 8;
    if (riff_size < 4 || riff_end > file_size) {
        throw std::runtime_error("invalid RIFF size");
    }

    bool found_format = false;
    bool found_data = false;
    std::uint16_t audio_format = 0;
    std::uint16_t channels = 0;
    std::uint32_t sample_rate = 0;
    std::uint16_t block_align = 0;
    std::uint16_t bits_per_sample = 0;
    std::vector<std::byte> pcm_bytes;

    while (static_cast<std::uint64_t>(input.tellg()) + 8 <= riff_end) {
        const auto chunk_id = read_fourcc(input);
        const auto chunk_size = read_value<std::uint32_t>(input);
        const auto payload_start = static_cast<std::uint64_t>(input.tellg());
        const auto padded_size = static_cast<std::uint64_t>(chunk_size) + (chunk_size & 1U);
        if (payload_start + padded_size > riff_end) {
            throw std::runtime_error("WAV chunk exceeds RIFF boundary");
        }

        if (equals_fourcc(chunk_id, "fmt ")) {
            if (found_format || chunk_size < 16) {
                throw std::runtime_error("invalid WAV format chunk");
            }
            audio_format = read_value<std::uint16_t>(input);
            channels = read_value<std::uint16_t>(input);
            sample_rate = read_value<std::uint32_t>(input);
            (void)read_value<std::uint32_t>(input);  // byte rate
            block_align = read_value<std::uint16_t>(input);
            bits_per_sample = read_value<std::uint16_t>(input);
            found_format = true;
        } else if (equals_fourcc(chunk_id, "data")) {
            if (found_data) {
                throw std::runtime_error("multiple WAV data chunks are unsupported");
            }
            pcm_bytes.resize(chunk_size);
            if (!pcm_bytes.empty()) {
                input.read(
                    reinterpret_cast<char*>(pcm_bytes.data()),
                    static_cast<std::streamsize>(pcm_bytes.size()));
                if (!input) {
                    throw std::runtime_error("truncated WAV PCM payload");
                }
            }
            found_data = true;
        }

        input.seekg(static_cast<std::streamoff>(payload_start + padded_size), std::ios::beg);
        if (!input) {
            throw std::runtime_error("cannot seek across WAV chunk");
        }
    }

    if (!found_format || !found_data) {
        throw std::runtime_error("WAV file is missing format or PCM data");
    }
    const auto expected_block_align = static_cast<std::uint32_t>(channels) * 2U;
    if (audio_format != 1 || channels == 0 || sample_rate == 0 || bits_per_sample != 16 ||
        block_align != expected_block_align || pcm_bytes.size() % block_align != 0) {
        throw std::runtime_error("WAV file must be interleaved PCM16 audio");
    }

    Pcm16Wave result{
        .sample_rate = sample_rate,
        .channels = channels,
        .interleaved = std::vector<std::int16_t>(pcm_bytes.size() / sizeof(std::int16_t)),
    };
    if (!pcm_bytes.empty()) {
        std::memcpy(result.interleaved.data(), pcm_bytes.data(), pcm_bytes.size());
    }
    return result;
}

}  // namespace stemstudio
