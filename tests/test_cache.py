from app.cache import ResponseCache


def test_hit_miss_and_key_independent_of_dict_order(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.get("m", {"a": 1, "b": 2}) is None

    cache.put("m", {"a": 1, "b": 2}, {"answer": 42})

    assert cache.get("m", {"b": 2, "a": 1}) == {"answer": 42}
    assert cache.get("other-model", {"a": 1, "b": 2}) is None
    assert cache.get("m", {"a": 1, "b": 3}) is None


def test_disabled_cache_never_reads_or_writes(tmp_path):
    cache = ResponseCache(tmp_path / "c", enabled=False)
    cache.put("m", {"a": 1}, {"x": 1})
    assert cache.get("m", {"a": 1}) is None
    assert not (tmp_path / "c").exists()


def test_corrupt_entry_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("m", "payload", {"x": 1})
    (path,) = tmp_path.rglob("*.json")
    path.write_text("{truncated")
    assert cache.get("m", "payload") is None


def test_model_ids_with_slashes_are_safe_directory_names(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("meta-models/muse-spark-open-1026", "p", {"x": 1})
    assert cache.get("meta-models/muse-spark-open-1026", "p") == {"x": 1}
