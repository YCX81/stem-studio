#pragma once

#include "audio_window_buffer.h"

#include <cstddef>
#include <filesystem>
#include <span>

namespace stemstudio {

void write_pcm16_wav_atomic(
    const std::filesystem::path& destination,
    const AudioGeometry& geometry,
    std::span<const std::byte> pcm);

}  // namespace stemstudio
