#pragma once

#include <chrono>
#include <cstddef>
#include <filesystem>

namespace stemstudio {

void atomic_replace_file(
    const std::filesystem::path& source,
    const std::filesystem::path& destination,
    std::size_t maximum_attempts = 50,
    std::chrono::milliseconds retry_delay = std::chrono::milliseconds{2});

}  // namespace stemstudio
