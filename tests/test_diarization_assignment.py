from backend.app.adapters.diarization import DiarizationAdapter


def test_database_timestamps_and_real_embedding():
    rows = [{"id": "line", "start_sec": 1.0, "end_sec": 3.0}]
    turns = [{"start": 1.1, "end": 2.9, "speaker": "cluster-0", "embedding": [0.6, 0.8]}]
    result = DiarizationAdapter.assign(rows, turns, "episode")
    assert result[0]["speaker_key"] == "episode:cluster-0"
    assert result[0]["speaker_embedding"] == [0.6, 0.8]


def test_different_episodes_do_not_share_random_cluster_identity():
    turns = [{"start": 0, "end": 1, "speaker": "cluster-0", "embedding": [1, 0]}]
    first = DiarizationAdapter.assign([{"start": 0, "end": 1}], turns, "a")
    second = DiarizationAdapter.assign([{"start": 0, "end": 1}], turns, "b")
    assert first[0]["speaker_key"] != second[0]["speaker_key"]


def test_no_overlapping_turn_is_not_assigned():
    result = DiarizationAdapter.assign([{"start_sec": 10, "end_sec": 11}], [], "a")
    assert result[0]["speaker_key"] is None
