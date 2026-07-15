#include "mixer_control.h"

#include <chrono>
#include <filesystem>
#include <format>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>

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
        path_ = std::filesystem::temp_directory_path() / std::format("stem-control-tests-{}", suffix);
        std::filesystem::create_directories(path_);
    }
    ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};

void test_reads_an_atomic_gain_snapshot() {
    TemporaryDirectory temporary;
    const auto path = temporary.path() / "mixer-control.tsv";
    require(!stemstudio::read_mixer_control(path), "a missing snapshot must be ignored");
    std::ofstream output(path);
    output << "stem-studio-mixer-v1\n"
              "sequence\t42\n"
              "vocals\t0.75\n"
              "drums\t0\n"
              "bass\t1\n";
    output.close();

    const auto snapshot = stemstudio::read_mixer_control(path);
    require(snapshot && snapshot->sequence == 42, "control sequence mismatch");
    require(snapshot->has_gain(stemstudio::StemId::vocals), "vocal gain missing");
    require(snapshot->gain(stemstudio::StemId::vocals) == 0.75F, "vocal gain mismatch");
    require(snapshot->gain(stemstudio::StemId::drums) == 0.0F, "mute gain mismatch");
    require(!snapshot->has_gain(stemstudio::StemId::piano), "unspecified gain must remain absent");
}

void test_rejects_partial_or_unsafe_values() {
    TemporaryDirectory temporary;
    const auto path = temporary.path() / "mixer-control.tsv";
    std::ofstream{path} << "stem-studio-mixer-v1\nsequence\t1\nvocals\t1.5\n";
    bool rejected = false;
    try {
        (void)stemstudio::read_mixer_control(path);
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    require(rejected, "out-of-range gain must be rejected");

    std::ofstream{path} << "stem-studio-mixer-v1\nsequence\t2\nunknown\t0.5\n";
    rejected = false;
    try {
        (void)stemstudio::read_mixer_control(path);
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    require(rejected, "unknown stem must be rejected");
}

void test_selects_profile_specific_control_path() {
    const std::filesystem::path root{"live"};
    require(
        stemstudio::mixer_control_path(root, 2) == root / "mixer-control-2.tsv",
        "two-track control path mismatch");
    require(
        stemstudio::mixer_control_path(root, 4) == root / "mixer-control-4.tsv",
        "four-track control path mismatch");
    require(
        stemstudio::mixer_control_path(root, 6) == root / "mixer-control-6.tsv",
        "six-track control path mismatch");

    bool rejected = false;
    try {
        (void)stemstudio::mixer_control_path(root, 3);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "unsupported mixer track count must be rejected");
}

void test_tracks_applied_control_latency_and_peak() {
    stemstudio::MixerControlMetrics metrics;
    metrics.record_applied(1'000'000'000ULL, 1'012'345'678ULL);
    metrics.record_applied(2'000'000'000ULL, 2'004'000'000ULL);

    const auto snapshot = metrics.snapshot();
    require(snapshot.update_count == 2, "mixer update count mismatch");
    require(
        snapshot.last_latency_microseconds == 4'000,
        "latest mixer latency mismatch");
    require(
        snapshot.maximum_latency_microseconds == 12'346,
        "mixer latency must round up to avoid understating the peak");
}

void test_clamps_future_control_clock_to_zero_latency() {
    stemstudio::MixerControlMetrics metrics;
    metrics.record_applied(2'000'000'001ULL, 2'000'000'000ULL);

    const auto snapshot = metrics.snapshot();
    require(snapshot.update_count == 1, "future control command must still be counted");
    require(snapshot.last_latency_microseconds == 0, "future command latency must clamp to zero");
    require(snapshot.maximum_latency_microseconds == 0, "future command must not inflate peak latency");
}

void test_ignores_restored_control_snapshot_older_than_observation_start() {
    stemstudio::MixerControlMetrics metrics{2'000'000'000ULL};
    metrics.record_applied(1'999'999'999ULL, 2'100'000'000ULL);

    const auto snapshot = metrics.snapshot();
    require(snapshot.update_count == 0, "restored control must not count as a live interaction");
    require(snapshot.last_latency_microseconds == 0, "restored control must not report latency");
    require(snapshot.maximum_latency_microseconds == 0, "restored control must not inflate peak latency");
}
}  // namespace

int main() {
    test_reads_an_atomic_gain_snapshot();
    test_rejects_partial_or_unsafe_values();
    test_selects_profile_specific_control_path();
    test_tracks_applied_control_latency_and_peak();
    test_clamps_future_control_clock_to_zero_latency();
    test_ignores_restored_control_snapshot_older_than_observation_start();
    return 0;
}
