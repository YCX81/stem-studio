#include "atomic_file.h"

#include <Windows.h>

#include <stdexcept>
#include <system_error>
#include <thread>

namespace stemstudio {
namespace {
[[nodiscard]] bool is_transient_replace_error(const DWORD error) noexcept {
    return error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION ||
           error == ERROR_LOCK_VIOLATION;
}
}  // namespace

void atomic_replace_file(
    const std::filesystem::path& source,
    const std::filesystem::path& destination,
    const std::size_t maximum_attempts,
    const std::chrono::milliseconds retry_delay) {
    if (maximum_attempts == 0) {
        throw std::invalid_argument("atomic replace requires at least one attempt");
    }

    for (std::size_t attempt = 0; attempt < maximum_attempts; ++attempt) {
        if (MoveFileExW(
                source.c_str(),
                destination.c_str(),
                MOVEFILE_REPLACE_EXISTING)) {
            return;
        }

        const auto error = GetLastError();
        if (attempt + 1 == maximum_attempts || !is_transient_replace_error(error)) {
            throw std::system_error(
                static_cast<int>(error),
                std::system_category(),
                "cannot atomically replace file");
        }
        std::this_thread::sleep_for(retry_delay);
    }
}

}  // namespace stemstudio
