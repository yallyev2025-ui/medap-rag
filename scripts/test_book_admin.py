"""Управление книгой с сайта без БД: python -m scripts.test_book_admin"""

import sys
import types

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None, embed_passages=None, limited_threads=None))

from app.workflows.book_admin import VersionReport, switch_warning


def report(version, active, chunks, pages, **kw):
    return VersionReport(version=version, active=active, chunks=chunks, tables=kw.get("tables", 0),
                         needs_review=kw.get("review", 0), pages_covered=pages, figures=kw.get("figures", 0))


def test_new_version_with_same_coverage_is_fine() -> None:
    reports = [report(1, True, 1000, 400), report(2, False, 1100, 400, tables=20, figures=80)]
    assert switch_warning(reports, 2) is None


def test_worse_coverage_is_warned_with_numbers() -> None:
    reports = [report(1, True, 1000, 400), report(2, False, 1000, 300)]
    text = switch_warning(reports, 2)
    assert text and "300 вместо 400" in text


def test_much_fewer_chunks_is_warned() -> None:
    reports = [report(1, True, 1000, 400), report(2, False, 700, 400)]
    assert "700 вместо 1000" in switch_warning(reports, 2)


def test_rollback_to_old_version_is_warned_only_if_old_is_worse() -> None:
    reports = [report(1, False, 1000, 400), report(2, True, 1100, 410)]
    assert switch_warning(reports, 1) is None  # откат на не худшую версию — без замечаний


def test_unknown_version_and_same_version() -> None:
    reports = [report(1, True, 1000, 400)]
    assert "нет версии 5" in switch_warning(reports, 5)
    assert switch_warning(reports, 1) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
