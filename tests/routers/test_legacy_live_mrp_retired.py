def test_retired_worker_never_opens_network():
    import reconcile_worker

    assert "urllib" not in reconcile_worker.__dict__
    assert reconcile_worker.main() == 0
