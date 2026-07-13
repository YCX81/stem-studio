cmake_minimum_required(VERSION 3.25)
project(StemStudioAudioHost VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

add_library(stem_host_core STATIC
    src/audio_window_buffer.cpp
    src/center_suppressor.cpp
    src/live_paths.cpp
    src/wav_writer.cpp
)
target_include_directories(stem_host_core PUBLIC include)
target_compile_definitions(stem_host_core PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_compile_options(stem_host_core PRIVATE /W4 /permissive- /EHsc)

add_executable(stem-studio-audio-host
    src/main.cpp
    src/process_loopback_capture.cpp
)
target_link_libraries(stem-studio-audio-host PRIVATE stem_host_core mmdevapi ole32 avrt winmm)
target_compile_definitions(stem-studio-audio-host PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_compile_options(stem-studio-audio-host PRIVATE /W4 /permissive- /EHsc)

enable_testing()
add_executable(stem_host_tests tests/audio_window_buffer_tests.cpp)
target_link_libraries(stem_host_tests PRIVATE stem_host_core)
target_compile_options(stem_host_tests PRIVATE /W4 /permissive- /EHsc)
add_test(NAME stem_host_tests COMMAND stem_host_tests)

add_executable(stem_host_tone_player tests/tone_player.cpp)
target_link_libraries(stem_host_tone_player PRIVATE winmm)
target_compile_definitions(stem_host_tone_player PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
