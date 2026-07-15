#include "wasapi_stem_renderer.h"
#include "wasapi_recovery_policy.h"

#include <Windows.h>
#include <audioclient.h>
#include <avrt.h>
#include <mmdeviceapi.h>
#include <wrl/client.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <format>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace stemstudio {
namespace {
using Microsoft::WRL::ComPtr;

class HResultFailure final : public std::runtime_error {
public:
    HResultFailure(const std::string_view operation, const HRESULT result)
        : std::runtime_error{std::format(
              "{} failed, HRESULT=0x{:08X}",
              operation,
              static_cast<std::uint32_t>(result))},
          result_{result} {}

    [[nodiscard]] HRESULT result() const noexcept { return result_; }

private:
    HRESULT result_;
};

[[noreturn]] void throw_hresult(const std::string_view operation, const HRESULT result) {
    throw HResultFailure{operation, result};
}

void require_success(const std::string_view operation, const HRESULT result) {
    if (FAILED(result)) {
        throw_hresult(operation, result);
    }
}

class Apartment final {
public:
    Apartment() {
        const auto result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(result) && result != RPC_E_CHANGED_MODE) {
            throw_hresult("CoInitializeEx", result);
        }
        should_uninitialize_ = SUCCEEDED(result);
    }
    ~Apartment() {
        if (should_uninitialize_) {
            CoUninitialize();
        }
    }

private:
    bool should_uninitialize_{false};
};

class EventHandle final {
public:
    EventHandle() : value_{CreateEventW(nullptr, FALSE, FALSE, nullptr)} {
        if (value_ == nullptr) {
            throw std::runtime_error("CreateEventW failed for WASAPI renderer");
        }
    }
    ~EventHandle() { CloseHandle(value_); }
    EventHandle(const EventHandle&) = delete;
    EventHandle& operator=(const EventHandle&) = delete;
    [[nodiscard]] HANDLE get() const noexcept { return value_; }

private:
    HANDLE value_;
};

class MultimediaPriority final {
public:
    MultimediaPriority() {
        handle_ = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task_index_);
    }
    ~MultimediaPriority() {
        if (handle_ != nullptr) {
            AvRevertMmThreadCharacteristics(handle_);
        }
    }

private:
    DWORD task_index_{0};
    HANDLE handle_{nullptr};
};
}  // namespace

WasapiStemRenderer::WasapiStemRenderer(
    const std::uint32_t sample_rate,
    const std::uint16_t channels,
    SynchronizedStemBuffer& buffer,
    const std::size_t smoothing_frames)
    : sample_rate_{sample_rate},
      channels_{channels},
      buffer_{buffer},
      mixer_{channels, smoothing_frames} {
    if (sample_rate_ == 0 || channels_ == 0 || buffer_.channels() != channels_) {
        throw std::invalid_argument("WASAPI renderer geometry mismatch");
    }
    for (auto& gain : target_gains_) {
        gain.store(1.0F, std::memory_order_relaxed);
    }
}

std::size_t WasapiStemRenderer::index_for(const StemId id) {
    const auto index = static_cast<std::size_t>(id);
    if (index >= stem_id_count) {
        throw std::invalid_argument("unknown stem id");
    }
    return index;
}

void WasapiStemRenderer::set_gain(const StemId id, const float gain) {
    if (!std::isfinite(gain) || gain < 0.0F || gain > 1.0F) {
        throw std::invalid_argument("stem gain must be finite and between zero and one");
    }
    target_gains_.at(index_for(id)).store(gain, std::memory_order_release);
}

