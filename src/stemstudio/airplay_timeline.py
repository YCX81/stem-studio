from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AirPlayAnnotationError(ValueError):
    """The capture sidecar cannot be trusted for song-aligned caching."""


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AirPlayAnnotationError(f"{name} 必须是非负整数。")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > 1_024:
        raise AirPlayAnnotationError(f"{name} 必须是长度受限的文本。")
    return value


@dataclass(frozen=True)
class AirPlayTrackAnchor:
    revision: int
    metadata_revision: int
    has_progress: bool
    start_rtp: int
    current_rtp: int
    end_rtp: int
    stream_frame: int
    track_position_frame: int
    track_duration_frame: int
    title: str
    artist: str
    album: str

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "metadata_revision": self.metadata_revision,
            "has_progress": self.has_progress,
            "start_rtp": self.start_rtp,
            "current_rtp": self.current_rtp,
            "end_rtp": self.end_rtp,
            "anchor_stream_frame": self.stream_frame,
            "track_position_frame": self.track_position_frame,
            "track_duration_frame": self.track_duration_frame,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }


@dataclass(frozen=True)
class AirPlayTrackSegment:
    revision: int
    metadata_revision: int
    start_rtp: int
    stream_start_frame: int
    stream_end_frame: int
    track_start_frame: int
    track_end_frame: int
    track_duration_frame: int
    title: str
    artist: str
    album: str

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "metadata_revision": self.metadata_revision,
            "start_rtp": self.start_rtp,
            "stream_start_frame": self.stream_start_frame,
            "stream_end_frame": self.stream_end_frame,
            "track_start_frame": self.track_start_frame,
            "track_end_frame": self.track_end_frame,
            "track_duration_frame": self.track_duration_frame,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }


@dataclass(frozen=True)
class CaptureAnnotation:
    sequence: int
    sample_rate: int
    stream_start_frame: int
    stream_end_frame: int
    track: AirPlayTrackAnchor
    anchors: tuple[AirPlayTrackAnchor, ...]

    @property
    def window_frames(self) -> int:
        return self.stream_end_frame - self.stream_start_frame

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "source": "airplay",
            "sequence": self.sequence,
            "sample_rate": self.sample_rate,
            "stream_start_frame": self.stream_start_frame,
            "stream_end_frame": self.stream_end_frame,
            "track": self.track.to_dict(),
            "anchors": [anchor.to_dict() for anchor in self.anchors],
        }

    def output_segments(
        self,
        *,
        offset_frames: int,
        frame_count: int,
    ) -> tuple[AirPlayTrackSegment, ...]:
        if offset_frames < 0 or frame_count <= 0:
            raise ValueError("输出区间必须包含正数帧。")
        output_start = self.stream_start_frame + offset_frames
        output_end = output_start + frame_count
        if output_end > self.stream_end_frame:
            raise ValueError("输出区间超出捕获窗口。")

        current: AirPlayTrackAnchor | None = None
        cursor = output_start
        events: list[AirPlayTrackAnchor] = []
        for anchor in self.anchors:
            if anchor.stream_frame <= output_start:
                current = anchor
            elif anchor.stream_frame < output_end:
                events.append(anchor)

        if current is None:
            if not events:
                return ()
            current = events.pop(0)
            projected_start = (
                current.track_position_frame
                - (current.stream_frame - output_start)
            )
            cursor = output_start if projected_start >= 0 else current.stream_frame

        segments: list[AirPlayTrackSegment] = []

        def append_segment(anchor: AirPlayTrackAnchor, start: int, end: int) -> None:
            if end <= start or not anchor.has_progress or anchor.track_duration_frame <= 0:
                return
            track_start = anchor.track_position_frame + start - anchor.stream_frame
            track_end = anchor.track_position_frame + end - anchor.stream_frame
            if track_start < 0 or track_start >= anchor.track_duration_frame:
                return
            if track_end > anchor.track_duration_frame:
                end -= track_end - anchor.track_duration_frame
                track_end = anchor.track_duration_frame
            if end <= start:
                return
            segments.append(
                AirPlayTrackSegment(
                    revision=anchor.revision,
                    metadata_revision=anchor.metadata_revision,
                    start_rtp=anchor.start_rtp,
                    stream_start_frame=start,
                    stream_end_frame=end,
                    track_start_frame=track_start,
                    track_end_frame=track_end,
                    track_duration_frame=anchor.track_duration_frame,
                    title=anchor.title,
                    artist=anchor.artist,
                    album=anchor.album,
                )
            )

        for anchor in events:
            append_segment(current, cursor, anchor.stream_frame)
            current = anchor
            cursor = anchor.stream_frame
        append_segment(current, cursor, output_end)
        return tuple(segments)


