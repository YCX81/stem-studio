#pragma once

#include <Audiopolicy.h>
#include <wrl/client.h>

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>

namespace stemstudio {

// Windows may mark process-loopback packets as AUDCLNT_BUFFERFLAGS_SILENT
// when a session is attenuated to -80 dB. Keep the source below normal
// listening level without crossing that engine-side silence threshold.
inline constexpr float audio_session_isolation_volume = 0.01F;
inline constexpr float audio_session_isolation_compensation =
    1.0F / audio_session_isolation_volume;

struct AudioSessionMuteStats {
    std::size_t tracked_sessions{};
    std::size_t isolated_sessions{};
};

class AudioSessionMuteGuard final {
public:
    AudioSessionMuteGuard(
        std::uint32_t excluded_process_id,
        std::uint32_t target_process_id = 0);
    ~AudioSessionMuteGuard();

    AudioSessionMuteGuard(const AudioSessionMuteGuard&) = delete;
    AudioSessionMuteGuard& operator=(const AudioSessionMuteGuard&) = delete;

    void refresh();
    void restore() noexcept;
    [[nodiscard]] AudioSessionMuteStats stats() const noexcept;

private:
    struct SessionState {
        Microsoft::WRL::ComPtr<ISimpleAudioVolume> volume;
        float original_volume{1.0F};
        bool originally_muted{};
    };

    std::uint32_t excluded_process_id_{};
    std::uint32_t target_process_id_{};
    mutable std::mutex mutex_;
    std::unordered_map<std::wstring, SessionState> sessions_;
    std::size_t isolated_sessions_{};
};

}  // namespace stemstudio
