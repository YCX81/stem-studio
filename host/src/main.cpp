#include "audio_level_meter.h"
#include "audio_session_mute.h"
#include "audio_window_buffer.h"
#include "atomic_file.h"
#include "device_audio_queue.h"
#include "device_udp_sender.h"
#include "live_paths.h"
#include "mixer_control.h"
#include "process_loopback_capture.h"
#include "stem_mixer.h"
#include "stem_sequence_loader.h"
#include "stem_stream_buffer.h"
#include "wasapi_stem_renderer.h"
#include "wav_writer.h"

#include <Windows.h>
#include <objbase.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Media.Control.h>
#include <winrt/base.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cwchar>
#include <filesystem>
#include <format>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {
std::atomic_bool stop_requested{false};

class ComApartment final {
public:
    explicit ComApartment(const DWORD mode) : result_{CoInitializeEx(nullptr, mode)} {}
    ~ComApartment() {
        if (SUCCEEDED(result_)) {
            CoUninitialize();
        }
    }
    ComApartment(const ComApartment&) = delete;
    ComApartment& operator=(const ComApartment&) = delete;
    [[nodiscard]] HRESULT result() const noexcept { return result_; }

private:
    HRESULT result_;
};

BOOL WINAPI handle_console(const DWORD signal) {
    if (signal == CTRL_C_EVENT || signal == CTRL_BREAK_EVENT || signal == CTRL_CLOSE_EVENT) {
        stop_requested.store(true, std::memory_order_release);
        return TRUE;
    }
    return FALSE;
}

int fail_hresult(const char* operation, const HRESULT result) {
    std::cerr << operation << " failed, HRESULT=0x" << std::hex
              << static_cast<unsigned long>(result) << '\n';
    return 1;
}

[[nodiscard]] std::size_t parse_track_count(const std::wstring_view value) {
    if (value == L"2" || value == L"2stems") return 2;
    if (value == L"4" || value == L"4stems") return 4;
    if (value == L"6" || value == L"6stems") return 6;

    // Compatibility with the previous single-monitor CLI while the controller
    // and a running host are upgraded independently.
    if (value == L"instrumental" || value == L"vocals") return 2;
    if (value == L"drums" || value == L"bass" || value == L"other") return 4;
    if (value == L"guitar" || value == L"piano") return 6;
    throw std::invalid_argument("track profile must be 2, 4, or 6");
}

[[nodiscard]] std::uint32_t parse_hop_seconds(const std::wstring_view value) {
    if (value == L"3") return 3;
    if (value == L"6") return 6;
    throw std::invalid_argument("live hop must be 3 or 6 seconds");
}

[[nodiscard]] std::string json_escape(const std::string_view value) {
    std::string escaped;
    escaped.reserve(value.size());
    for (const char character : value) {
        switch (character) {
        case '\\': escaped += "\\\\"; break;
        case '"': escaped += "\\\""; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default: escaped.push_back(character); break;
        }
    }
    return escaped;
}

[[nodiscard]] std::string_view state_name(const stemstudio::BufferReadState state) noexcept {
    switch (state) {
    case stemstudio::BufferReadState::audio: return "playing";
    case stemstudio::BufferReadState::underrun: return "rebuffering";
    case stemstudio::BufferReadState::prebuffering: return "prebuffering";
    }
    return "unknown";
}

[[nodiscard]] std::uint64_t current_system_time_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count());
}

struct WindowsMediaTrack {
    std::uint64_t revision{};
    std::string source;
    std::string title;
    std::string artist;
    std::string album;
    double position_seconds{};
    double duration_seconds{};
    bool playing{};
};

[[nodiscard]] WindowsMediaTrack read_windows_media_track(
    const winrt::Windows::Media::Control::GlobalSystemMediaTransportControlsSessionManager&
        manager) {
    WindowsMediaTrack track;
    const auto session = manager.GetCurrentSession();
    if (!session) return track;

    const auto media = session.TryGetMediaPropertiesAsync().get();
    const auto timeline = session.GetTimelineProperties();
    const auto playback = session.GetPlaybackInfo();
    track.source = winrt::to_string(session.SourceAppUserModelId());
    track.title = winrt::to_string(media.Title());
    track.artist = winrt::to_string(media.Artist());
    track.album = winrt::to_string(media.AlbumTitle());
    track.position_seconds = static_cast<double>(timeline.Position().count()) / 10'000'000.0;
    track.duration_seconds = static_cast<double>(
                                 (timeline.EndTime() - timeline.StartTime()).count()) /
                             10'000'000.0;
    track.playing = playback.PlaybackStatus() ==
                    winrt::Windows::Media::Control::GlobalSystemMediaTransportControlsSessionPlaybackStatus::Playing;
    const auto identity = track.source + '\n' + track.title + '\n' + track.artist + '\n' +
                          track.album;
    track.revision = identity.empty() ? 0 : std::hash<std::string>{}(identity);
    if (track.revision == 0 && !identity.empty()) track.revision = 1;
    return track;
}