def _parse_anchor(payload: Any, name: str) -> AirPlayTrackAnchor:
    if not isinstance(payload, dict):
        raise AirPlayAnnotationError(f"{name} 必须是对象。")
    has_progress = payload.get("has_progress")
    if type(has_progress) is not bool:
        raise AirPlayAnnotationError(f"{name}.has_progress 必须是布尔值。")
    anchor = AirPlayTrackAnchor(
        revision=_integer(payload.get("revision"), f"{name}.revision"),
        metadata_revision=_integer(
            payload.get("metadata_revision"),
            f"{name}.metadata_revision",
        ),
        has_progress=has_progress,
        start_rtp=_integer(payload.get("start_rtp"), f"{name}.start_rtp"),
        current_rtp=_integer(payload.get("current_rtp"), f"{name}.current_rtp"),
        end_rtp=_integer(payload.get("end_rtp"), f"{name}.end_rtp"),
        stream_frame=_integer(
            payload.get("anchor_stream_frame"),
            f"{name}.anchor_stream_frame",
        ),
        track_position_frame=_integer(
            payload.get("track_position_frame"),
            f"{name}.track_position_frame",
        ),
        track_duration_frame=_integer(
            payload.get("track_duration_frame"),
            f"{name}.track_duration_frame",
        ),
        title=_text(payload.get("title"), f"{name}.title"),
        artist=_text(payload.get("artist"), f"{name}.artist"),
        album=_text(payload.get("album"), f"{name}.album"),
    )
    if anchor.track_position_frame > anchor.track_duration_frame:
        raise AirPlayAnnotationError(f"{name} 的歌曲位置超过总时长。")
    return anchor


def load_capture_annotation(
    path: str | Path,
    *,
    expected_sequence: int | None = None,
    expected_sample_rate: int | None = None,
    expected_window_frames: int | None = None,
) -> CaptureAnnotation:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AirPlayAnnotationError("AirPlay 时间轴侧车文件不可读。") from exc
    if not isinstance(payload, dict):
        raise AirPlayAnnotationError("AirPlay 时间轴根节点必须是对象。")
    if payload.get("version") != 1 or payload.get("source") != "airplay":
        raise AirPlayAnnotationError("AirPlay 时间轴版本或来源无效。")

    sequence = _integer(payload.get("sequence"), "sequence", minimum=1)
    sample_rate = _integer(payload.get("sample_rate"), "sample_rate", minimum=1)
    stream_start = _integer(payload.get("stream_start_frame"), "stream_start_frame")
    stream_end = _integer(payload.get("stream_end_frame"), "stream_end_frame", minimum=1)
    if stream_end <= stream_start:
        raise AirPlayAnnotationError("AirPlay 时间轴窗口范围无效。")
    if expected_sequence is not None and sequence != expected_sequence:
        raise AirPlayAnnotationError("AirPlay 时间轴序号与捕获文件不一致。")
    if expected_sample_rate is not None and sample_rate != expected_sample_rate:
        raise AirPlayAnnotationError("AirPlay 时间轴采样率与实时配置不一致。")
    if (
        expected_window_frames is not None
        and stream_end - stream_start != expected_window_frames
    ):
        raise AirPlayAnnotationError("AirPlay 时间轴窗口帧数与捕获文件不一致。")

    raw_anchors = payload.get("anchors")
    if not isinstance(raw_anchors, list) or len(raw_anchors) > 4_096:
        raise AirPlayAnnotationError("AirPlay 时间轴锚点列表无效。")
    anchors = tuple(
        _parse_anchor(anchor, f"anchors[{index}]")
        for index, anchor in enumerate(raw_anchors)
    )
    if any(
        previous.stream_frame > current.stream_frame
        for previous, current in zip(anchors, anchors[1:])
    ):
        raise AirPlayAnnotationError("AirPlay 时间轴锚点未按时间顺序排列。")
    if any(anchor.stream_frame > stream_end for anchor in anchors):
        raise AirPlayAnnotationError("AirPlay 时间轴锚点超出捕获窗口。")

    return CaptureAnnotation(
        sequence=sequence,
        sample_rate=sample_rate,
        stream_start_frame=stream_start,
        stream_end_frame=stream_end,
        track=_parse_anchor(payload.get("track"), "track"),
        anchors=anchors,
    )


def try_load_capture_annotation(
    path: str | Path,
    **expectations: int | None,
) -> CaptureAnnotation | None:
    try:
        return load_capture_annotation(path, **expectations)
    except AirPlayAnnotationError:
        return None
