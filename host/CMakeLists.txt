cmake_minimum_required(VERSION 3.25)
project(StemStudioAudioHost VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

add_library(stem_host_core STATIC
    src/airplay_track_state.cpp
    src/airplay_stream_state.cpp
    src/atomic_file.cpp
    src/audio_level_meter.cpp
    src/audio_window_buffer.cpp
    src/center_suppressor.cpp
    src/device_audio_protocol.cpp
    src/live_paths.cpp
    src/mixer_control.cpp
    src/pcm_window_publisher.cpp
    src/stem_mixer.cpp
    src/stem_sequence_loader.cpp
    src/stem_stream_buffer.cpp
    src/wasapi_recovery_policy.cpp
    src/wav_reader.cpp
    src/wav_writer.cpp
)
target_include_directories(stem_host_core PUBLIC include)
target_compile_definitions(stem_host_core PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_compile_options(stem_host_core PRIVATE /W4 /permissive- /EHsc)

add_executable(stem-studio-audio-host
    src/main.cpp
    src/process_loopback_capture.cpp
    src/wasapi_stem_renderer.cpp
)
target_link_libraries(stem-studio-audio-host PRIVATE stem_host_core mmdevapi ole32 avrt winmm)
target_compile_definitions(stem-studio-audio-host PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_compile_options(stem-studio-audio-host PRIVATE /W4 /permissive- /EHsc)

enable_testing()
add_executable(stem_host_tests tests/audio_window_buffer_tests.cpp)
target_link_libraries(stem_host_tests PRIVATE stem_host_core)
target_compile_options(stem_host_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME stem_host_tests COMMAND stem_host_tests)

add_executable(stem_mixer_tests tests/stem_mixer_tests.cpp)
target_link_libraries(stem_mixer_tests PRIVATE stem_host_core)
target_compile_options(stem_mixer_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME stem_mixer_tests COMMAND stem_mixer_tests)

add_executable(stem_stream_buffer_tests tests/stem_stream_buffer_tests.cpp)
target_link_libraries(stem_stream_buffer_tests PRIVATE stem_host_core)
target_compile_options(stem_stream_buffer_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME stem_stream_buffer_tests COMMAND stem_stream_buffer_tests)

add_executable(wav_reader_tests tests/wav_reader_tests.cpp)
target_link_libraries(wav_reader_tests PRIVATE stem_host_core)
target_compile_options(wav_reader_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME wav_reader_tests COMMAND wav_reader_tests)

add_executable(stem_sequence_loader_tests tests/stem_sequence_loader_tests.cpp)
target_link_libraries(stem_sequence_loader_tests PRIVATE stem_host_core)
target_compile_options(stem_sequence_loader_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME stem_sequence_loader_tests COMMAND stem_sequence_loader_tests)

add_executable(mixer_control_tests tests/mixer_control_tests.cpp)
target_link_libraries(mixer_control_tests PRIVATE stem_host_core)
target_compile_options(mixer_control_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME mixer_control_tests COMMAND mixer_control_tests)

add_executable(atomic_file_tests tests/atomic_file_tests.cpp)
target_link_libraries(atomic_file_tests PRIVATE stem_host_core)
target_compile_options(atomic_file_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME atomic_file_tests COMMAND atomic_file_tests)

add_executable(airplay_track_state_tests tests/airplay_track_state_tests.cpp)
target_link_libraries(airplay_track_state_tests PRIVATE stem_host_core)
target_compile_options(airplay_track_state_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME airplay_track_state_tests COMMAND airplay_track_state_tests)

add_executable(airplay_stream_state_tests tests/airplay_stream_state_tests.cpp)
target_link_libraries(airplay_stream_state_tests PRIVATE stem_host_core)
target_compile_options(airplay_stream_state_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME airplay_stream_state_tests COMMAND airplay_stream_state_tests)

add_executable(wasapi_recovery_policy_tests tests/wasapi_recovery_policy_tests.cpp)
target_link_libraries(wasapi_recovery_policy_tests PRIVATE stem_host_core)
target_compile_options(wasapi_recovery_policy_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME wasapi_recovery_policy_tests COMMAND wasapi_recovery_policy_tests)

add_executable(device_audio_protocol_tests tests/device_audio_protocol_tests.cpp)
target_link_libraries(device_audio_protocol_tests PRIVATE stem_host_core)
target_compile_options(device_audio_protocol_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME device_audio_protocol_tests COMMAND device_audio_protocol_tests)

add_executable(stem_host_tone_player tests/tone_player.cpp)
target_link_libraries(stem_host_tone_player PRIVATE winmm)
target_compile_definitions(stem_host_tone_player PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
