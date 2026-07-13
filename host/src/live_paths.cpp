#include "live_paths.h"

#include <algorithm>
#include <array>
#include <format>
#include <regex>

namespace stemstudio {
namespace {
std::uint64_t maximum_matching_sequence(
    const std::filesystem::path& directory,
    const std::wregex& pattern) {
    std::uint64_t maximum{0};
    if (!std::filesystem::is_directory(directory)) {
        return maximum;
    }
    for (const auto& entry : std::filesystem::directory_iterator(directory)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        std::wsmatch match;
        const auto name = entry.path().filename().wstring();
        if (std::regex_match(name, match, pattern)) {
            maximum = (std::max)(maximum, std::stoull(match[1].str()));
        }
    }
    return maximum;
}
}  // namespace

bool is_supported_monitor_stem(const std::wstring_view stem) noexcept {
    static constexpr std::array<std::wstring_view, 7> supported{
        L"instrumental", L"vocals", L"drums", L"bass", L"other", L"guitar", L"piano"};
    return std::ranges::find(supported, stem) != supported.end();
}

std::uint64_t next_capture_sequence(
    const std::filesystem::path& inbox,
    const std::filesystem::path& outbox) {
    static const std::wregex capture_pattern{LR"(^capture-(\d{8})\.wav$)"};
    static const std::wregex result_pattern{LR"(^result-(\d{8})(?:\.json|-[^.]+\.wav)$)"};
    const auto capture_maximum = maximum_matching_sequence(inbox, capture_pattern);
    const auto result_maximum = maximum_matching_sequence(outbox, result_pattern);
    return (std::max)(capture_maximum, result_maximum) + 1;
}

PlaybackAvailability probe_playback(
    const std::filesystem::path& outbox,
    const std::uint64_t sequence,
    const std::wstring_view stem) {
    const auto stem_path = outbox / std::format(L"result-{:08}-{}.wav", sequence, stem);
    if (std::filesystem::is_regular_file(stem_path)) {
        return PlaybackAvailability::ready;
    }
    const auto manifest = outbox / std::format(L"result-{:08}.json", sequence);
    if (std::filesystem::is_regular_file(manifest)) {
        return PlaybackAvailability::skipped;
    }
    return PlaybackAvailability::waiting;
}

}  // namespace stemstudio
