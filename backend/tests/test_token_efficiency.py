"""Token-efficiency helpers: cached workspace tree and dynamic output budget."""
from app import codegen


def test_tree_cache_reuses_unchanged_layout(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x")
    t1 = codegen._existing_tree(str(tmp_path))
    assert "app/main.py" in t1
    # Content-only change: layout fingerprint unchanged, string identical.
    (tmp_path / "app" / "main.py").write_text("yy")
    assert codegen._existing_tree(str(tmp_path)) == t1
    # New file: directory mtime/count change busts the cache.
    (tmp_path / "app" / "new.py").write_text("z")
    t3 = codegen._existing_tree(str(tmp_path))
    assert "app/new.py" in t3


def test_tree_cache_stamps_entry(tmp_path):
    (tmp_path / "a.py").write_text("x")
    codegen._existing_tree(str(tmp_path))
    assert str(tmp_path) in codegen._tree_cache


def test_output_budget_small_fix_task():
    task = {"title": "Fix typo in header label", "description": "", "points": 1}
    assert codegen._output_budget(task) == 4000


def test_output_budget_low_points_even_without_keyword():
    assert codegen._output_budget({"title": "Add icon", "description": "", "points": 2}) == 4000


def test_output_budget_large_task_keeps_full_envelope():
    task = {"title": "Implement conversation history",
            "description": "full feature with storage", "points": 5}
    assert codegen._output_budget(task) == 8000


def test_output_budget_handles_missing_fields_and_rows():
    # Missing points must not raise; ambiguous task keeps the safe default.
    assert codegen._output_budget({"title": "Build portal", "description": None}) == 8000
