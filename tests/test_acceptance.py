from stemstudio.acceptance import LiveAcceptanceRecorder


def _snapshot(**overrides) -> dict:
    snapshot = {
        "streaming": False,
        "playback_state": "prebuffering",
        "captured_sequence": 148,
        "gpu_sequence": 148,
        "skipped_sequence": 0,
        "underruns": 0,
        "device_recoveries": 0,
        "device_recovering": False,
        "mixer_updates": 12,
        "last_mixer_control_latency_ms": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_hit": False,
        "cache_scope": "none",
        "songs_cached": 0,
        "pending_windows": 0,
        "ready_buffer_seconds": 0.0,
        "track_title": "",
        "track_artist": "",
        "track_duration_seconds": 0.0,
    }
    snapshot.update(overrides)
    return snapshot


def test_acceptance_waits_until_real_streaming_evidence_exists() -> None:
    recorder = LiveAcceptanceRecorder()

    recorder.observe(_snapshot(), observed_at_ns=1)
    report = recorder.report(observed_at_ns=2)

    assert report["state"] == "waiting_for_phone"
    assert report["passed"] is False
    assert report["mixer_latency_limit_ms"] == 50.0
    assert report["requirements"]["stream_received"] is False


def test_acceptance_passes_first_play_replay_and_live_mixer_evidence() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
            ready_buffer_seconds=12.0,
            track_title="Acceptance Song",
            track_artist="Artist",
            track_duration_seconds=180.0,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            cache_misses=2,
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=24.5,
            ready_buffer_seconds=11.8,
            track_title="Acceptance Song",
            track_artist="Artist",
            track_duration_seconds=180.0,
        ),
        observed_at_ns=3,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=151,
            gpu_sequence=151,
            cache_hits=1,
            cache_misses=2,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=13,
            ready_buffer_seconds=12.0,
            track_title="Acceptance Song",
            track_artist="Artist",
            track_duration_seconds=180.0,
        ),
        observed_at_ns=4,
    )

    report = recorder.report(observed_at_ns=5)

    assert report["state"] == "passed"
    assert report["passed"] is True
    assert all(report["requirements"].values())
    assert report["metrics"]["active_underrun_delta"] == 0
    assert report["metrics"]["active_mixer_update_delta"] == 1
    assert report["metrics"]["max_active_mixer_latency_ms"] == 24.5
    assert report["metrics"]["tracks_seen"] == [
        {
            "title": "Acceptance Song",
            "artist": "Artist",
            "duration_seconds": 180.0,
        }
    ]


def test_acceptance_rejects_a_mixer_update_above_the_default_50ms_limit() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            cache_misses=1,
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=50.001,
        ),
        observed_at_ns=3,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=151,
            gpu_sequence=151,
            cache_hits=1,
            cache_misses=1,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=13,
        ),
        observed_at_ns=4,
    )

    report = recorder.report(observed_at_ns=5)

    assert report["state"] == "failed"
    assert report["requirements"]["mixer_adjusted_during_stream"] is True
    assert report["requirements"]["mixer_latency_below_limit"] is False
    assert report["metrics"]["max_active_mixer_latency_ms"] == 50.001


def test_acceptance_rejects_an_underrun_during_active_playback() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            cache_hits=1,
            cache_misses=1,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=20.0,
            underruns=1,
        ),
        observed_at_ns=3,
    )

    report = recorder.report(observed_at_ns=4)

    assert report["state"] == "failed"
    assert report["requirements"]["zero_active_underruns"] is False
    assert report["metrics"]["active_underrun_delta"] == 1


def test_acceptance_rejects_a_device_recovery_during_active_playback() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(device_recoveries=1), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
            device_recoveries=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            skipped_sequence=0,
            cache_hits=1,
            cache_misses=1,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=20.0,
            device_recoveries=2,
        ),
        observed_at_ns=3,
    )

    report = recorder.report(observed_at_ns=4)

    assert report["state"] == "failed"
    assert report["requirements"]["zero_active_device_recoveries"] is False
    assert report["metrics"]["active_device_recovery_delta"] == 1


