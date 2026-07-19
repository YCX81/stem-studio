#pragma once

#include <Audioclient.h>
#include <Mmdeviceapi.h>
#include <Windows.h>
#include <wrl.h>
#include <wrl/implements.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>

namespace stemstudio {

class ProcessLoopbackCapture final
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          Microsoft::WRL::FtmBase,
          IActivateAudioInterfaceCompletionHandler> {
public:
    using AudioCallback = std::function<void(std::span<const std::byte>)>;

    ProcessLoopbackCapture();
    ~ProcessLoopbackCapture() override;
    HRESULT start(
        std::uint32_t process_id,
        AudioCallback callback,
        bool exclude_process_tree = false);
    void run_until(const std::atomic_bool& stop_requested);
    void stop() noexcept;

    IFACEMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* operation) override;

private:
    void drain_packets();

    Microsoft::WRL::ComPtr<IAudioClient> audio_client_;
    Microsoft::WRL::ComPtr<IAudioCaptureClient> capture_client_;
    HANDLE activation_event_{nullptr};
    HANDLE sample_event_{nullptr};
    HRESULT activation_result_{E_PENDING};
    AudioCallback callback_;
    WAVEFORMATEX format_{};
    bool started_{false};
};

}  // namespace stemstudio
