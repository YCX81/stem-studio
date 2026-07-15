#include "mixer_control.h"

#include <charconv>
#include <cmath>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace stemstudio {
namespace {
[[nodiscard]] std::size_t index_for(const StemId id) {
    const auto index = static_cast<std::size_t>(id);
    if (index >= stem_id_count) {
        throw std::invalid_argument("unknown stem id");
    }
    return index;
}

[[nodiscard]] std::uint64_t parse_sequence(const std::string_view text) {
    std::uint64_t value = 0;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size() || value == 0) {
        throw std::runtime_error("invalid mixer control sequence");
    }
    return value;
}

[[nodiscard]] float parse_gain(const std::string_view text) {
    float value = 0.0F;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size() ||
        !std::isfinite(value) || value < 0.0F || value > 1.0F) {
        throw std::runtime_error("invalid mixer control gain");
    }
    return value;
}
}  // namespace

bool MixerControlSnapshot::has_gain(const StemId id) const {
    return present.at(index_for(id));
}

float MixerControlSnapshot::gain(const StemId id) const {
    if (!has_gain(id)) {
        throw std::invalid_argument("mixer control has no value for stem");
    }
    return gains.at(index_for(id));
}

MixerControlMetrics::MixerControlMetrics(
    const std::uint64_t observation_start_nanoseconds) noexcept
    : observation_start_nanoseconds_{observation_start_nanoseconds} {}

void MixerControlMetrics::record_applied(
    const std::uint64_t command_time_nanoseconds,
    const std::uint64_t applied_time_nanoseconds) noexcept {
    if (command_time_nanoseconds < observation_start_nanoseconds_) {
        return;
    }
    const auto latency_nanoseconds = applied_time_nanoseconds > command_time_nanoseconds
                                         ? applied_time_nanoseconds - command_time_nanoseconds
                                         : 0;
    const auto latency_microseconds = latency_nanoseconds == 0
                                          ? 0
                                          : 1 + (latency_nanoseconds - 1) / 1'000;
    last_latency_microseconds_.store(latency_microseconds, std::memory_order_relaxed);

    auto maximum = maximum_latency_microseconds_.load(std::memory_order_relaxed);
    while (maximum < latency_microseconds &&
           !maximum_latency_microseconds_.compare_exchange_weak(
               maximum,
               latency_microseconds,
               std::memory_order_relaxed,
               std::memory_order_relaxed)) {
    }
    update_count_.fetch_add(1, std::memory_order_release);
}

MixerControlMetricsSnapshot MixerControlMetrics::snapshot() const noexcept {
    return {
        update_count_.load(std::memory_order_acquire),
        last_latency_microseconds_.load(std::memory_order_relaxed),
        maximum_latency_microseconds_.load(std::memory_order_relaxed),
    };
}

std::filesystem::path mixer_control_path(
    const std::filesystem::path& live_root,
    const std::size_t track_count) {
    switch (track_count) {
    case 2: return live_root / "mixer-control-2.tsv";
    case 4: return live_root / "mixer-control-4.tsv";
    case 6: return live_root / "mixer-control-6.tsv";
    default: throw std::invalid_argument("mixer track count must be 2, 4, or 6");
    }
}

std::optional<MixerControlSnapshot> read_mixer_control(const std::filesystem::path& path) {
    if (!std::filesystem::is_regular_file(path)) {
        return std::nullopt;
    }
    std::ifstream input(path);
    if (!input) {
        throw std::runtime_error("cannot open mixer control snapshot");
    }

    std::string line;
    if (!std::getline(input, line)) {
        throw std::runtime_error("empty mixer control snapshot");
    }
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line != "stem-studio-mixer-v1") {
        throw std::runtime_error("unsupported mixer control version");
    }

    MixerControlSnapshot snapshot{};
    bool has_sequence = false;
    bool has_any_gain = false;
    while (std::getline(input, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        const auto separator = line.find('\t');
        if (separator == std::string::npos || line.find('\t', separator + 1) != std::string::npos) {
            throw std::runtime_error("invalid mixer control row");
        }
        const std::string_view key{line.data(), separator};
        const std::string_view value{line.data() + separator + 1, line.size() - separator - 1};
        if (key == "sequence") {
            if (has_sequence) {
                throw std::runtime_error("duplicate mixer control sequence");
            }
            snapshot.sequence = parse_sequence(value);
            has_sequence = true;
            continue;
        }

        const auto id = stem_id_from_name(key);
        if (!id) {
            throw std::runtime_error("unknown mixer control stem");
        }
        const auto index = index_for(*id);
        if (snapshot.present.at(index)) {
            throw std::runtime_error("duplicate mixer control stem");
        }
        snapshot.gains.at(index) = parse_gain(value);
        snapshot.present.at(index) = true;
        has_any_gain = true;
    }
    if (!input.eof()) {
        throw std::runtime_error("failed while reading mixer control snapshot");
    }
    if (!has_sequence || !has_any_gain) {
        throw std::runtime_error("incomplete mixer control snapshot");
    }
    return snapshot;
}

}  // namespace stemstudio
