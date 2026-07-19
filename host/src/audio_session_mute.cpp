#include "audio_session_mute.h"

#include <Mmdeviceapi.h>
#include <Windows.h>

#include <stdexcept>

namespace stemstudio {

AudioSessionMuteGuard::AudioSessionMuteGuard(
    const std::uint32_t excluded_process_id,
    const std::uint32_t target_process_id)
    : excluded_process_id_{excluded_process_id},
      target_process_id_{target_process_id} {
    if (excluded_process_id_ == 0) {
        throw std::invalid_argument("excluded audio session process id must be positive");
    }
}

AudioSessionMuteGuard::~AudioSessionMuteGuard() {
    restore();
}

void AudioSessionMuteGuard::refresh() {
    Microsoft::WRL::ComPtr<IMMDeviceEnumerator> devices;
    auto hr = CoCreateInstance(
        __uuidof(MMDeviceEnumerator),
        nullptr,
        CLSCTX_ALL,
        IID_PPV_ARGS(&devices));
    if (FAILED(hr)) throw std::runtime_error("cannot create audio device enumerator");

    Microsoft::WRL::ComPtr<IMMDevice> endpoint;
    hr = devices->GetDefaultAudioEndpoint(eRender, eMultimedia, &endpoint);
    if (FAILED(hr)) throw std::runtime_error("cannot open default audio endpoint");

    Microsoft::WRL::ComPtr<IAudioSessionManager2> manager;
    hr = endpoint->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, &manager);
    if (FAILED(hr)) throw std::runtime_error("cannot activate audio session manager");

    Microsoft::WRL::ComPtr<IAudioSessionEnumerator> enumerator;
    hr = manager->GetSessionEnumerator(&enumerator);
    if (FAILED(hr)) throw std::runtime_error("cannot enumerate audio sessions");

    int count = 0;
    if (FAILED(enumerator->GetCount(&count))) return;
    std::scoped_lock lock{mutex_};
    isolated_sessions_ = 0;
    for (int index = 0; index < count; ++index) {
        Microsoft::WRL::ComPtr<IAudioSessionControl> control;
        if (FAILED(enumerator->GetSession(index, &control))) continue;
        Microsoft::WRL::ComPtr<IAudioSessionControl2> control2;
        if (FAILED(control.As(&control2))) continue;
        DWORD process_id = 0;
        if (FAILED(control2->GetProcessId(&process_id)) || process_id == 0 ||
            process_id == excluded_process_id_) {
            continue;
        }
        if (target_process_id_ != 0 && process_id != target_process_id_) continue;
        LPWSTR raw_identifier = nullptr;
        if (FAILED(control2->GetSessionInstanceIdentifier(&raw_identifier)) ||
            raw_identifier == nullptr) {
            continue;
        }
        const std::wstring identifier{raw_identifier};
        CoTaskMemFree(raw_identifier);
        Microsoft::WRL::ComPtr<ISimpleAudioVolume> volume;
        if (FAILED(control.As(&volume))) continue;

        auto entry = sessions_.find(identifier);
        if (entry == sessions_.end()) {
            float original_volume = 1.0F;
            BOOL muted = FALSE;
            if (FAILED(volume->GetMasterVolume(&original_volume)) ||
                FAILED(volume->GetMute(&muted))) {
                continue;
            }
            entry = sessions_.emplace(
                identifier,
                SessionState{volume, original_volume, muted != FALSE}).first;
        } else {
            entry->second.volume = volume;
        }
        const auto volume_result =
            volume->SetMasterVolume(audio_session_isolation_volume, nullptr);
        const auto mute_result = volume->SetMute(FALSE, nullptr);
        if (SUCCEEDED(volume_result) && SUCCEEDED(mute_result)) ++isolated_sessions_;
    }
}

void AudioSessionMuteGuard::restore() noexcept {
    std::scoped_lock lock{mutex_};
    for (auto& [identifier, state] : sessions_) {
        static_cast<void>(identifier);
        if (state.volume) {
            state.volume->SetMasterVolume(state.original_volume, nullptr);
            state.volume->SetMute(state.originally_muted ? TRUE : FALSE, nullptr);
        }
    }
    sessions_.clear();
    isolated_sessions_ = 0;
}

AudioSessionMuteStats AudioSessionMuteGuard::stats() const noexcept {
    std::scoped_lock lock{mutex_};
    return {sessions_.size(), isolated_sessions_};
}

}  // namespace stemstudio
