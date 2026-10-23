"""书库索引解析：字段归一、排序、分类与关键字过滤（纯函数，不碰对象存储）。"""
from backend.app.services.library import parse_index


def _raw() -> dict:
    return {
        "library": {"name": "我的书库", "prefix": "library"},
        "generated_at": "2026-10-23T16:00:00+00:00",
        "totals": {"novels": 4, "chapters": 100, "txt_bytes": 1024},
        "categories": [
            {"name": "玄幻奇幻", "slug": "xuanhuan", "count": 2},
            {"name": "文学名著", "slug": "wenxue", "count": 1},
        ],
        "novels": [
            {
                "uid": "80qishu:1",
                "title": "小书",
                "author": "作者甲",
                "category": "玄幻奇幻",
                "category_slug": "xuanhuan",
                "chapter_count": 12,
                "word_count": 3_000,
                "size_bytes": 9_000,
                "status": "已完结",
                "txt": "library/books/玄幻奇幻/1_小书/小书.txt",
                "cover": "library/books/玄幻奇幻/1_小书/cover.jpg",
            },
            {
                "uid": "80qishu:2",
                "title": "大书",
                "author": "Author B",
                "category": "玄幻奇幻",
                "chapter_count": 900,
                "word_count": 9_000_000,
                "txt": "library/books/玄幻奇幻/2_大书/大书.txt",
            },
            # 缺 txt：导入无从下手，解析时直接丢弃
            {"uid": "80qishu:3", "title": "没有原文", "category": "玄幻奇幻"},
            # 空标题：丢弃
            {"uid": "80qishu:4", "title": "  ", "txt": "library/x.txt"},
            {
                "uid": "80qishu:5",
                "title": "水浒传",
                "author": "[元]施耐庵",
                "category": "文学名著",
                "chapter_count": 120,
                "word_count": 780_490,
                "txt": "library/books/文学名著/5_水浒传/水浒传.txt",
            },
        ],
    }


def test_parse_index_drops_entries_without_source_and_normalizes_fields():
    catalog = parse_index(_raw(), loaded_at=1.0)

    assert [b.uid for b in catalog.books] == ["80qishu:2", "80qishu:5", "80qishu:1"]
    assert set(catalog.by_uid) == {"80qishu:1", "80qishu:2", "80qishu:5"}

    small = catalog.by_uid["80qishu:1"]
    assert small.category == "玄幻奇幻"
    assert small.category_slug == "xuanhuan"
    assert small.cover_key == "library/books/玄幻奇幻/1_小书/cover.jpg"

    # 缺省字段要退化成安全默认值，而不是 None
    big = catalog.by_uid["80qishu:2"]
    assert big.category_slug == ""
    assert big.cover_key is None
    assert big.status == ""
    assert big.size_bytes == 0


def test_parse_index_sorts_by_word_count_desc():
    catalog = parse_index(_raw(), loaded_at=1.0)
    words = [b.word_count for b in catalog.books]
    assert words == sorted(words, reverse=True)


def test_query_filters_by_category_and_keyword_case_insensitively():
    catalog = parse_index(_raw(), loaded_at=1.0)

    total, items = catalog.query(category=None, q=None, limit=10, offset=0)
    assert (total, len(items)) == (3, 3)

    total, items = catalog.query(category="玄幻奇幻", q=None, limit=10, offset=0)
    assert total == 2
    assert {b.title for b in items} == {"小书", "大书"}

    # 英文作者名大小写不敏感
    total, items = catalog.query(category=None, q="author b", limit=10, offset=0)
    assert total == 1
    assert items[0].title == "大书"

    # 中文书名子串
    total, items = catalog.query(category=None, q="水浒", limit=10, offset=0)
    assert total == 1
    assert items[0].uid == "80qishu:5"

    # 分页只影响返回片段，不影响 total
    total, items = catalog.query(category=None, q=None, limit=1, offset=1)
    assert total == 3
    assert len(items) == 1


def test_parse_index_keeps_totals_and_categories():
    catalog = parse_index(_raw(), loaded_at=1.0)
    assert catalog.name == "我的书库"
    assert catalog.prefix == "library"
    assert catalog.totals["novels"] == 4
    assert [c["name"] for c in catalog.categories] == ["玄幻奇幻", "文学名著"]
