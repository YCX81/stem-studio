#pragma once

#include "device_audio_queue.h"
#include "stem_mixer.h"
#include "stem_stream_buffer.h"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

namespace stemstudio {

struct WasapiRendererStats final {
    std::uint64_t device_open_count{0};
    std::uint64_t device_recovery_count{0};
    std::uint64_t rendered_audio_frames{0};
    std::uint64_t rendered_silence_frames{0};
    std::uint32_t device_buffer_frames{0};
    std::int32_t last_device_hresult{0};
    bool device_recovering{false};
    BufferReadState state{BufferReadState::prebuffering};
    std::array<float, stem_id_count> target_gains{};
};

class WasapiStemRenderer final {
public:
    WasapiStemRenderer(
        std::uint32_t sample_rate,
        std::uint16_t channels,
        SynchronizedStemBuffer& buffer,
        std::size_t smoothing_frames,
        DeviceAudioPacketQueue* device_queue = nullptr);

    void set_gain(StemId id, float gain);
    void run(const std::atomic_bool& stop_requested);
    [[nodiscard]] WasapiRendererStats stats() const noexcept;

private:
    [[nodiscard]] static std::size_t index_for(StemId id);

    std::uint32_t sample_rate_;
    std::uint16_t channels_;
    SynchronizedStemBuffer& buffer_;
    RealtimeStemMixer mixer_;
    DeviceAudioPacketQueue* device_queue_;
    std::array<std::atomic<float>, stem_id_count> target_gains_{};

    std::atomic<std::uint64_t> device_open_count_{0};
    std::atomic<std::uint64_t> device_recovery_count_{0};
    std::atomic<std::uint64_t> rendered_audio_frames_{0};
    std::atomic<std::uint64_t> rendered_silence_frames_{0};
    std::atomic<std::uint32_t> device_buffer_frames_{0};
    std::atomic<std::int32_t> last_device_hresult_{0};
    std::atomic_bool device_recovering_{false};
    std::atomic<BufferReadState> state_{BufferReadState::prebuffering};
};

}  // namespace stemstudio
