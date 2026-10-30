"""Реестр источников без БД: python -m scripts.test_sources"""

import sys
import types

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None))

from app.workflows.sources import parse_authors, query_terms


def test_authors_from_json_and_string() -> None:
    assert parse_authors('["Новицкий В. В.", "Гольдберг Е. Д."]', "ignored") == ["Новицкий В. В.", "Гольдберг Е. Д."]
    assert parse_authors(None, "Новицкий В. В., Гольдберг Е. Д.; Уразова О. И.") == [
        "Новицкий В. В.",
        "Гольдберг Е. Д.",
        "Уразова О. И.",
    ]
    assert parse_authors("не json", "Автор А.") == ["Автор А."]
    assert parse_authors(None, "") == []


def test_query_terms() -> None:
    assert query_terms("Механизм отёка и асцит") == ["механизм", "отёка", "асцит"]
    assert query_terms(None) == []
    assert len(query_terms("а б в слово " * 10)) <= 8


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
