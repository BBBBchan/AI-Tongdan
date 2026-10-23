"""API contracts for archived live fragments and the two playback clocks."""
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from clipper.config import Settings
from clipper.server import create_app


@pytest.fixture
def archive(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, import_roots=(tmp_path,)))
    store = app.state.store
    session = store.create_session(title="归档直播测试", mode="live", status="live",
                                   duration=33.25, platform="bilibili")
    # Initial recording delay, continuous fragments, a six-second outage,
    # then a contiguous new recorder generation with a fresh timestamp origin.
    records = [(2, 8, 1), (8, 14, 1), (20, 26, 1), (26, 33.25, 2)]
    paths = []
    for i, (start, end, generation) in enumerate(records):
        source = tmp_path / f"fragment-{i}.ts"
        source.write_bytes(f"archived-fragment-{i}".encode())
        paths.append(source)
        store.add_segment(session["id"], start, end, source, {"generation": generation})
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, sid=session["id"], paths=paths,
                              segments=store.segments(session["id"]), tmp=tmp_path)


def test_archive_playlist_keeps_old_generations_and_marks_both_kinds_of_discontinuity(archive):
    a = archive
    response = a.client.get(f"/api/sessions/{a.sid}/hls/index.m3u8")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert response.headers["cache-control"] == "no-store"
    lines = response.text.splitlines()
    assert "#EXT-X-TARGETDURATION:8" in lines
    assert "#EXT-X-MEDIA-SEQUENCE:0" in lines
    assert "#EXT-X-ENDLIST" not in lines
    uris = [line for line in lines if line and not line.startswith("#")]
    assert uris == [f"{segment['id']}.ts" for segment in a.segments]
    assert [line for line in lines if line.startswith("#EXTINF:")] == [
        "#EXTINF:6.000000,", "#EXTINF:6.000000,", "#EXTINF:6.000000,", "#EXTINF:7.250000,"
    ]
    assert lines.count("#EXT-X-DISCONTINUITY") == 2
    # A gap without a generation change and a generation change without a
    # gap both require a discontinuity before their EXTINF line.
    for index in (2, 3):
        uri_index = lines.index(uris[index])
        assert lines[uri_index - 2] == "#EXT-X-DISCONTINUITY"
    assert lines[lines.index(uris[1]) - 2] != "#EXT-X-DISCONTINUITY"

    # The oldest generation remains fetchable even after reconnecting.
    first = a.client.get(f"/api/sessions/{a.sid}/hls/{uris[0]}")
    assert first.status_code == 200
    assert first.content == b"archived-fragment-0"
    assert first.headers["content-type"].startswith("video/mp2t")


def test_session_clock_and_contiguous_playback_clock_map_across_initial_delay_and_gap(archive):
    a = archive
    response = a.client.get(f"/api/sessions/{a.sid}")
    assert response.status_code == 200
    value = response.json()
    assert value["media_url"] == f"/api/sessions/{a.sid}/hls/index.m3u8"
    mapping = value["playback_segments"]
    assert mapping == [
        {"media_start": 0, "media_end": 6, "start": 2, "end": 8},
        {"media_start": 6, "media_end": 12, "start": 8, "end": 14},
        {"media_start": 12, "media_end": 18, "start": 20, "end": 26},
        {"media_start": 18, "media_end": 25.25, "start": 26, "end": 33.25},
    ]
    assert value["session"]["duration"] == 33.25
    assert mapping[-1]["media_end"] == 25.25

    # Validate consumers can round-trip meaningful source moments without
    # mistaking the playback's 12-second position for session second 12.
    for source_time, playback_time in [(3, 1), (10, 8), (21.5, 13.5), (29, 21)]:
        source_part = next(x for x in mapping if x["start"] <= source_time < x["end"])
        assert source_part["media_start"] + source_time - source_part["start"] == playback_time
        playback_part = next(x for x in mapping if x["media_start"] <= playback_time < x["media_end"])
        assert playback_part["start"] + playback_time - playback_part["media_start"] == source_time
    assert not any(x["start"] <= 17 < x["end"] for x in mapping), "The outage must not be represented as saved footage."


@pytest.mark.parametrize("status,closed", [
    ("live", False), ("reconnecting", False), ("waiting", False),
    ("stopped", True), ("error", True), ("limit_reached", True),
])
def test_endlist_tracks_whether_more_archive_fragments_can_arrive(archive, status, closed):
    a = archive
    a.store.update_session(a.sid, status=status)
    response = a.client.get(f"/api/sessions/{a.sid}/hls/index.m3u8")
    assert response.status_code == 200
    assert ("#EXT-X-ENDLIST" in response.text) is closed


def test_unknown_removed_and_foreign_fragments_are_not_served(archive):
    a = archive
    base = f"/api/sessions/{a.sid}/hls/"
    assert a.client.get(base + "0000000000000000.ts").status_code == 404
    assert a.client.get(base + "credentials.json").status_code == 404
    a.paths[1].unlink()
    assert a.client.get(base + a.segments[1]["id"] + ".ts").status_code == 404

    other = a.store.create_session(mode="live", duration=10)
    source = a.tmp / "other-session.ts"
    source.write_bytes(b"belongs to a different session")
    a.store.add_segment(other["id"], 0, 10, source)
    foreign_id = a.store.segments(other["id"])[0]["id"]
    assert a.client.get(base + foreign_id + ".ts").status_code == 404
    assert a.client.get(f"/api/sessions/missing/hls/index.m3u8").status_code == 404


def test_empty_live_archive_and_file_sessions_do_not_claim_hls_readiness(archive):
    a = archive
    empty = a.store.create_session(mode="live", duration=0)
    file_session = a.store.create_session(mode="file", duration=6, media_path=str(a.paths[0]))
    a.store.add_segment(file_session["id"], 0, 6, a.paths[0])
    for sid in (empty["id"], file_session["id"]):
        assert a.client.get(f"/api/sessions/{sid}/hls/index.m3u8").status_code == 404
    empty_detail = a.client.get(f"/api/sessions/{empty['id']}").json()
    assert empty_detail["playback_segments"] == []
    assert empty_detail["media_url"] is None
    file_detail = a.client.get(f"/api/sessions/{file_session['id']}").json()
    assert file_detail["playback_segments"] == []
    assert file_detail["media_url"] == f"/api/sessions/{file_session['id']}/media"
