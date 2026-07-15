#include "wav_reader.h"
#include "wav_writer.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <format>
#include <fstream>
#include <iostream>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
void require(const bool condition, const std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string{message});
    }
}

class TemporaryDirectory final {
public:
    TemporaryDirectory() {
        const auto suffix = std::chrono::steady_clock::now().time_since_epoch().count();
        path_ = std::filesystem::temp_directory_path() / std::format("stem-wav-tests-{}", suffix);
        std::filesystem::create_directories(path_);
    }
    ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};

void test_writer_reader_round_trip() {
    TemporaryDirectory temporary;
    const auto path = temporary.path() / "roundtrip.wav";
    const stemstudio::AudioGeometry geometry{44'100, 2, 16, 8, 8};
    const std::vector<std::int16_t> samples{
        0, 1,
        -1, 32'767,
        -32'768, 1'234,
    };
    stemstudio::write_pcm16_wav_atomic(path, geometry, std::as_bytes(std::span{samples}));

    const auto wave = stemstudio::read_pcm16_wav(path);
    require(wave.sample_rate == 44'100, "sample rate mismatch");
    require(wave.channels == 2, "channel count mismatch");
    require(wave.frames() == 3, "frame count mismatch");
    require(wave.interleaved == samples, "PCM payload changed during file loading");
}

void test_rejects_truncated_or_non_pcm_input() {
    TemporaryDirectory temporary;
    const auto truncated = temporary.path() / "truncated.wav";
    std::ofstream{truncated, std::ios::binary}.write("RIFF", 4);

    bool rejected = false;
    try {
        (void)stemstudio::read_pcm16_wav(truncated);
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    require(rejected, "truncated WAV must be rejected");
}
}  // namespace

int main() {
    try {
        test_writer_reader_round_trip();
        test_rejects_truncated_or_non_pcm_input();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
