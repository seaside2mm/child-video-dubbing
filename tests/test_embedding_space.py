import pytest
from backend.app.pipeline import Pipeline


@pytest.mark.parametrize("left,right,expected", [
    (None, None, False),
    ("", "", False),
    ("ecapa", None, False),
    ("ecapa", "community1", False),
    ("community1", "community1", True),
])
def test_cross_episode_matching_requires_known_same_encoder(left, right, expected):
    assert Pipeline._same_embedding_model(left, right) is expected
