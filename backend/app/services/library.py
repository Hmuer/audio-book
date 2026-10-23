"""公共书库（library）：从对象存储读取书目，供「从书库导入」使用。

数据由外部归档器（`/opt/get-book`）写入同一个 COS 桶：

    library/
    ├── index.json                  # 轻量索引：分类 + 每本书的元信息 + txt key
    ├── catalog.json                # 全量（含章节目录，800MB 级，不读）
    └── books/<分类>/<ID_书名>/<书名>.txt

本模块只读 `index.json`（~36MB）：
- 首次使用时下载到本地缓存文件，之后按 TTL 复用，避免每次请求都走 COS；
- 解析成 `LibraryBook` 紧凑结构常驻内存（约 29k 本、几十 MB），
  分类过滤/关键字搜索都是内存操作，毫秒级；
- 导入时按 `txt` key 回源单个 TXT，再走既有的 `import_file` 流程
  （编码探测、落盘、project.status=imported），后续识别完全复用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..core.config import settings
from .project import ProjectResp, import_file
from .storage import get_storage

logger = logging.getLogger(__name__)

#: 书库索引里单本 TXT 的大小上限（目前最大约 54MB）
MAX_BOOK_BYTES = 120 * 1024 * 1024
#: 读取 index.json 的内存上限（留足余量；实际 ~36MB）
MAX_INDEX_BYTES = 256 * 1024 * 1024


class LibraryError(RuntimeError):
    """书库不可用 / 找不到书籍等可预期错误（API 层转 4xx/503）。"""


@dataclass(slots=True)
class LibraryBook:
    uid: str
    title: str
    author: str
    category: str
    category_slug: str
    chapter_count: int
    word_count: int
    size_bytes: int
    status: str
    txt_key: str
    cover_key: str | None
    #: 预拼的关键字（标题 + 作者），搜索时直接 casefold 比较
    search: str

    def to_public(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "author": self.author,
            "category": self.category,
            "category_slug": self.category_slug,
            "chapter_count": self.chapter_count,
            "word_count": self.word_count,
            "size_bytes": self.size_bytes,
            "status": self.status,
        }


@dataclass(slots=True)
class LibraryCatalog:
    name: str
    prefix: str
    generated_at: str
    totals: dict
    categories: list[dict]
    books: list[LibraryBook]
    by_uid: dict[str, LibraryBook]
    loaded_at: float

    def query(
        self, *, category: str | None, q: str | None, limit: int, offset: int,
    ) -> tuple[int, list[LibraryBook]]:
        keyword = (q or "").strip().casefold()
        cat = (category or "").strip()
        matched = [
            b for b in self.books
            if (not cat or b.category == cat)
            and (not keyword or keyword in b.search)
        ]
        return len(matched), matched[offset: offset + limit]


# ---------- 进程内缓存 ----------

_catalog: LibraryCatalog | None = None
_load_lock = asyncio.Lock()


def _cache_path() -> Path:
    return Path(settings.DATA_DIR) / "cache" / "library_index.json"


def _index_key() -> str:
    prefix = (settings.LIBRARY_PREFIX or "library").strip().strip("/")
    return f"{prefix}/index.json" if prefix else "index.json"


def reset_cache() -> None:
    """测试 / 手动刷新用：丢掉进程内缓存，下次请求重新加载。"""
    global _catalog
    _catalog = None


def parse_index(raw: dict, *, loaded_at: float | None = None) -> LibraryCatalog:
    """把 index.json 的 dict 解析成常驻内存的紧凑结构（纯函数，便于测试）。"""
    novels = raw.get("novels") or []
    books: list[LibraryBook] = []
    for item in novels:
        title = (item.get("title") or "").strip()
        author = (item.get("author") or "").strip()
        if not title or not item.get("txt"):
            continue
        books.append(
            LibraryBook(
                uid=str(item.get("uid") or ""),
                title=title,
                author=author,
                category=(item.get("category") or "未分类").strip(),
                category_slug=(item.get("category_slug") or "").strip(),
                chapter_count=int(item.get("chapter_count") or 0),
                word_count=int(item.get("word_count") or 0),
                size_bytes=int(item.get("size_bytes") or 0),
                status=(item.get("status") or "").strip(),
                txt_key=str(item["txt"]),
                cover_key=item.get("cover") or None,
                search=f"{title}\n{author}".casefold(),
            )
        )
    # 默认按体量倒序：书库里都是完结整本，字数多的一般更值得先展示。
    books.sort(key=lambda b: (b.word_count, b.chapter_count), reverse=True)
    library = raw.get("library") or {}
    return LibraryCatalog(
        name=(library.get("name") or "我的书库").strip(),
        prefix=(library.get("prefix") or settings.LIBRARY_PREFIX or "library").strip(),
        generated_at=str(raw.get("generated_at") or ""),
        totals=raw.get("totals") or {},
        categories=raw.get("categories") or [],
        books=books,
        by_uid={b.uid: b for b in books},
        loaded_at=loaded_at or time.time(),
    )


async def _download_index(dest: Path) -> None:
    st = get_storage()
    if not st.archives_remotely:
        raise LibraryError("书库存放在对象存储里，当前未启用对象存储（STORAGE_BACKEND=s3）")
    key = _index_key()
    ok = await st.fetch_to(key=key, dest=dest)
    if not ok:
        raise LibraryError(f"无法从对象存储读取书库索引（{key}），请确认书库已归档")
    size = dest.stat().st_size
    if size > MAX_INDEX_BYTES:
        dest.unlink(missing_ok=True)
        raise LibraryError(f"书库索引异常偏大（{size} 字节），已放弃加载")


async def load_catalog(*, force: bool = False) -> LibraryCatalog:
    """读取书库目录（带内存缓存 + 本地文件 TTL 缓存）。"""
    global _catalog

    ttl = max(int(settings.LIBRARY_INDEX_TTL_SECONDS or 0), 0)
    if _catalog is not None and not force:
        if ttl and time.time() - _catalog.loaded_at < ttl:
            return _catalog
        if not ttl:
            return _catalog

    async with _load_lock:
        if _catalog is not None and not force and ttl and time.time() - _catalog.loaded_at < ttl:
            return _catalog

        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = False
        if path.is_file() and not force:
            age = time.time() - path.stat().st_mtime
            if ttl and age < ttl:
                stale = True  # 命中本地缓存，只是进程内没有
        if not stale:
            try:
                await _download_index(path)
            except LibraryError:
                if not path.is_file():
                    raise
                logger.warning("[library] 索引刷新失败，沿用本地旧缓存 %s", path)
            except Exception as e:  # 网络抖动等：有旧缓存就继续用
                if not path.is_file():
                    raise LibraryError(f"读取书库索引失败: {type(e).__name__}: {e}") from e
                logger.warning("[library] 索引刷新异常，沿用本地旧缓存: %s", e)

        raw = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))
        _catalog = parse_index(raw)
        logger.info(
            "[library] 索引已加载 books=%d categories=%d generated_at=%s",
            len(_catalog.books), len(_catalog.categories), _catalog.generated_at,
        )
        return _catalog


async def get_catalog() -> LibraryCatalog:
    return await load_catalog()


async def find_book(uid: str) -> LibraryBook:
    catalog = await load_catalog()
    book = catalog.by_uid.get(uid)
    if book is None:
        raise LibraryError(f"书库里找不到这本书：{uid}")
    return book


async def import_library_book(project_id: str, uid: str) -> ProjectResp:
    """把书库里的一本书导入项目（等价于上传该 TXT）。"""
    book = await find_book(uid)
    st = get_storage()
    if not st.archives_remotely:
        raise LibraryError("书库存放在对象存储里，当前未启用对象存储（STORAGE_BACKEND=s3）")

    tmp = Path(settings.DATA_DIR) / "cache" / f"library-{uuid.uuid4().hex}.txt"
    try:
        ok = await st.fetch_to(key=book.txt_key, dest=tmp)
        if not ok:
            raise LibraryError(f"从书库下载《{book.title}》失败，请稍后重试")
        size = tmp.stat().st_size
        if size <= 0:
            raise LibraryError(f"《{book.title}》的 TXT 是空文件")
        if size > MAX_BOOK_BYTES:
            raise LibraryError(
                f"《{book.title}》超过 {MAX_BOOK_BYTES // (1024 * 1024)}MB，暂不支持导入"
            )
        data = await asyncio.to_thread(tmp.read_bytes)
    finally:
        tmp.unlink(missing_ok=True)

    filename = f"{book.title}.txt"
    logger.info(
        "[library] 导入 project_id=%s... uid=%s title=%r size=%d",
        project_id[:8], uid, book.title, len(data),
    )
    return await import_file(project_id, data, filename)
