#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <glib.h>
#include <gst/app/gstappsink.h>
#include <gst/gst.h>

#include "renderers/audio_renderer.h"
#include "lib/logger.h"

typedef struct pcm_stats_s {
    GMutex mutex;
    uint64_t frames;
    int peak;
} pcm_stats_t;

static void log_message(void *context, int level, const char *message) {
    (void)context;
    (void)level;
    fprintf(stderr, "%s\n", message);
}

static void receive_pcm(
    void *context,
    const unsigned char *data,
    size_t size,
    unsigned char compression_type) {
    pcm_stats_t *stats = (pcm_stats_t *)context;
    if (compression_type != 2 || size % 4 != 0) {
        return;
    }
    int local_peak = 0;
    for (size_t offset = 0; offset + 1 < size; offset += 2) {
        const int16_t sample = (int16_t)((uint16_t)data[offset] | ((uint16_t)data[offset + 1] << 8));
        const int magnitude = sample < 0 ? -(int)sample : (int)sample;
        if (magnitude > local_peak) {
            local_peak = magnitude;
        }
    }
    g_mutex_lock(&stats->mutex);
    stats->frames += size / 4;
    if (local_peak > stats->peak) {
        stats->peak = local_peak;
    }
    g_mutex_unlock(&stats->mutex);
}

int main(void) {
    const uint64_t required_frames = 4410U;
    bool audio_sync = false;
    bool video_sync = false;
    unsigned char compression_type = 2;
    unsigned short sequence = 0;
    uint64_t ntp_time = 0;
    GError *error = NULL;
    pcm_stats_t stats = {0};
    g_mutex_init(&stats.mutex);

    logger_t *logger = logger_init();
    logger_set_callback(logger, log_message, NULL);
    logger_set_level(logger, LOGGER_INFO);
    if (!gstreamer_init()) {
        return 2;
    }
    audio_renderer_set_pcm_callback(receive_pcm, &stats);
    audio_renderer_init(logger, "fakesink", &audio_sync, &video_sync, "");
    audio_renderer_start(&compression_type);

    GstElement *generator = gst_parse_launch(
        "audiotestsrc wave=sine samplesperbuffer=441 ! "
        "audio/x-raw,format=S16LE,rate=44100,channels=2 ! "
        "avenc_alac ! appsink name=generator_sink sync=false",
        &error);
    if (error != NULL || generator == NULL) {
        fprintf(stderr, "ALAC generator failed: %s\n", error ? error->message : "unknown");
        return 3;
    }
    GstElement *sink = gst_bin_get_by_name(GST_BIN(generator), "generator_sink");
    gst_element_set_state(generator, GST_STATE_PLAYING);

    for (int chunk = 0; chunk < 500; ++chunk) {
        GstSample *sample = gst_app_sink_try_pull_sample(GST_APP_SINK(sink), GST_SECOND);
        if (sample == NULL) {
            return 4;
        }
        GstBuffer *buffer = gst_sample_get_buffer(sample);
        GstMapInfo map;
        if (gst_buffer_map(buffer, &map, GST_MAP_READ)) {
            int byte_count = (int)map.size;
            audio_renderer_render_buffer(map.data, &byte_count, &sequence, &ntp_time);
            gst_buffer_unmap(buffer, &map);
        }
        gst_sample_unref(sample);
        ++sequence;
        g_usleep(1000);
        g_mutex_lock(&stats.mutex);
        const gboolean enough = stats.frames >= required_frames;
        g_mutex_unlock(&stats.mutex);
        if (enough) {
            break;
        }
    }

    g_usleep(250000);
    gst_element_set_state(generator, GST_STATE_NULL);
    gst_object_unref(sink);
    gst_object_unref(generator);
    g_mutex_lock(&stats.mutex);
    const uint64_t frames = stats.frames;
    const int peak = stats.peak;
    g_mutex_unlock(&stats.mutex);
    printf("{\"codec\":\"ALAC\",\"pcm_frames\":%llu,\"peak_pcm16\":%d}\n",
           (unsigned long long)frames, peak);
    if (frames < required_frames || peak < 1000) {
        return 5;
    }
    return 0;
}
