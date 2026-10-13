"""Models shared by all page families."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

BASE_URL = "https://elibrary.ru"


class ElibModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_inf_nan="null")


class SearchPage(ElibModel, Generic[T]):
    """One page of results of any list on elibrary.ru."""

    items: list[T] = Field(default_factory=list)
    total: int | None = Field(None, description="Total number of hits reported by elibrary.ru")
    total_in_database: int | None = Field(None, description="Size of the searched collection (the 'из N' figure)")
    page: int = 1
    per_page: int | None = None
    pages: int | None = Field(None, description="Number of result pages, when shown")
    query: dict[str, Any] = Field(default_factory=dict, description="Normalised query that produced this page")
    source_url: str | None = None
    from_cache: bool = False
    fetched_at: float | None = None

    @property
    def has_next(self) -> bool:
        if self.pages is not None:
            return self.page < self.pages
        if self.total is not None and self.per_page:
            return self.page * self.per_page < self.total
        return False


class PublicationHit(ElibModel):
    """A publication row in a result list (search results, author/org publication lists,
    journal issue contents). Contains only what the row shows; use ``get_publication`` for more."""

    id: int = Field(description="eLIBRARY item id (item.asp?id=...)")
    title: str
    authors: list[str] = Field(default_factory=list, description="Author names as shown, e.g. 'Петров В.А.'")
    source: str | None = Field(None, description="Bibliographic line: journal, year, volume, issue, pages, patent number...")
    kind: str | None = Field(
        None,
        description="Best-effort publication type inferred from the row: article, conference, book, chapter, "
        "dissertation, patent, report, preprint, grant, dataset, other",
    )
    year: int | None = None
    pages: str | None = Field(None, description="Page range when shown in its own column (issue contents)")
    section: str | None = Field(None, description="Rubric / section heading the row belongs to (issue contents)")
    citations: int | None = Field(None, description="Citations in RSCI (РИНЦ) shown in the row")
    position: int | None = Field(None, description="Row number on the page")
    versions: list[int] = Field(
        default_factory=list,
        description="eLIBRARY ids of the other versions of this publication listed under the row ('Версии:', "
        "e.g. the English translation); their bibliographic data is not part of `source`",
    )
    url: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.url:
            self.url = f"{BASE_URL}/item.asp?id={self.id}"
