from scripts.test_luna_then_sol import group_root


def test_campaign_container_namespace_includes_full_artifact_root(tmp_path):
    first=tmp_path/'attempt-a'/'study'
    second=tmp_path/'attempt-b'/'study'
    assert group_root(first,'luna','default').name != group_root(second,'luna','default').name
    assert group_root(first,'luna','default') == group_root(first,'luna','default')
    assert group_root(first,'luna','default').name != group_root(first,'sol','default').name
