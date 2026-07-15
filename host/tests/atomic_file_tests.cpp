#include "atomic_file.h"

#include <Windows.h>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <format>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>

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
        path_ = std::filesystem::temp_directory_path() / std::format("stem-atomic-tests-{}", suffix);
        std::filesystem::create_directories(path_);
    }
    ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};

void test_publishes_and_replaces_complete_file() {
    TemporaryDirectory temporary;
    const auto destination = temporary.path() / "status.json";
    const auto partial = temporary.path() / "status.json.part";

    std::ofstream{partial} << "first";
    stemstudio::atomic_replace_file(partial, destination);
    require(std::filesystem::is_regular_file(destination), "initial publication missing");
    require(!std::filesystem::exists(partial), "initial partial file must be consumed");

    std::ofstream{partial} << "second";
    stemstudio::atomic_replace_file(partial, destination);
    std::ifstream input{destination};
    std::string content;
    input >> content;
    require(content == "second", "replacement content mismatch");
    require(!std::filesystem::exists(partial), "replacement partial file must be consumed");
}

void test_concurrent_readers_never_observe_a_missing_or_partial_status() {
    TemporaryDirectory temporary;
    const auto destination = temporary.path() / "status.json";
    const auto partial = temporary.path() / "status.json.part";
    std::ofstream{destination} << "value-0";

    std::atomic_bool stop{false};
    std::atomic<std::uint64_t> samples{0};
    std::atomic<std::uint64_t> transient_open_failures{0};
    std::atomic<std::uint64_t> missing_paths{0};
    std::atomic<std::uint64_t> invalid_reads{0};
    std::jthread reader([&] {
        while (!stop.load(std::memory_order_acquire)) {
            std::ifstream input{destination};
            if (!input) {
                transient_open_failures.fetch_add(1, std::memory_order_relaxed);
                if (GetFileAttributesW(destination.c_str()) == INVALID_FILE_ATTRIBUTES) {
                    const auto error = GetLastError();
                    if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) {
                        missing_paths.fetch_add(1, std::memory_order_relaxed);
                    }
                }
                std::this_thread::yield();
                continue;
            }
            std::string content;
            input >> content;
            if (!content.starts_with("value-")) {
                invalid_reads.fetch_add(1, std::memory_order_relaxed);
            }
            samples.fetch_add(1, std::memory_order_relaxed);
            std::this_thread::yield();
        }
    });

    for (std::uint64_t revision = 1; revision <= 1'000; ++revision) {
        std::ofstream{partial} << "value-" << revision;
        stemstudio::atomic_replace_file(partial, destination);
    }
    stop.store(true, std::memory_order_release);
    reader.join();

    require(samples.load(std::memory_order_relaxed) > 0, "reader did not sample status files");
    require(missing_paths.load(std::memory_order_relaxed) == 0, "reader observed a missing status path");
    require(invalid_reads.load(std::memory_order_relaxed) == 0, "reader observed partial status content");
}
}  // namespace

int main() {
    try {
        test_publishes_and_replaces_complete_file();
        test_concurrent_readers_never_observe_a_missing_or_partial_status();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
