#include <Windows.h>
#include <mmsystem.h>

#include <cmath>
#include <cstdint>
#include <string_view>
#include <vector>

int wmain(const int argc, wchar_t* argv[]) {
    constexpr std::uint32_t sample_rate = 44'100;
    constexpr std::uint32_t seconds = 40;
    constexpr double frequency = 523.25;
    std::vector<std::int16_t> samples(sample_rate * seconds * 2);
    for (std::size_t frame = 0; frame < sample_rate * seconds; ++frame) {
        const auto value = static_cast<std::int16_t>(std::sin(2.0 * 3.141592653589793 * frequency * frame / sample_rate) * 8'000);
        samples[frame * 2] = value;
        samples[frame * 2 + 1] = value;
    }
    WAVEFORMATEX format{};
    format.wFormatTag = WAVE_FORMAT_PCM;
    format.nChannels = 2;
    format.nSamplesPerSec = sample_rate;
    format.wBitsPerSample = 16;
    format.nBlockAlign = 4;
    format.nAvgBytesPerSec = sample_rate * format.nBlockAlign;
    HWAVEOUT device = nullptr;
    if (waveOutOpen(&device, WAVE_MAPPER, &format, 0, 0, CALLBACK_NULL) != MMSYSERR_NOERROR) return 1;
    if (argc == 2 && std::wstring_view(argv[1]) == L"--muted") {
        waveOutSetVolume(device, 0);
    }
    WAVEHDR header{};
    header.lpData = reinterpret_cast<LPSTR>(samples.data());
    header.dwBufferLength = static_cast<DWORD>(samples.size() * sizeof(std::int16_t));
    if (waveOutPrepareHeader(device, &header, sizeof(header)) != MMSYSERR_NOERROR) return 2;
    if (waveOutWrite(device, &header, sizeof(header)) != MMSYSERR_NOERROR) return 3;
    while ((header.dwFlags & WHDR_DONE) == 0) Sleep(50);
    waveOutUnprepareHeader(device, &header, sizeof(header));
    waveOutClose(device);
    return 0;
}
