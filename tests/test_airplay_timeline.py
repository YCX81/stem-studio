import json
from pathlib import Path

import pytest

from stemstudio.airplay_timeline import (
    AirPlayAnnotationError,
    load_capture_annotation,
    try_load_capture_annotation,
)


def _anchor(
    revision: int,
    stream_frame: int,
    track_frame: int,
    *,
    title: str = "Song",
) -> dict:
    return {
        "revision": revision,
        "metadata_revision": revision,
        "has_progress": True,
        "start_rtp": 1_000,
        "current_rtp": 1_000 + track_frame,
        "end_rtp": 11_000,
        "anchor_stream_frame": stream_frame,
        "track_position_frame": track_frame,
        "track_duration_frame": 10_000,
        "title": title,
        "artist": "Artist",
        "album": "Album",
    }


def _payload() -> dict:
    anchors = [
        _anchor(1, 90, 1_000),
        _anchor(1, 160, 1_070),
        _anchor(2, 200, 0, title="Next Song"),
    ]
    return {
        "version": 1,
        "source": "airplay",
        "sequence": 7,
        "sample_rate": 10,
        "stream_start_frame": 100,
        "stream_end_frame": 220,
        "track": anchors[-1],
        "anchors": anchors,
    }


def test_annotation_maps_output_frames_across_progress_and_track_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capture-00000007.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    annotation = load_capture_annotation(
        path,
        expected_sequence=7,
        expected_sample_rate=10,
        expected_window_frames=120,
    )
    segments = annotation.output_segments(offset_frames=20, frame_count=100)

    assert [
        (
            segment.revision,
            segment.stream_start_frame,
            segment.stream_end_frame,
            segment.track_start_frame,
            segment.track_end_frame,
            segment.title,
        )
        for segment in segments
    ] == [
        (1, 120, 160, 1_030, 1_070, "Song"),
        (1, 160, 200, 1_070, 1_110, "Song"),
        (2, 200, 220, 0, 20, "Next Song"),
    ]


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda payload: payload.update(sequence=8), "序号"),
        (lambda payload: payload.update(sample_rate=44_100), "采样率"),
        (
            lambda payload: payload["anchors"].reverse(),
            "时间顺序",
        ),
        (
            lambda payload: payload["anchors"][0].update(track_position_frame=-1),
            "非负整数",
        ),
    ],
)
def test_annotation_rejects_mismatched_or_unsafe_timeline(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    payload = _payload()
    mutation(payload)
    path = tmp_path / "capture-00000007.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AirPlayAnnotationError, match=message):
        load_capture_annotation(
            path,
            expected_sequence=7,
            expected_sample_rate=10,
            expected_window_frames=120,
        )


def test_optional_annotation_loader_turns_missing_or_corrupt_sidecar_into_miss(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "capture-00000007.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert try_load_capture_annotation(missing, expected_sequence=7) is None
    assert try_load_capture_annotation(corrupt, expected_sequence=7) is None


def test_first_progress_anchor_back_projects_to_window_start_when_safe(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["anchors"] = [_anchor(1, 120, 1_020)]
    payload["track"] = payload["anchors"][0]
    path = tmp_path / "capture-00000007.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    annotation = load_capture_annotation(path, expected_sequence=7)
    segments = annotation.output_segments(offset_frames=0, frame_count=60)

    assert len(segments) == 1
    assert segments[0].stream_start_frame == 100
    assert segments[0].track_start_frame == 1_000
    assert segments[0].stream_end_frame == 160
