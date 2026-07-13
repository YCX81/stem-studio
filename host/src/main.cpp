#include "audio_window_buffer.h"
#include "live_paths.h"
#include "process_loopback_capture.h"
#include "wav_writer.h"

#include <Windows.h>
#include <objbase.h>
#include <mmsystem.h>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <format>
#include <fstream>
#include <iostream>
#include <string>
#include <string_view>
#include <thread>

namespace {
std::atomic_bool stop_requested{false};

BOOL WINAPI handle_console(const DWORD signal) {
    if (signal == CTRL_C_EVENT || signal == CTRL_BREAK_EVENT || signal == CTRL_CLOSE_EVENT) {
        stop_requested.store(true);
        return TRUE;
    }
    return FALSE;
}

int fail_hresult(const char* operation, const HRESULT hr) {
    std::cerr << operation << " failed, HRESULT=0x" << std::hex << static_cast<unsigned long>(hr) << '\n';
    return 1;
}

void write_playback_status(
    const std::filesystem::path& live_root,
    const std::string_view state,
    const std::uint64_t sequence,
    const std::string_view stem) {
    const auto destination = live_root / L"playback-status.json";
    auto partial = destination;
    partial += L".part";
    std::ofstream output(partial, std::ios::trunc);
    output << "{\"state\":\"" << state
           << "\",\"sequence\":" << sequence
           << ",\"stem\":\"" << stem << "\"}";
    output.close();
    std::error_code ignored;
    std::filesystem::remove(destination, ignored);
    std::filesystem::rename(partial, destination);
}
}  // namespace

int wmain(const int argc, wchar_t* argv[]) {
    if (argc < 3 || argc > 4) {
        std::wcerr << L"Usage: stem-studio-audio-host <process-id> <live-data-directory> [stem]\n";
        return 2;
    }
    wchar_t* end = nullptr;
    const auto process_id = std::wcstoul(argv[1], &end, 10);
    if (process_id == 0 || *end != L'\0') {
        std::wcerr << L"Invalid process id.\n";
        return 2;
    }
    const auto live_root = std::filesystem::absolute(argv[2]);
    const auto inbox = live_root / L"inbox";
    const auto outbox = live_root / L"outbox";
    const std::wstring stem = argc == 4 ? argv[3] : L"instrumental";
    if (!stemstudio::is_supported_monitor_stem(stem)) {
        std::wcerr << L"Unsupported monitor stem.\n";
        return 2;
    }
    const auto stem_name = [&stem] {
        std::string value;
        value.reserve(stem.size());
        for (const wchar_t character : stem) {
            value.push_back(static_cast<char>(character));
        }
        return value;
    }();
    std::filesystem::create_directories(inbox);
    std::filesystem::create_directories(outbox);
    const auto co_result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(co_result)) return fail_hresult("CoInitializeEx", co_result);
    SetConsoleCtrlHandler(handle_console, TRUE);

    try {
        const stemstudio::AudioGeometry geometry{};
        const auto initial_sequence = stemstudio::next_capture_sequence(inbox, outbox);
        stemstudio::AudioWindowBuffer windows(geometry, [&](const std::uint64_t sequence, const std::span<const std::byte> pcm) {
            const auto filename = std::format(L"capture-{:08}.wav", sequence);
            stemstudio::write_pcm16_wav_atomic(inbox / filename, geometry, pcm);
            std::wcout << L"Published " << filename << L'\n';
        }, initial_sequence);
        auto capture = Microsoft::WRL::Make<stemstudio::ProcessLoopbackCapture>();
        const auto start_result = capture->start(static_cast<std::uint32_t>(process_id), [&](const std::span<const std::byte> pcm) {
            windows.append(pcm);
        });
        if (FAILED(start_result)) {
            CoUninitialize();
            return fail_hresult("process loopback activation", start_result);
        }
        std::jthread monitor([&](const std::stop_token stop_token) {
            auto sequence = initial_sequence;
            while (!stop_token.stop_requested() && !stop_requested.load()) {
                const auto filename = std::format(L"result-{:08}-{}.wav", sequence, stem);
                const auto path = outbox / filename;
                const auto availability = stemstudio::probe_playback(outbox, sequence, stem);
                if (availability == stemstudio::PlaybackAvailability::ready) {
                    write_playback_status(live_root, "playing", sequence, stem_name);
                    PlaySoundW(path.c_str(), nullptr, SND_FILENAME | SND_SYNC | SND_NODEFAULT);
                    write_playback_status(live_root, "played", sequence, stem_name);
                    ++sequence;
                } else if (availability == stemstudio::PlaybackAvailability::skipped) {
                    write_playback_status(live_root, "skipped", sequence, stem_name);
                    ++sequence;
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
            }
        });
        std::wcout << L"Capturing process " << process_id << L". Press Ctrl+C to stop.\n";
        capture->run_until(stop_requested);
        monitor.request_stop();
        PlaySoundW(nullptr, nullptr, 0);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        CoUninitialize();
        return 1;
    }
    CoUninitialize();
    return 0;
}
