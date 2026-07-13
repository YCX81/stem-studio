#include "wav_writer.h"

#include <array>
#include <fstream>
#include <stdexcept>

namespace stemstudio {
namespace {

template <typename T>
void write_value(std::ofstream& stream, const T value) {
    stream.write(reinterpret_cast<const char*>(&value), sizeof(value));
}

}  // namespace

void write_pcm16_wav_atomic(
    const std::filesystem::path& destination,
    const AudioGeometry& geometry,
    const std::span<const std::byte> pcm) {
    geometry.validate();
    if (pcm.size() > 0xFFFF'FF00ULL || pcm.size() % geometry.bytes_per_frame() != 0) {
        throw std::invalid_argument("invalid PCM payload size");
    }
    std::filesystem::create_directories(destination.parent_path());
    auto partial = destination;
    partial += L".part";
    std::ofstream output(partial, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error("cannot create WAV file");
    }
    const auto data_size = static_cast<std::uint32_t>(pcm.size());
    const auto riff_size = 36U + data_size;
    const auto byte_rate = geometry.sample_rate * static_cast<std::uint32_t>(geometry.bytes_per_frame());
    const auto block_align = static_cast<std::uint16_t>(geometry.bytes_per_frame());
    output.write("RIFF", 4);
    write_value(output, riff_size);
    output.write("WAVEfmt ", 8);
    write_value(output, std::uint32_t{16});
    write_value(output, std::uint16_t{1});
    write_value(output, geometry.channels);
    write_value(output, geometry.sample_rate);
    write_value(output, byte_rate);
    write_value(output, block_align);
    write_value(output, geometry.bits_per_sample);
    output.write("data", 4);
    write_value(output, data_size);
    output.write(reinterpret_cast<const char*>(pcm.data()), static_cast<std::streamsize>(pcm.size()));
    output.close();
    if (!output) {
        throw std::runtime_error("failed to finish WAV file");
    }
    std::error_code ignored;
    std::filesystem::remove(destination, ignored);
    std::filesystem::rename(partial, destination);
}

}  // namespace stemstudio