void WasapiStemRenderer::run(const std::atomic_bool& stop_requested) {
    Apartment apartment;
    MultimediaPriority priority;

    std::uint32_t consecutive_failures = 0;
    while (!stop_requested.load(std::memory_order_acquire)) {
        try {
            ComPtr<IMMDeviceEnumerator> enumerator;
            require_success(
                "CoCreateInstance(MMDeviceEnumerator)",
                CoCreateInstance(
                    __uuidof(MMDeviceEnumerator),
                    nullptr,
                    CLSCTX_ALL,
                    IID_PPV_ARGS(&enumerator)));

            ComPtr<IMMDevice> endpoint;
            require_success(
                "GetDefaultAudioEndpoint",
                enumerator->GetDefaultAudioEndpoint(eRender, eMultimedia, &endpoint));

            ComPtr<IAudioClient> client;
            require_success(
                "IMMDevice::Activate(IAudioClient)",
                endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, &client));

            WAVEFORMATEX format{};
            format.wFormatTag = WAVE_FORMAT_PCM;
            format.nChannels = channels_;
            format.nSamplesPerSec = sample_rate_;
            format.wBitsPerSample = 16;
            format.nBlockAlign = static_cast<WORD>(channels_ * sizeof(std::int16_t));
            format.nAvgBytesPerSec = sample_rate_ * format.nBlockAlign;

            constexpr DWORD stream_flags =
                AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY |
                AUDCLNT_STREAMFLAGS_NOPERSIST;
            constexpr REFERENCE_TIME requested_buffer_duration = 200'000;  // 20 ms
            require_success(
                "IAudioClient::Initialize",
                client->Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    stream_flags,
                    requested_buffer_duration,
                    0,
                    &format,
                    nullptr));

            UINT32 device_buffer_frames = 0;
            require_success("IAudioClient::GetBufferSize", client->GetBufferSize(&device_buffer_frames));
            if (device_buffer_frames == 0) {
                throw std::runtime_error("WASAPI returned an empty device buffer");
            }
            device_buffer_frames_.store(device_buffer_frames, std::memory_order_release);

            EventHandle audio_event;
            require_success("IAudioClient::SetEventHandle", client->SetEventHandle(audio_event.get()));

            ComPtr<IAudioRenderClient> render_client;
            require_success(
                "IAudioClient::GetService(IAudioRenderClient)",
                client->GetService(IID_PPV_ARGS(&render_client)));

            const auto active_stems = buffer_.active_stems();
            std::array<std::vector<std::int16_t>, stem_id_count> scratch{};
            std::vector<MutableStemBlockView> read_views;
            std::vector<StemBlockView> mix_views;
            read_views.reserve(active_stems.size());
            mix_views.reserve(active_stems.size());
            const auto maximum_samples = static_cast<std::size_t>(device_buffer_frames) * channels_;
            for (const auto id : active_stems) {
                auto& samples = scratch.at(index_for(id));
                samples.resize(maximum_samples);
                read_views.push_back(MutableStemBlockView{id, samples});
                mix_views.push_back(StemBlockView{id, samples});
            }

            std::array<float, stem_id_count> applied_gains{};
            applied_gains.fill(1.0F);
            const auto render = [&](const UINT32 frame_count) {
                if (frame_count == 0) {
                    return;
                }
                const auto sample_count = static_cast<std::size_t>(frame_count) * channels_;
                for (std::size_t index = 0; index < active_stems.size(); ++index) {
                    const auto id = active_stems[index];
                    auto& samples = scratch.at(index_for(id));
                    read_views[index].interleaved = std::span{samples}.first(sample_count);
                    mix_views[index].interleaved =
                        std::span<const std::int16_t>{samples}.first(sample_count);
                }
                for (const auto id : active_stems) {
                    const auto index = index_for(id);
                    const auto requested = target_gains_.at(index).load(std::memory_order_acquire);
                    if (requested != applied_gains.at(index)) {
                        mixer_.set_gain(id, requested);
                        applied_gains.at(index) = requested;
                    }
                }

                BYTE* device_data = nullptr;
                require_success(
                    "IAudioRenderClient::GetBuffer",
                    render_client->GetBuffer(frame_count, &device_data));
                const auto read_state = buffer_.pop(read_views);
                state_.store(read_state, std::memory_order_release);
                DWORD release_flags = AUDCLNT_BUFFERFLAGS_SILENT;
                if (read_state == BufferReadState::audio) {
                    mixer_.mix(
                        mix_views,
                        std::span{
                            reinterpret_cast<std::int16_t*>(device_data),
                            sample_count});
                    release_flags = 0;
                    rendered_audio_frames_.fetch_add(frame_count, std::memory_order_relaxed);
                } else {
                    rendered_silence_frames_.fetch_add(frame_count, std::memory_order_relaxed);
                }
                require_success(
                    "IAudioRenderClient::ReleaseBuffer",
                    render_client->ReleaseBuffer(frame_count, release_flags));
            };

            device_open_count_.fetch_add(1, std::memory_order_relaxed);
            render(device_buffer_frames);
            require_success("IAudioClient::Start", client->Start());
            consecutive_failures = 0;
            device_recovering_.store(false, std::memory_order_release);

            while (!stop_requested.load(std::memory_order_acquire)) {
                const auto wait_result = WaitForSingleObject(audio_event.get(), 100);
                if (wait_result == WAIT_TIMEOUT) {
                    continue;
                }
                if (wait_result != WAIT_OBJECT_0) {
                    throw std::runtime_error("WASAPI audio event wait failed");
                }
                UINT32 padding = 0;
                require_success("IAudioClient::GetCurrentPadding", client->GetCurrentPadding(&padding));
                if (padding > device_buffer_frames) {
                    throw std::runtime_error("WASAPI padding exceeds device buffer");
                }
                render(device_buffer_frames - padding);
            }
            require_success("IAudioClient::Stop", client->Stop());
            return;
        } catch (const HResultFailure& error) {
            if (stop_requested.load(std::memory_order_acquire)) {
                device_recovering_.store(false, std::memory_order_release);
                return;
            }
            const auto result = static_cast<std::int32_t>(error.result());
            if (!should_retry_wasapi_failure(result)) {
                throw;
            }
            last_device_hresult_.store(result, std::memory_order_release);
            device_recovery_count_.fetch_add(1, std::memory_order_relaxed);
            device_buffer_frames_.store(0, std::memory_order_release);
            device_recovering_.store(true, std::memory_order_release);
            state_.store(BufferReadState::prebuffering, std::memory_order_release);

            auto remaining = wasapi_retry_delay(consecutive_failures++);
            constexpr auto polling_interval = std::chrono::milliseconds{20};
            while (remaining.count() > 0 &&
                   !stop_requested.load(std::memory_order_acquire)) {
                const auto pause = std::min(remaining, polling_interval);
                std::this_thread::sleep_for(pause);
                remaining -= pause;
            }
        }
    }
    device_recovering_.store(false, std::memory_order_release);
}

WasapiRendererStats WasapiStemRenderer::stats() const noexcept {
    WasapiRendererStats result{
        .device_open_count = device_open_count_.load(std::memory_order_acquire),
        .device_recovery_count = device_recovery_count_.load(std::memory_order_acquire),
        .rendered_audio_frames = rendered_audio_frames_.load(std::memory_order_acquire),
        .rendered_silence_frames = rendered_silence_frames_.load(std::memory_order_acquire),
        .device_buffer_frames = device_buffer_frames_.load(std::memory_order_acquire),
        .last_device_hresult = last_device_hresult_.load(std::memory_order_acquire),
        .device_recovering = device_recovering_.load(std::memory_order_acquire),
        .state = state_.load(std::memory_order_acquire),
    };
    for (std::size_t index = 0; index < target_gains_.size(); ++index) {
        result.target_gains.at(index) = target_gains_.at(index).load(std::memory_order_acquire);
    }
    return result;
}

}  // namespace stemstudio
