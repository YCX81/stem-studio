#include "process_loopback_capture.h"

#include <audioclientactivationparams.h>
#include <propvarutil.h>

#include <stdexcept>
#include <vector>

namespace stemstudio {

ProcessLoopbackCapture::ProcessLoopbackCapture() {
    activation_event_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    sample_event_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (activation_event_ == nullptr || sample_event_ == nullptr) {
        throw std::runtime_error("failed to create WASAPI events");
    }
}

ProcessLoopbackCapture::~ProcessLoopbackCapture() {
    stop();
    if (sample_event_ != nullptr) CloseHandle(sample_event_);
    if (activation_event_ != nullptr) CloseHandle(activation_event_);
}

HRESULT ProcessLoopbackCapture::start(const std::uint32_t process_id, AudioCallback callback) {
    if (process_id == 0 || !callback) return E_INVALIDARG;
    callback_ = std::move(callback);

    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = process_id;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT variant{};
    variant.vt = VT_BLOB;
    variant.blob.cbSize = sizeof(params);
    variant.blob.pBlobData = reinterpret_cast<BYTE*>(&params);
    Microsoft::WRL::ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
    auto hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &variant,
        this,
        &operation);
    if (FAILED(hr)) return hr;
    if (WaitForSingleObject(activation_event_, 15'000) != WAIT_OBJECT_0) return HRESULT_FROM_WIN32(ERROR_TIMEOUT);
    if (FAILED(activation_result_)) return activation_result_;

    format_.wFormatTag = WAVE_FORMAT_PCM;
    format_.nChannels = 2;
    format_.nSamplesPerSec = 44'100;
    format_.wBitsPerSample = 16;
    format_.nBlockAlign = format_.nChannels * format_.wBitsPerSample / 8;
    format_.nAvgBytesPerSec = format_.nSamplesPerSec * format_.nBlockAlign;
    hr = audio_client_->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        0,
        0,
        &format_,
        nullptr);
    if (FAILED(hr)) return hr;
    hr = audio_client_->GetService(IID_PPV_ARGS(&capture_client_));
    if (FAILED(hr)) return hr;
    hr = audio_client_->SetEventHandle(sample_event_);
    if (FAILED(hr)) return hr;
    hr = audio_client_->Start();
    if (SUCCEEDED(hr)) started_ = true;
    return hr;
}

HRESULT ProcessLoopbackCapture::ActivateCompleted(IActivateAudioInterfaceAsyncOperation* operation) {
    Microsoft::WRL::ComPtr<IUnknown> activated;
    HRESULT inner = E_UNEXPECTED;
    auto hr = operation->GetActivateResult(&inner, &activated);
    activation_result_ = FAILED(hr) ? hr : inner;
    if (SUCCEEDED(activation_result_)) {
        activation_result_ = activated.As(&audio_client_);
    }
    SetEvent(activation_event_);
    return S_OK;
}

void ProcessLoopbackCapture::run_until(const std::atomic_bool& stop_requested) {
    while (!stop_requested.load()) {
        if (WaitForSingleObject(sample_event_, 200) == WAIT_OBJECT_0) {
            drain_packets();
        }
    }
    stop();
}

void ProcessLoopbackCapture::drain_packets() {
    UINT32 frames = 0;
    while (SUCCEEDED(capture_client_->GetNextPacketSize(&frames)) && frames > 0) {
        BYTE* data = nullptr;
        DWORD flags = 0;
        UINT64 device_position = 0;
        UINT64 qpc_position = 0;
        if (FAILED(capture_client_->GetBuffer(&data, &frames, &flags, &device_position, &qpc_position))) return;
        const auto byte_count = static_cast<std::size_t>(frames) * format_.nBlockAlign;
        if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0) {
            const std::vector<std::byte> silence(byte_count);
            callback_(silence);
        } else {
            callback_(std::span<const std::byte>(reinterpret_cast<const std::byte*>(data), byte_count));
        }
        capture_client_->ReleaseBuffer(frames);
    }
}

void ProcessLoopbackCapture::stop() noexcept {
    if (started_ && audio_client_) {
        audio_client_->Stop();
        started_ = false;
    }
}

}  // namespace stemstudio
