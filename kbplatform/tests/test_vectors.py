import numpy as np
import pytest

from kbplatform.vectors import VectorStore


def test_add_and_search_returns_nearest_first(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1, 2], [[1, 0, 0, 0], [0, 1, 0, 0]])
    hits = vs.search([1, 0, 0, 0], topk=2)
    assert hits[0][0] == 1
    assert hits[0][1] > hits[1][1]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-3)


def test_storage_is_fp16_on_disk(tmp_path):
    """Global Constraint #4: 必须 fp16 存盘。"""
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[0.1, 0.2, 0.3, 0.4]])
    f = tmp_path / "vectors.f16"
    assert f.exists()
    assert f.stat().st_size == 4 * 2


def test_ids_persist_and_reload(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([7, 9], [[1, 0, 0, 0], [0, 1, 0, 0]])
    vs2 = VectorStore(str(tmp_path), dim=4)
    assert vs2.ids() == [7, 9]
    assert vs2.count() == 2


def test_remove_marks_tombstone_and_excludes_from_search(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1, 2], [[1, 0, 0, 0], [0, 1, 0, 0]])
    vs.remove([1])
    assert [i for i, _ in vs.search([1, 0, 0, 0], topk=5)] == [2]
    assert 1 not in vs.ids()


def test_search_with_zero_vector_does_not_crash(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[1, 0, 0, 0]])
    assert vs.search([0, 0, 0, 0], topk=5) == []


def test_reset_clears_everything(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[1, 0, 0, 0]])
    vs.reset()
    assert vs.count() == 0
    assert not (tmp_path / "vectors.f16").exists()


def test_add_rejects_wrong_dimension(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    with pytest.raises(ValueError):
        vs.add([1], [[1, 0, 0]])


def test_add_rejects_length_mismatch(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    with pytest.raises(ValueError):
        vs.add([1, 2], [[1, 0, 0, 0]])


def test_search_on_empty_store_returns_empty(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    assert vs.search([1, 0, 0, 0], topk=5) == []
