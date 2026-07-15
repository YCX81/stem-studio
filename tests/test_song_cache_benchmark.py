from tools.benchmark_song_cache import run_benchmark


def test_real_geometry_song_cache_benchmark_stays_inside_realtime_deadline() -> None:
    result = run_benchmark(duration_seconds=12, repeats=2)

    assert result["tracks"] == 6
    assert result["cache_bytes"] > 14_000_000
    assert result["cold_total_seconds"] < result["realtime_deadline_seconds"]
    assert result["warm_max_seconds"] < result["realtime_deadline_seconds"]
