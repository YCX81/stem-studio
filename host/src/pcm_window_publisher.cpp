#include "pcm_window_publisher.h"

#include "live_paths.h"
#include "wav_writer.h"

#include <format>
#include <fstream>
#include <stdexcept>
#include <utility>

namespace stemstudio {
namespace {
void write_text_atomic(const std::filesystem::path& destination, const std::string& content) {
    if (content.empty()) {
        throw std::invalid_argument("capture annotation cannot be empty");
    }
    auto partial = destination;
    partial += ".part";
    std::ofstream output(partial, std::ios::binary | std::ios::trunc);
    output.write(content.data(), static_cast<std::streamsize>(content.size()));
    output.close();
    if (!output) {
        throw std::runtime_error("failed to finish capture annotation");
    }
    std::error_code ignored;
    std::filesystem::remove(destination, ignored);
    std::filesystem::rename(partial, destination);
}
}  // namespace

PcmWindowPublisher::PcmWindowPublisher(
    std::filesystem::path live_root,
    const AudioGeometry geometry,
    std::uint64_t initial_sequence,
    AnnotationProvider annotation_provider)
    : geometry_(geometry),
      live_root_(std::filesystem::absolute(std::move(live_root))),
      inbox_(live_root_ / "inbox"),
      stats_{.geometry = geometry_},
      annotation_provider_(std::move(annotation_provider)),
      windows_(
          geometry_,
          [this](const std::uint64_t sequence, const std::span<const std::byte> pcm) {
              const auto filename = std::format("capture-{:08}.wav", sequence);
              const auto destination = inbox_ / filename;
              auto staged_audio = destination;
              staged_audio += ".pending";
              auto annotation_path = destination;
              annotation_path.replace_extension(".json");
              const auto hop_frames =
                  static_cast<std::uint64_t>(geometry_.sample_rate) * geometry_.hop_seconds;
              const auto window_frames =
                  static_cast<std::uint64_t>(geometry_.sample_rate) * geometry_.window_seconds;
              const PcmWindowDescriptor descriptor{
                  .sequence = sequence,
                  .stream_start_frame = stats_.published_windows * hop_frames,
                  .stream_end_frame = stats_.published_windows * hop_frames + window_frames,
              };
              try {
                  write_pcm16_wav_atomic(staged_audio, geometry_, pcm);
                  if (annotation_provider_) {
                      const auto annotation = annotation_provider_(descriptor);
                      if (annotation) {
                          write_text_atomic(annotation_path, *annotation);
                      }
                  }
                  std::error_code ignored;
                  std::filesystem::remove(destination, ignored);
                  std::filesystem::rename(staged_audio, destination);
              } catch (...) {
                  std::error_code ignored;
                  std::filesystem::remove(staged_audio, ignored);
                  auto staged_partial = staged_audio;
                  staged_partial += ".part";
                  std::filesystem::remove(staged_partial, ignored);
                  std::filesystem::remove(annotation_path, ignored);
                  auto annotation_partial = annotation_path;
                  annotation_partial += ".part";
                  std::filesystem::remove(annotation_partial, ignored);
                  throw;
              }
              ++stats_.published_windows;
              stats_.last_published_sequence = sequence;
          },
          initial_sequence == 0
              ? next_capture_sequence(inbox_, live_root_ / "outbox")
              : initial_sequence) {
    std::filesystem::create_directories(inbox_);
    std::filesystem::create_directories(live_root_ / "outbox");
}

void PcmWindowPublisher::append(const std::span<const std::byte> pcm) {
    if (pcm.size() % geometry_.bytes_per_frame() != 0) {
        throw std::invalid_argument("PCM input must contain complete stereo frames");
    }
    std::scoped_lock lock(mutex_);
    windows_.append(pcm);
    stats_.pcm_frames += pcm.size() / geometry_.bytes_per_frame();
}

PcmPublisherStats PcmWindowPublisher::stats() const {
    std::scoped_lock lock(mutex_);
    return stats_;
}

}  // namespace stemstudio