void write_capture_input_status(
    const std::filesystem::path& live_root,
    const std::string_view state,
    const bool system_loopback,
    const std::uint32_t process_id,
    const std::uint64_t pcm_frames,
    const stemstudio::AudioLevels& levels,
    const WindowsMediaTrack& track,
    const stemstudio::AudioSessionMuteStats mute_stats,
    const std::string_view media_error = {}) {
    const auto destination = live_root / L"capture-input-status.json";
    auto partial = destination;
    partial += L".part";
    std::ofstream output(partial, std::ios::trunc);
    if (!output) throw std::runtime_error("cannot create capture input status file");
    output << "{\"version\":1"
           << ",\"state\":\"" << state << '"'
           << ",\"source\":\"windows\""
           << ",\"input_scope\":\"" << (system_loopback ? "system" : "process") << '"'
           << ",\"process_id\":" << (system_loopback ? 0 : process_id)
           << ",\"sample_rate\":44100"
           << ",\"channels\":2"
           << ",\"bits_per_sample\":16"
           << ",\"pcm_frames\":" << pcm_frames
           << ",\"peak_left\":" << std::format("{:.6f}", levels.peak_left)
           << ",\"peak_right\":" << std::format("{:.6f}", levels.peak_right)
           << ",\"rms_left\":" << std::format("{:.6f}", levels.rms_left)
           << ",\"rms_right\":" << std::format("{:.6f}", levels.rms_right)
           << ",\"signal_detected\":"
           << ((levels.peak_left > 0.0001F || levels.peak_right > 0.0001F) ? "true" : "false")
           << ",\"source_sessions_tracked\":" << mute_stats.tracked_sessions
           << ",\"source_sessions_isolated\":" << mute_stats.isolated_sessions
           << ",\"source_isolation_db\":-40"
           << ",\"waveform\":[";
    for (std::size_t index = 0; index < levels.waveform.size(); ++index) {
        if (index != 0) output << ',';
        output << std::format("{:.6f}", levels.waveform[index]);
    }
    output << "]"
           << ",\"track\":{"
           << "\"revision\":" << track.revision
           << ",\"source\":\"" << json_escape(track.source) << '"'
           << ",\"title\":\"" << json_escape(track.title) << '"'
           << ",\"artist\":\"" << json_escape(track.artist) << '"'
           << ",\"album\":\"" << json_escape(track.album) << '"'
           << ",\"position_seconds\":" << std::format("{:.3f}", track.position_seconds)
           << ",\"duration_seconds\":" << std::format("{:.3f}", track.duration_seconds)
           << ",\"playing\":" << (track.playing ? "true" : "false")
           << '}';
    if (!media_error.empty()) {
        output << ",\"media_error\":\"" << json_escape(media_error) << '"';
    }
    output << '}';
    output.close();
    if (!output) throw std::runtime_error("failed to finish capture input status file");
    stemstudio::atomic_replace_file(partial, destination);
}