def test_acceptance_rejects_a_device_still_recovering_when_audio_is_active() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
            device_recovering=True,
        ),
        observed_at_ns=2,
    )

    report = recorder.report(observed_at_ns=3)

    assert report["requirements"]["zero_active_device_recoveries"] is False
    assert report["metrics"]["active_device_recovering_seen"] is True


def test_acceptance_rejects_a_skipped_sequence_during_active_playback() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(skipped_sequence=148), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            skipped_sequence=148,
            cache_misses=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            skipped_sequence=149,
            cache_hits=1,
            cache_misses=1,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=20.0,
        ),
        observed_at_ns=3,
    )

    report = recorder.report(observed_at_ns=4)

    assert report["state"] == "failed"
    assert report["requirements"]["zero_active_skipped_sequences"] is False
    assert report["metrics"]["active_skipped_sequence_delta"] == 1


def test_acceptance_ignores_mixer_updates_and_underruns_outside_active_audio() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            mixer_updates=13,
            last_mixer_control_latency_ms=12.0,
            underruns=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
            mixer_updates=13,
            underruns=1,
        ),
        observed_at_ns=3,
    )
    recorder.observe(
        _snapshot(
            playback_state="prebuffering",
            captured_sequence=149,
            gpu_sequence=149,
            cache_misses=1,
            mixer_updates=13,
            underruns=2,
        ),
        observed_at_ns=4,
    )

    report = recorder.report(observed_at_ns=5)

    assert report["requirements"]["mixer_adjusted_during_stream"] is False
    assert report["metrics"]["active_underrun_delta"] == 0


def test_acceptance_ignores_idle_continuity_events_between_first_play_and_replay() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(skipped_sequence=148), observed_at_ns=1)
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=149,
            gpu_sequence=149,
            skipped_sequence=148,
            cache_misses=1,
        ),
        observed_at_ns=2,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=150,
            gpu_sequence=150,
            skipped_sequence=148,
            cache_misses=2,
            songs_cached=1,
            mixer_updates=13,
            last_mixer_control_latency_ms=20.0,
        ),
        observed_at_ns=3,
    )
    recorder.observe(
        _snapshot(
            skipped_sequence=149,
            underruns=1,
            device_recoveries=1,
            device_recovering=True,
            cache_misses=2,
            songs_cached=1,
            mixer_updates=14,
        ),
        observed_at_ns=4,
    )
    recorder.observe(
        _snapshot(
            streaming=True,
            playback_state="playing",
            captured_sequence=151,
            gpu_sequence=151,
            skipped_sequence=149,
            underruns=1,
            device_recoveries=1,
            cache_hits=1,
            cache_misses=2,
            cache_hit=True,
            cache_scope="song",
            songs_cached=1,
            mixer_updates=14,
        ),
        observed_at_ns=5,
    )

    report = recorder.report(observed_at_ns=6)

    assert report["state"] == "passed"
    assert report["requirements"]["zero_active_underruns"] is True
    assert report["requirements"]["zero_active_device_recoveries"] is True
    assert report["requirements"]["zero_active_skipped_sequences"] is True
    assert report["metrics"]["active_underrun_delta"] == 0
    assert report["metrics"]["active_device_recovery_delta"] == 0
    assert report["metrics"]["active_skipped_sequence_delta"] == 0


def test_acceptance_does_not_keep_a_stale_startup_song_inventory() -> None:
    recorder = LiveAcceptanceRecorder()
    recorder.observe(_snapshot(songs_cached=2), observed_at_ns=1)
    recorder.observe(_snapshot(songs_cached=0), observed_at_ns=2)

    report = recorder.report(observed_at_ns=3)

    assert report["requirements"]["song_cache_available"] is False
    assert report["metrics"]["songs_cached_initial"] == 2
    assert report["metrics"]["songs_cached_maximum"] == 2
    assert report["metrics"]["songs_cached_latest"] == 0
