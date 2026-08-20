from __future__ import annotations

import pytest

from scripts.campaign_prewarm import PrewarmError, image_references


def test_image_references_are_unique_sorted_cleanroom_tags() -> None:
    assert image_references(
        {
            "jobs": [
                {"image": "programbench/two"},
                {"image": "programbench/one"},
                {"image": "programbench/two"},
            ]
        }
    ) == ("programbench/one:task_cleanroom", "programbench/two:task_cleanroom")


@pytest.mark.parametrize("manifest", [{}, {"jobs": []}, {"jobs": [{"image": ""}]}, {"jobs": [{}]}])
def test_image_references_reject_invalid_manifest(manifest: object) -> None:
    with pytest.raises(PrewarmError):
        image_references(manifest)
