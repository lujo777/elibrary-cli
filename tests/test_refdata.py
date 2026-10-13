"""Contract of the reference lists (``refdata.py``): one meaning of every option field in every list.

``value`` is what the query model field named by ``query_field`` accepts (and so the CLI option / MCP
tool parameter); ``site_code`` / ``form_field`` say what the site's form sends. No network."""

from __future__ import annotations

from typing import Any

import pytest

from elibrary_cli import refdata
from elibrary_cli.models.authors import AuthorQuery
from elibrary_cli.models.journals import JournalQuery
from elibrary_cli.models.organizations import OrgQuery
from elibrary_cli.models.publications import PublicationQuery
from elibrary_cli.queries.authors import build_author_search_form
from elibrary_cli.queries.journals import build_journal_search_form
from elibrary_cli.queries.organizations import build_org_search_form
from elibrary_cli.queries.publications import build_search_form

# query model -> (class, minimal valid arguments, form builder)
MODELS: dict[str, tuple[Any, dict[str, Any], Any]] = {
    "PublicationQuery": (PublicationQuery, {"text": "ab"}, build_search_form),
    "AuthorQuery": (AuthorQuery, {"surname": "Иванов"}, build_author_search_form),
    "JournalQuery": (JournalQuery, {}, build_journal_search_form),
    "OrgQuery": (OrgQuery, {"name": "университет"}, build_org_search_form),
}
# form field of a select when it is not the "<field>" part of the list name
SITE_FIELD = {"publications.period": "issues", "publications.sort": "orderby"}
OPTION_KEYS = {"value", "label", "count", "site_code", "form_field"}


def _query(name: str, value: str) -> tuple[Any, Any]:
    model_name, _, field = refdata.describe_options()[refdata.NAMES.index(name)]["query_field"].partition(".")
    model, base, build = MODELS[model_name]
    if field == "<поле>":  # checkbox lists: value is the name of a boolean field
        kwargs: dict[str, Any] = {value: True}
    elif field in ("types", "where"):
        kwargs = {field: [value]}
    else:
        kwargs = {field: value}
    return model(**base, **kwargs), build


@pytest.mark.parametrize("name", refdata.NAMES)
def test_every_option_value_is_accepted_by_its_query_field(name):
    """The documented contract: pass ``value`` to the query model / tool and the site gets ``site_code``
    (or the ``form_field`` checkbox). Before, half of the lists had the site code in ``value``
    (AuthorQuery(sort="2") failed) and the publication lists had the form field in ``key``."""
    options = refdata.list_options(name)
    assert options
    for opt in options:
        assert set(opt) <= OPTION_KEYS, opt
        assert "key" not in opt
        q, build = _query(name, opt["value"])
        form = build(q)
        pairs = list(form.items()) if isinstance(form, dict) else list(form)
        if "form_field" in opt:
            assert (opt["form_field"], "on") in pairs, (name, opt)
        else:
            field = SITE_FIELD.get(name, name.partition(".")[2])
            assert (field, opt.get("site_code", opt["value"])) in pairs, (name, opt)
        if "site_code" in opt:
            assert opt["site_code"] != opt["value"]


def test_options_of_coded_selects():
    assert refdata.list_options("vak")[1] == {"value": "k1", "label": "Перечень ВАК (категория 1)", "count": 772,
                                             "site_code": "2"}
    assert refdata.list_options("authors.sortorder")[2] == {"value": "citations", "label": "по числу цитирований",
                                                            "site_code": "2"}
    assert refdata.list_options("authors.metrics")[0] == {"value": "core", "label": "по ядру РИНЦ", "site_code": "0"}
    assert refdata.list_options("orgs.sortorder")[2] == {"value": "publications", "label": "по числу статей",
                                                         "site_code": "3"}
    assert refdata.list_options("publications.order")[0] == {"value": "desc", "label": "по убыванию",
                                                             "site_code": "rev"}
    # lists whose model takes the site code itself have no site_code
    assert refdata.list_options("author:codetype")[0] == {"value": "SPIN", "label": "SPIN-код"}
    assert refdata.list_options("regions")[0]["value"] == "1943" and "site_code" not in refdata.list_options("regions")[0]


def test_options_of_checkbox_lists():
    assert refdata.list_options("types")[0] == {"value": "article", "label": "статьи в журналах",
                                               "form_field": "type_article"}
    assert refdata.list_options("where")[0]["form_field"] == "where_name"
    assert refdata.list_options("flags")[1] == {"value": "fuzzy", "label": "искать похожий текст (включено по "
                                               "умолчанию)", "form_field": "search_freetext"}
    core = refdata.list_options("journal_flags")[2]
    assert (core["value"], core["form_field"]) == ("core_rinc", "corerisc")
    assert PublicationQuery(text="ab", types=["article"]).types == ["article"]
    assert JournalQuery(core_rinc=True).core_rinc is True


def test_find_options_matches_value_label_and_site_codes():
    assert refdata.find_options("vak", "k2")[0]["site_code"] == "3"
    assert refdata.find_options("vak", "3")[0]["value"] == "k2"           # by the site code
    assert refdata.find_options("vak", "категория 2")[0]["value"] == "k2"
    assert refdata.find_options("types", "type_patent")[0]["value"] == "patent"  # by the form field
    assert refdata.find_options("journal_flags", "corerisc")[0]["value"] == "core_rinc"
    assert refdata.find_options("regions", "новосибирская") == [
        {"value": "51", "label": "Новосибирская область", "count": 333}]