void write_playback_status(
    const std::filesystem::path& live_root,
    const stemstudio::SynchronizedStemBuffer& buffer,
    const stemstudio::WasapiStemRenderer& renderer,
    const std::size_t track_count,
    const std::uint32_t sample_rate,
    const std::size_t window_frames,
    const std::size_t hop_frames,
    const std::size_t overlap_frames,
    const std::uint64_t initial_sequence,
    const std::uint64_t queued_sequence,
    const std::uint64_t skipped_sequence,
    const std::uint64_t control_sequence,
    const stemstudio::MixerControlMetricsSnapshot control_metrics,
    const std::size_t gain_smoothing_frames,
    const stemstudio::DeviceAudioPacketQueue* const device_queue,
    const stemstudio::DeviceUdpSender* const device_sender,
    const std::string_view fatal_error,
    const std::string_view control_error) {
    const auto buffer_stats = buffer.stats();
    const auto renderer_stats = renderer.stats();
    const auto played_sequence = buffer_stats.total_read_frames == 0
                                     ? 0
                                     : initial_sequence +
                                           (buffer_stats.total_read_frames - 1) / hop_frames;
    const auto destination = live_root / L"playback-status.json";
    auto partial = destination;
    partial += L".part";
    std::ofstream output(partial, std::ios::trunc);
    if (!output) {
        throw std::runtime_error("cannot create playback status file");
    }

    const auto state = fatal_error.empty() ? state_name(renderer_stats.state) : "error";
    output << "{\"version\":2"
           << ",\"state\":\"" << state << '"'
           << ",\"sequence\":" << played_sequence
           << ",\"queued_sequence\":" << queued_sequence
           << ",\"skipped_sequence\":" << skipped_sequence
           << ",\"track_count\":" << track_count
           << ",\"stem\":\"mix\""
           << ",\"analysis_window_seconds\":"
           << std::format("{:.3f}", static_cast<double>(window_frames) / sample_rate)
           << ",\"hop_seconds\":"
           << std::format("{:.3f}", static_cast<double>(hop_frames) / sample_rate)
           << ",\"overlap_milliseconds\":"
           << std::format("{:.1f}", static_cast<double>(overlap_frames) * 1'000.0 / sample_rate)
           << ",\"buffered_frames\":" << buffer_stats.buffered_frames
           << ",\"buffered_seconds\":"
           << std::format("{:.3f}", static_cast<double>(buffer_stats.buffered_frames) / sample_rate)
           << ",\"prebuffer_seconds\":"
           << std::format("{:.3f}", static_cast<double>(buffer_stats.prebuffer_frames) / sample_rate)
           << ",\"minimum_buffered_frames\":" << buffer_stats.minimum_buffered_frames
           << ",\"underruns\":" << buffer_stats.underruns
           << ",\"last_underrun_system_time_ns\":"
           << buffer_stats.last_underrun_system_time_ns
           << ",\"last_underrun_buffered_frames\":"
           << buffer_stats.last_underrun_buffered_frames
           << ",\"last_underrun_total_read_frames\":"
           << buffer_stats.last_underrun_total_read_frames
           << ",\"device_open_count\":" << renderer_stats.device_open_count
           << ",\"device_recoveries\":" << renderer_stats.device_recovery_count
           << ",\"device_recovering\":"
           << (renderer_stats.device_recovering ? "true" : "false")
           << ",\"device_buffer_frames\":" << renderer_stats.device_buffer_frames
           << ",\"rendered_audio_frames\":" << renderer_stats.rendered_audio_frames
           << ",\"rendered_silence_frames\":" << renderer_stats.rendered_silence_frames
           << ",\"control_sequence\":" << control_sequence
           << ",\"mixer_updates\":" << control_metrics.update_count
           << ",\"last_mixer_control_latency_ms\":"
           << std::format(
                  "{:.3f}",
                  static_cast<double>(control_metrics.last_latency_microseconds) / 1'000.0)
           << ",\"max_mixer_control_latency_ms\":"
           << std::format(
                  "{:.3f}",
                  static_cast<double>(control_metrics.maximum_latency_microseconds) / 1'000.0)
           << ",\"gain_smoothing_ms\":"
           << std::format(
                  "{:.3f}",
                  static_cast<double>(gain_smoothing_frames) * 1'000.0 / sample_rate);
    if (device_queue != nullptr && device_sender != nullptr) {
        const auto queue_stats = device_queue->stats();
        const auto sender_stats = device_sender->stats();
        output << ",\"device_network_enabled\":true"
               << ",\"device_queue_packets\":" << queue_stats.depth_packets
               << ",\"device_queue_capacity_packets\":" << queue_stats.capacity_packets
               << ",\"device_enqueued_packets\":" << queue_stats.enqueued_packets
               << ",\"device_dropped_packets\":" << queue_stats.dropped_packets
               << ",\"device_dropped_frames\":" << queue_stats.dropped_frames
               << ",\"device_sent_packets\":" << sender_stats.sent_packets
               << ",\"device_sent_bytes\":" << sender_stats.sent_bytes
               << ",\"device_send_errors\":" << sender_stats.send_errors
               << ",\"device_last_socket_error\":" << sender_stats.last_socket_error;
    } else {
        output << ",\"device_network_enabled\":false";
    }
    if (renderer_stats.last_device_hresult != 0) {
        output << ",\"last_device_hresult\":\""
               << std::format(
                      "0x{:08X}",
                      static_cast<std::uint32_t>(renderer_stats.last_device_hresult))
               << '\"';
    }
    output << ",\"stems\":[";
    bool first = true;
    for (const auto id : buffer.active_stems()) {
        if (!first) output << ',';
        output << '"' << stemstudio::stem_name(id) << '"';
        first = false;
    }
    output << "]"
           << ",\"gains\":{";
    first = true;
    for (const auto id : buffer.active_stems()) {
        if (!first) output << ',';
        const auto index = static_cast<std::size_t>(id);
        output << '"' << stemstudio::stem_name(id) << "\":"
               << std::format("{:.4f}", renderer_stats.target_gains.at(index));
        first = false;
    }
    output << '}';
    if (!fatal_error.empty()) {
        output << ",\"error\":\"" << json_escape(fatal_error) << '"';
    }
    if (!control_error.empty()) {
        output << ",\"control_error\":\"" << json_escape(control_error) << '"';
    }
    output << '}';
    output.close();
    if (!output) {
        throw std::runtime_error("failed to finish playback status file");
    }
    stemstudio::atomic_replace_file(partial, destination);
}
}  // namespace

int wmain(const int argc, wchar_t* argv[]) {
    if (argc < 3 || argc > 7) {
        std::wcerr << L"Usage: stem-studio-audio-host <process-id|--system-loopback|--playback-only> "
                      L"<live-data-directory> [2|4|6] [device-ipv4:udp-port] "
                      L"[--hop-seconds 3|6]\n";
        return 2;
    }

    const bool playback_only = std::wstring_view(argv[1]) == L"--playback-only";
    const bool system_loopback = std::wstring_view(argv[1]) == L"--system-loopback";
    unsigned long process_id = 0;
    if (system_loopback) {
        // Excluding this process tree captures every other Windows audio session
        // while keeping Stem Studio's own rendered stems out of the input.
        process_id = GetCurrentProcessId();
    } else if (!playback_only) {
        wchar_t* end = nullptr;
        process_id = std::wcstoul(argv[1], &end, 10);
        if (process_id == 0 || *end != L'\0') {
            std::wcerr << L"Invalid process id.\n";
            return 2;
        }
    }

    try {
        const auto track_count = parse_track_count(argc >= 4 ? std::wstring_view{argv[3]} : L"2");
        std::optional<std::wstring_view> device_endpoint;
        std::uint32_t hop_seconds = 6;
        for (int index = 4; index < argc; ++index) {
            const std::wstring_view argument{argv[index]};
            if (argument == L"--hop-seconds") {
                if (++index >= argc) throw std::invalid_argument("--hop-seconds requires a value");
                hop_seconds = parse_hop_seconds(argv[index]);
            } else if (!device_endpoint.has_value()) {
                device_endpoint = argument;
            } else {
                throw std::invalid_argument("unknown audio host argument");
            }
        }
        const auto active_stems = stemstudio::stems_for_profile(track_count);
        const auto live_root = std::filesystem::absolute(argv[2]);
        const auto inbox = live_root / L"inbox";
        const auto outbox = live_root / L"outbox";
        std::filesystem::create_directories(inbox);
        std::filesystem::create_directories(outbox);

        const ComApartment com{COINIT_MULTITHREADED};
        if (FAILED(com.result())) return fail_hresult("CoInitializeEx", com.result());
        SetConsoleCtrlHandler(handle_console, TRUE);
        const auto shutdown_event_name = std::format(
            L"Local\\StemStudioAudioHostStop-{}", GetCurrentProcessId());
        winrt::handle shutdown_event{
            CreateEventW(nullptr, TRUE, FALSE, shutdown_event_name.c_str())};
        std::optional<std::jthread> shutdown_event_thread;
        if (shutdown_event) {
            shutdown_event_thread.emplace([event = shutdown_event.get()] {
                while (!stop_requested.load(std::memory_order_acquire)) {
                    if (WaitForSingleObject(event, 100) == WAIT_OBJECT_0) {
                        stop_requested.store(true, std::memory_order_release);
                        break;
                    }
                }
            });
        }

        auto geometry = stemstudio::AudioGeometry{};
        geometry.hop_seconds = hop_seconds;
        geometry.validate();
        const auto initial_sequence = stemstudio::next_capture_sequence(inbox, outbox);
        const auto window_frames = static_cast<std::size_t>(geometry.sample_rate) * geometry.window_seconds;
        const auto hop_frames = static_cast<std::size_t>(geometry.sample_rate) * geometry.hop_seconds;
        const auto overlap_frames = static_cast<std::size_t>(geometry.sample_rate) / 10;
        const auto gain_smoothing_frames = static_cast<std::size_t>(geometry.sample_rate) / 50;
        stemstudio::SynchronizedStemBuffer stream_buffer{
            geometry.channels,
            active_stems,
            hop_frames * stemstudio::default_live_buffer_capacity_hops,
            hop_frames * stemstudio::default_live_prebuffer_hops,
        };
        std::unique_ptr<stemstudio::DeviceAudioPacketQueue> device_queue;
        std::unique_ptr<stemstudio::DeviceUdpSender> device_sender;
        if (device_endpoint.has_value()) {
            const auto endpoint = stemstudio::parse_device_udp_endpoint(*device_endpoint);
            const auto session_id = static_cast<std::uint32_t>(
                current_system_time_nanoseconds() ^ static_cast<std::uint64_t>(GetCurrentProcessId()));
            constexpr std::size_t device_queue_capacity_packets = 128;
            const auto frames_per_packet = static_cast<std::size_t>(geometry.sample_rate) / 200;
            device_queue = std::make_unique<stemstudio::DeviceAudioPacketQueue>(
                session_id,
                geometry.sample_rate,
                geometry.channels,
                device_queue_capacity_packets,
                frames_per_packet);
            device_sender = std::make_unique<stemstudio::DeviceUdpSender>(
                endpoint.ipv4_address,
                endpoint.port,
                *device_queue);
        }
        stemstudio::WasapiStemRenderer renderer{
            geometry.sample_rate,
            geometry.channels,
            stream_buffer,
            gain_smoothing_frames,
            device_queue.get(),
        };
        stemstudio::StemSequenceStitcher sequence_stitcher{
            geometry.channels,
            active_stems,
            hop_frames,
            overlap_frames,
        };

        std::mutex error_mutex;
        std::string fatal_error;
        std::string control_error;
        const auto record_error = [&](const std::string_view message) {
            {
                const std::scoped_lock lock{error_mutex};
                if (fatal_error.empty()) {
                    fatal_error = message;
                }
            }
            stop_requested.store(true, std::memory_order_release);
        };
        const auto read_error = [&] {
            const std::scoped_lock lock{error_mutex};
            return fatal_error;
        };
        const auto set_control_error = [&](const std::string_view message) {
            const std::scoped_lock lock{error_mutex};
            control_error = message;
        };
        const auto read_control_error = [&] {
            const std::scoped_lock lock{error_mutex};
            return control_error;
        };

        std::atomic<std::uint64_t> queued_sequence{0};
        std::atomic<std::uint64_t> skipped_sequence{0};
        std::atomic<std::uint64_t> captured_pcm_frames{0};
        std::mutex capture_telemetry_mutex;
        stemstudio::AudioLevels capture_levels;
        capture_levels.waveform.assign(32, 0.0F);
        WindowsMediaTrack windows_media_track;
        std::string windows_media_error;
        auto source_mute_guard = playback_only
                                     ? nullptr
                                 : std::make_unique<stemstudio::AudioSessionMuteGuard>(
                                           GetCurrentProcessId(),
                                           system_loopback
                                               ? 0
                                               : static_cast<std::uint32_t>(process_id));
        std::jthread loader_thread([&] {
            try {
                auto next_sequence = initial_sequence;
                std::optional<stemstudio::LoadedStemSequence> pending;
                while (!stop_requested.load(std::memory_order_acquire)) {
                    if (!pending) {
                        auto result = stemstudio::load_stem_sequence(
                            outbox,
                            next_sequence,
                            active_stems,
                            geometry.sample_rate,
                            geometry.channels);
                        if (result.state == stemstudio::StemSequenceState::waiting) {
                            std::this_thread::sleep_for(std::chrono::milliseconds(50));
                            continue;
                        }
                        if (result.state == stemstudio::StemSequenceState::skipped) {
                            skipped_sequence.store(next_sequence, std::memory_order_release);
                            sequence_stitcher.reset();
                            ++next_sequence;
                            continue;
                        }
                        if (!result.sequence) {
                            throw std::runtime_error("ready separated result omitted its audio sequence");
                        }
                        pending = sequence_stitcher.stitch(std::move(*result.sequence));
                    }

                    std::vector<stemstudio::StemBlockView> views;
                    views.reserve(pending->stems.size());
                    for (const auto& stem : pending->stems) {
                        views.push_back({stem.id, stem.interleaved});
                    }
                    if (!stream_buffer.try_push(views)) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(20));
                        continue;
                    }
                    queued_sequence.store(next_sequence, std::memory_order_release);
                    pending.reset();
                    ++next_sequence;
                }
            } catch (const std::exception& error) {
                record_error(error.what());
            }
        });

        std::jthread renderer_thread([&] {
            try {
                renderer.run(stop_requested);
            } catch (const std::exception& error) {
                record_error(error.what());
            }
        });
        std::optional<std::jthread> device_sender_thread;
        if (device_sender != nullptr) {
            device_sender_thread.emplace([&] { device_sender->run(stop_requested); });
        }

        std::atomic<std::uint64_t> applied_control_sequence{0};
        stemstudio::MixerControlMetrics control_metrics{current_system_time_nanoseconds()};
        std::jthread control_thread([&] {
            const auto control_path = stemstudio::mixer_control_path(live_root, track_count);
            while (!stop_requested.load(std::memory_order_acquire)) {
                try {
                    const auto snapshot = stemstudio::read_mixer_control(control_path);
                    if (snapshot && snapshot->sequence > applied_control_sequence.load(std::memory_order_acquire)) {
                        for (const auto id : active_stems) {
                            if (!snapshot->has_gain(id)) {
                                throw std::runtime_error("mixer snapshot omitted an active stem");
                            }
                        }
                        for (const auto id : active_stems) {
                            renderer.set_gain(id, snapshot->gain(id));
                        }
                        control_metrics.record_applied(
                            snapshot->sequence,
                            current_system_time_nanoseconds());
                        applied_control_sequence.store(snapshot->sequence, std::memory_order_release);
                        set_control_error("");
                    }
                } catch (const std::exception& error) {
                    set_control_error(error.what());
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        });

        std::jthread status_thread([&] {
            try {
                while (!stop_requested.load(std::memory_order_acquire)) {
                    if (!playback_only) {
                        stemstudio::AudioLevels levels;
                        WindowsMediaTrack track;
                        std::string media_error;
                        {
                            const std::scoped_lock lock{capture_telemetry_mutex};
                            levels = capture_levels;
                            track = windows_media_track;
                            media_error = windows_media_error;
                        }
                        write_capture_input_status(
                            live_root,
                            "capturing",
                            system_loopback,
                            static_cast<std::uint32_t>(process_id),
                            captured_pcm_frames.load(std::memory_order_acquire),
                            levels,
                            track,
                            source_mute_guard != nullptr
                                ? source_mute_guard->stats()
                                : stemstudio::AudioSessionMuteStats{},
                            media_error);
                    }
                    write_playback_status(
                        live_root,
                        stream_buffer,
                        renderer,
                        track_count,
                        geometry.sample_rate,
                        window_frames,
                        hop_frames,
                        overlap_frames,
                        initial_sequence,
                        queued_sequence.load(std::memory_order_acquire),
                        skipped_sequence.load(std::memory_order_acquire),
                        applied_control_sequence.load(std::memory_order_acquire),
                        control_metrics.snapshot(),
                        gain_smoothing_frames,
                        device_queue.get(),
                        device_sender.get(),
                        read_error(),
                        read_control_error());
                    std::this_thread::sleep_for(std::chrono::milliseconds(250));
                }
                write_playback_status(
                    live_root,
                    stream_buffer,
                    renderer,
                    track_count,
                    geometry.sample_rate,
                    window_frames,
                    hop_frames,
                    overlap_frames,
                    initial_sequence,
                    queued_sequence.load(std::memory_order_acquire),
                    skipped_sequence.load(std::memory_order_acquire),
                    applied_control_sequence.load(std::memory_order_acquire),
                    control_metrics.snapshot(),
                    gain_smoothing_frames,
                    device_queue.get(),
                    device_sender.get(),
                    read_error(),
                    read_control_error());
            } catch (const std::exception& error) {
                record_error(error.what());
            }
        });

        std::optional<std::jthread> windows_media_thread;
        if (!playback_only) {
            windows_media_thread.emplace([&] {
                try {
                    winrt::init_apartment(winrt::apartment_type::multi_threaded);
                    const auto manager = winrt::Windows::Media::Control::
                        GlobalSystemMediaTransportControlsSessionManager::RequestAsync().get();
                    while (!stop_requested.load(std::memory_order_acquire)) {
                        try {
                            auto track = read_windows_media_track(manager);
                            const std::scoped_lock lock{capture_telemetry_mutex};
                            windows_media_track = std::move(track);
                            windows_media_error.clear();
                        } catch (const winrt::hresult_error& error) {
                            const std::scoped_lock lock{capture_telemetry_mutex};
                            windows_media_error = winrt::to_string(error.message());
                        }
                        std::this_thread::sleep_for(std::chrono::milliseconds(500));
                    }
                    winrt::uninit_apartment();
                } catch (const winrt::hresult_error& error) {
                    const std::scoped_lock lock{capture_telemetry_mutex};
                    windows_media_error = winrt::to_string(error.message());
                }
            });
        }

        std::optional<std::jthread> source_mute_thread;
        if (source_mute_guard != nullptr) {
            source_mute_thread.emplace([&] {
                const auto result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
                while (!stop_requested.load(std::memory_order_acquire)) {
                    try {
                        source_mute_guard->refresh();
                    } catch (...) {
                        // A transient endpoint/session change is retried on the next poll.
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(250));
                }
                source_mute_guard->restore();
                if (SUCCEEDED(result)) CoUninitialize();
            });
        }

        if (playback_only) {
            std::wcout << L"Persistent WASAPI multi-stem playback is ready. Press Ctrl+C to stop.\n";
            while (!stop_requested.load(std::memory_order_acquire)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        } else {
            stemstudio::AudioWindowBuffer windows(
                geometry,
                [&](const std::uint64_t sequence, const std::span<const std::byte> pcm) {
                    const auto filename = std::format(L"capture-{:08}.wav", sequence);
                    stemstudio::write_pcm16_wav_atomic(inbox / filename, geometry, pcm);
                    std::wcout << L"Published " << filename << L'\n';
                },
                initial_sequence);
            auto capture = Microsoft::WRL::Make<stemstudio::ProcessLoopbackCapture>();
            const auto start_result = capture->start(
                static_cast<std::uint32_t>(process_id),
                [&](const std::span<const std::byte> pcm) {
                    auto levels = stemstudio::measure_pcm16_stereo(pcm);
                    {
                        const std::scoped_lock lock{capture_telemetry_mutex};
                        capture_levels = std::move(levels);
                    }
                    captured_pcm_frames.fetch_add(
                        pcm.size() / geometry.bytes_per_frame(),
                        std::memory_order_release);
                    windows.append(pcm);
                },
                system_loopback);
            if (FAILED(start_result)) {
                stop_requested.store(true, std::memory_order_release);
                return fail_hresult("process loopback activation", start_result);
            }
            if (system_loopback) {
                std::wcout << L"Capturing all Windows audio except Stem Studio playback. "
                              L"Press Ctrl+C to stop.\n";
            } else {
                std::wcout << L"Capturing process " << process_id
                           << L" with persistent multi-stem playback. Press Ctrl+C to stop.\n";
            }
            capture->run_until(stop_requested);
        }

        stop_requested.store(true, std::memory_order_release);
        loader_thread.join();
        renderer_thread.join();
        if (device_sender_thread.has_value()) {
            device_sender_thread->join();
        }
        control_thread.join();
        status_thread.join();
        if (windows_media_thread.has_value()) {
            windows_media_thread->join();
        }
        if (source_mute_thread.has_value()) {
            source_mute_thread->join();
        }
        if (shutdown_event_thread.has_value()) {
            shutdown_event_thread->join();
        }
        const auto error = read_error();
        if (!error.empty()) {
            std::cerr << error << '\n';
            return 1;
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
