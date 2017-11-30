from chalicelib.storage import SemiDBMCache


def test_can_cache_through_semidbm(tmpdir):
    db = SemiDBMCache(str(tmpdir))
    for i in range(20):
        db[str(i)] = {'count': i}
    for i in range(20):
        assert db[str(i)] == {'count': i}


def test_check_frequency_noop_when_below_size_threshold(tmpdir):
    db = SemiDBMCache(str(tmpdir), check_frequency=2)
    for i in range(20):
        db[str(i)] = {'count': i}
    for i in range(20):
        assert db[str(i)] == {'count': i}


def test_cache_noop_when_max_size_reached(tmpdir):
    db = SemiDBMCache(str(tmpdir), check_frequency=1, max_filesize=100)
    for i in range(20):
        db[str(i)] = {'count': i}
    assert db[b'1'] == {'count': 1}
    # We've exhausted the max_filesize so any setitems will be noops.
    db[b'100'] = {'count': 100}
    assert b'100' not in db
