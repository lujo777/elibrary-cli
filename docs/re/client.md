# Фасад клиента: `client.py`, `refdata.py`, реэкспорт пакетов

Заметки области «фасад» (октябрь 2026). Код: `src/elibrary_cli/client.py` (`ElibraryClient`,
`parse_identifier`, `elibrary_target`), `src/elibrary_cli/refdata.py` (справочники форм),
`models/__init__.py`, `parsers/__init__.py`, `queries/__init__.py` (реэкспорт). Тесты —
`tests/test_client.py` (156 тестов, только `FakeSite` из `tests/conftest.py` и сохранённые страницы;
**ни одного запроса к elibrary.ru, Zotero или открытым API**).

Фасад сам ничего не разбирает: он склеивает построители форм (`queries/*`), единственный HTTP-слой
(`http.ElibraryHTTP`: пауза, бюджеты, предохранитель после капчи, кэш, cookies) и чистые парсеры
(`parsers/*`). Свою логику фасад держит только там, где поведение зависит от состояния на сервере
(листание результатов поиска публикаций) или от перенаправлений (короткие адреса идентификаторов).
Капчу фасад не обходит: `CaptchaRequired` пробрасывается как есть, пройти проверку может только
человек (`browser_session` лишь открывает окно браузера с сессией CLI и ждёт).

## 1. Поиск публикаций: серверная пагинация

`query_results.asp` хранит **последний запрос сессии** (см. `docs/reverse-engineering.md` §3 и
`docs/re/publications.md`): запрос выполняется `GET query_results.asp?<форма>&changed=1` (ответ —
страница 1), страницы 2..N — `GET query_results.asp?pagenum=N` текущего запроса сессии.

* Подпись запроса — `search_key(q)`: `queries.publications.search_signature(q)` (16 hex от формы без
  номера страницы). Для «искать в результатах предыдущего запроса» (`in_previous_results`) к подписи
  подмешивается подпись текущего запроса сессии (результат зависит от него; без него — ошибка без
  запроса), для `open_only`/`itembox_id` — имя вошедшего пользователя.
* Состояние: `http.state["query.current"]` = `{"sig": подпись, "ts": время выполнения}` запроса,
  который сейчас «висит» на сервере (строка-подпись старых версий считается устаревшей). Хранится в
  общем SQLite (CLI и MCP-сервер видят одно и то же); стирается при входе/выходе/импорте cookies
  (`auth.py`, `http.set_cookies`), перед каждым новым выполнением запроса и при капче.
  `status()["current_query"]` по-прежнему отдаёт только подпись.
* Запомненному запросу верим, только если его страница 1 есть в кэше (есть с чем сверить итоги) или
  он моложе `TTL_SEARCH` (24 ч): тот же `SUserID` мог выполнить другой запрос в браузере (обычный
  случай после импорта cookies). «Искать в результатах» (`in_previous_results`) тоже требует запрос
  моложе 24 ч.
* Кэш: каждая страница — под ключом `search:<подпись>:<страница>` (`http.get(..., cache_key=...)`),
  срок `TTL_SEARCH` (24 ч). В кэш страница попадает **только после проверки** (не через `http`: запросы
  идут с `TTL_NONE`, затем `http.cache.put`), поэтому «чужая» или битая страница не закэшируется.

Алгоритм `search_publications(q)` (весь под `ElibraryHTTP._lock`, т. е. два потока не перемешают
«выполнить запрос → листать»):

1. Страница `N` есть в кэше → ответ без запросов (`from_cache=True`).
2. `N == 1`, `query.current` другой, или тот же, но без страницы 1 в кэше и старше 24 ч → выполнить
   запрос: `GET query_results.asp` + форма (`changed=1`), `Referer: querybox.asp`; сохранить
   страницу 1, записать `query.current` (`{"sig", "ts"}`). Для `N == 1` на этом всё (1 запрос).
3. `N > 1`: если страница 1 этого запроса известна (из шага 2 или из кэша) и `N > pages` — пустая
   страница без запроса.
4. `GET query_results.asp?pagenum=N`, `Referer: query_results.asp`.
5. Проверка согласованности: подсвеченный номер страницы в пагинаторе равен `N`; «ВСЕГО НАЙДЕНО
   ПУБЛИКАЦИЙ: X» совпадает со страницей 1 того же запроса; пустая страница при `total > (N-1)·100`
   или пустая страница при неизвестной странице 1 (так выглядит сессия, потерявшая запрос) —
   несогласована; `ParseError` — тоже.
6. Несогласованная страница → **один** повторный прогон (шаг 2) и снова шаг 4. Если страница
   несогласована и после свежего прогона — `ElibraryError` (сообщение: сеанс, вероятно, параллельно
   используется в браузере или другом процессе) или исходный `ParseError`.
7. Страница `N`, принятая **без** известной страницы 1 (запрос моложе 24 ч, но страница 1 стёрта
   `cache clear` или пропущена из-за `--refresh`), возвращается, но в кэш не кладётся: её итоги не с
   чем было сверить, и «чужая» страница не должна залипнуть на сутки.

Итого запросов: страница 1 — 1; следующая страница того же запроса — 1; страница N «чужого» запроса
— 2; повтор любой страницы в течение суток — 0. Проверено тестами с имитацией серверного состояния
(`SearchServer` в `tests/test_client.py`): `run → p2`, повтор из кэша, другой запрос → новый прогон,
подмена запроса «за спиной» клиента → `p2 → run → p2`, потерянная сессия, параллельные потоки,
капча, офлайн-режим.

Обёртки: `search_articles` (`types=article`), `search_patents` (`patent`), `search_books` (`book`),
`search_dissertations` (`disser`), `search_conference_papers` (`conf`); `types` можно переопределить.
Сборники трудов конференций и доклады в них — тип `conf` («материалы конференций»), главы книг могут
оказаться и в `book`, и в `conf` — для полноты `types=("book", "conf")`.

## 2. Карточки публикаций

* `get_publication(id)` — `GET item.asp?id=ID`, `TTL_ITEM` (30 дней), стандартный ключ кэша по URL
  (тот же, что читает `fulltext` при проверке вошедшей сессии).
* `get_publication(edn=...)` — сразу `GET item.asp?edn=EDN` (одна страница вместо цепочки
  перенаправлений короткого адреса).
* Свежая карточка кладётся в кэш **под обоими адресами** — `item.asp?id=` и `item.asp?edn=` (EDN из
  карточки), поэтому запрос той же публикации по другому идентификатору бесплатен.
* `get_publication(doi=...)` — см. §3: `GET /<DOI>` без следования перенаправлению; `Location:
  item.asp?id=…` → карточка; `Location` на `doi.org` → `NotFound` «Публикации с DOI … нет в
  eLIBRARY.RU (сайт перенаправляет на doi.org)». Внешние хосты клиент не запрашивает никогда.
* Страница, которую парсер не узнал (`ParseError`/`NotFound`), удаляется из кэша сразу (иначе ошибка
  «залипла» бы на весь срок кэша).
* `get_fulltext_flag(id)` — фрагмент `ispdf.asp?itemid=` (1 запрос на публикацию, только по явному
  вызову, кэш 7 дней).
* `get_publication_any(identifier)` — карточка по **любому** идентификатору (общая точка для CLI
  `elibrary get publication`, MCP `elibrary_get_publication` и `_publication_ref` экспорта / полного
  текста / Zotero; раньше CLI и MCP держали по своей копии `publication_by_identifier`): `int` — ID;
  строку разбирает `parse_identifier` (цифры — ID, если это не валидный ISBN-10/13); EDN → `item.asp?edn=`;
  ID/ссылка на `item.asp` → `item.asp?id=`; DOI/ISBN → короткий адрес (§3). Если идентификатор указывает
  не на публикацию — `WrongTarget` (подкласс `ValueError`; поля `identifier`, `target`, `target_id`,
  `expected`; текст «Идентификатор '…' указывает на автора, а не на публикацию»): для ссылок/префиксов и
  для SPIN, ORCID, ISSN, ROR — **без запроса**, для ISBN/DOI — после ответа сайта. CLI добавляет к
  сообщению нужную команду (`target_hint`), MCP — нужный инструмент с аргументом
  (`elibrary_get_author(author_id=3771)`). `entity_id` CLI бросает тот же `WrongTarget`.

## 3. Идентификаторы: `parse_identifier` (без сети) и `resolve`

| Ввод | kind | Что делает `resolve` |
|---|---|---|
| `46371463`, `id:…`, `author:3771`, `org:2541`, `journal:8610`, `issue:…` | `elibrary_id` | локально, 0 запросов (существование не проверяется) |
| ссылки `item.asp?id=`, `author_profile.asp?id=`/`authorid=`, `author_items.asp?authorid=`, `org_profile.asp?id=`, `org_about.asp?orgsid=`, `org_items.asp?orgsid=`, `title_about[_new].asp?id=`, `title_profile.asp?id=`, `title_items.asp?id=`, `contents.asp?titleid=` (журнал), `contents.asp?id=` (выпуск) | `url` | локально, 0 запросов |
| `FYOAKL`, `edn:…`, `https://elibrary.ru/fyoakl`, `item.asp?edn=` | `edn` | 1 запрос `item.asp?edn=` (карточка кэшируется) |
| `10.xxxx/…`, `doi:…`, `https://doi.org/…`, `https://elibrary.ru/10.…` | `doi` | `GET /<doi>` без перехода |
| ISBN-10/13 с проверкой контрольной цифры (`978-5-…`, `ISBN …`) | `isbn` | `GET /<isbn без дефисов>` |
| `NNNN-NNNN` с неверной контрольной цифрой ISSN, `spin:…`, `https://elibrary.ru/<цифры>` | `spin` | `GET /<8 цифр>` (без дефиса — «принудительно автор») |
| `NNNN-NNNC` с верной контрольной цифрой ISSN (или `X`) | `issn` | `GET /NNNN-NNNC`; если сайт открыл профиль автора, kind становится `spin` |
| `0000-0002-…-…`, `https://orcid.org/…` | `orcid` | `GET /<orcid>` |
| `0xxxxxxNN`, `https://ror.org/…` | `ror` | `GET /<ror>` |

* Голое число — eLIBRARY ID публикации (самый частый случай); SPIN из 8 цифр без дефиса надо писать как
  `spin:12345678`, иначе он будет понят как ID. Короткая ссылка `https://elibrary.ru/<цифры>` понимается
  по-сайтовому — как SPIN.
* Короткий адрес запрашивается с `follow_redirects=False`, разбирается `Page.location`. Цепочка не
  длиннее 4 шагов: если сайт для новой сессии сначала шлёт на `start_session.asp?rpage=…`, клиент
  проходит её вручную (каждый шаг — отдельный запрос через общий throttle), возвращение на тот же
  короткий адрес допускается один раз; перенаправление на главную (`defaultx.asp`) или на внешний хост
  (doi.org, orcid.org) — «нет в eLIBRARY.RU».
* `Location` на `page_captcha.asp` / `ip_blocked.asp` / `ip_restricted.asp` — это тоже «ворота»:
  клиент взводит предохранитель `http` так же, как `http._send` (`CaptchaRequired` / `AccessBlocked` /
  `IpRestricted`), следующих запросов нет. После QA (13.10.2026) это делает и сам `http._send` для любого
  запроса с `follow_redirects=False` (такой 3xx больше не попадает в кэш); проверка в клиенте осталась
  как вторая линия защиты.
* Если короткий адрес ответил страницей 200 (а не перенаправлением), распознаётся карточка публикации
  (`parse_publication`, карточка кладётся в кэш под `item.asp?id=`) или профиль автора (`AuthorID:`).
* Кэш ответов — JSON-«страницы» в том же `PageCache` под ключами `resolve:<kind>:<value>` на
  `TTL_IDENTIFIER` (1 год); промахи — `resolve-miss:<kind>:<value>` на 7 дней (публикация может
  появиться позже). `clear_cache()` очищает и их.

Ответ `resolve`: `{"kind", "value", "target", "id", "url", "from_cache"}` (+ `location` для
перенаправлений, `edn`, если сайт перенаправил на `item.asp?edn=`). `target`: `publication`,
`author`, `journal`, `organization`, `issue`.

## 4. Авторы, организации, журналы

Формы `authors.asp`, `orgs.asp`, `titles.asp`, `author_items.asp` (POST) состояния на сервере не
хранят: страница зависит только от тела формы. Кэш — под ключом `POST <url>?<urlencoded форма>`
(`http.post(..., cache_key=...)`): авторы/организации/журналы-поиск — `TTL_SEARCH`, списки публикаций
автора — `TTL_PROFILE`. Тела запросов в тестах побайтно совпадают с живыми запросами, которыми были
получены фикстуры (Петров; МГУ; «вычислительные технологии»).

* `search_authors(q)`: сайт фильтрует по организации **только** по `orgid` (живьём 13.10.2026: `orgname`
  без `orgid` вернул всех Ивановых). Поэтому `org_name` без `org_id` сначала превращается в id одним
  `search_organizations(name=org_name, sort="publications", order="desc")` (кэш 24 ч; повтор поиска и
  следующие страницы авторов новых запросов к `orgs.asp` не делают): точное совпадение нормализованного
  названия или сокращения в скобках, иначе — больше всего публикаций. Запрос модели проверяется **до**
  этого запроса (контекст `CONTEXT_RESOLVE_ORG_NAME` разрешает название как единственный критерий), в
  форму идут `orgid` и название найденной организации. Выбор — в `result.query["org_resolved"]`
  (`id`, `name`, `town`, `publications`, `match`, `candidates`, `query`), ничего не нашлось — `NotFound`
  без поиска авторов. Явный `org_id` всегда главнее `org_name`. Страну-название переводит в код сама
  модель (`AuthorQuery.country`, `models.authors.resolve_country`).
* `get_author(id)` — `author_profile.asp?id=`, `TTL_PROFILE`.
* `get_author_publications(id, page, sort, order, include_references, merge_versions, unlinked)`:
  стандартный вид (стр. 1, по дате, по убыванию, со ссылками, версии объединены) — `GET
  author_items.asp?authorid=ID&show_refs=1` (ссылка из профиля); остальное — POST формы страницы,
  `authorhash` берётся из закэшированной первой страницы (иначе пустой — сервер узнаёт автора по
  `authorid`), `Referer` — первая страница. Живьём проверено 13.10.2026 (страница 2 из 26 AuthorID 17783);
  `total` теперь читается и при «Всего найдена N публикация» (см. `docs/re/authors.md` §3).
* `get_organization(id, with_card=False, with_descriptions=False)` — `org_profile.asp?id=`;
  `with_card=True` тратит ещё один запрос на `org_about.asp?orgsid=` и вызывает `OrgProfile.merge`;
  нераспознанная карточка не роняет ответ, а добавляет запись в `notes`.
* `get_organization_publications(id, page, show_option, show_sotr, show_refs, years, sort, order)` —
  `GET org_items.asp?orgsid=…` (построитель области «организации», вживую не проверен).
* `get_journal(id, english=False, with_indicators=False)` — `title_about_new.asp?id=` (`&langid=2`);
  `with_indicators=True` — ещё `title_profile.asp?id=` и `JournalProfile.with_indicators`;
  нераспознанные показатели — запись в `extra["Показатели (title_profile.asp)"]`.
* `get_journal_issue(journal_id=…)` — последний выпуск `contents.asp?titleid=` (кэш 7 дней, выпуск
  меняется), `get_journal_issue(issue_id=…)` — `contents.asp?id=` (30 дней).

## 5. Полный текст, экспорт, Zotero

* `get_fulltext(id, offset, max_chars, sources, refresh)` — карточка (из кэша, если была) →
  `FulltextRequest.from_publication` → `fulltext.get_fulltext(http=этот клиент, net=общий
  ExternalHTTP)`. `ExternalHTTP` создаётся лениво один на клиента (его пауза по хостам живёт в процессе,
  поэтому один экземпляр на долгоживущий MCP-сервер лучше, чем новый на каждый вызов).
  Анонимная сессия: источник `elibrary` пропускается без запроса.
* `export(ids|строки|модели|SearchPage, fmt, as_text=False, ...)` — формат проверяется **до** запросов;
  строки результатов (`PublicationHit`) экспортируются как есть, без загрузки карточек; `bibtex`/`ris`
  — текст, `csl`/`zotero` — список словарей (или JSON-текст при `as_text=True`). `zotero_mcp=True`
  строит CSL через `exports.to_csl_json(..., zotero_mcp=True)` (только ключи, которые понимает
  `zotero_add_item` zotero-mcp, остальное — строками в `note`); MCP `elibrary_export_citation` делает так
  по умолчанию для `csl_json`, `plain_csl=true` возвращает обычный CSL JSON.
* `zotero_add(ids, collection, tags, with_pdf, dedupe, backend)` — карточки → `ZoteroWriter.add`;
  при `with_pdf=True` для каждой публикации вызывается `get_fulltext`, и если текст взят из PDF,
  файл прикладывается (`pdf_paths`); иначе в результате `pdf_note`. В каждый результат добавлен
  `elibrary_id`. Писатель можно передать в конструктор (`zotero=`), тогда клиент его не закрывает.

## 6. Сессия и кэш

`status()` = `http.status()` + `logged_in`, `current_query`, `breaker_url`; `set_cookies` →
`auth.import_cookies` (возвращает только имена cookies); `check_session`, `login` (по умолчанию
`ELIBRARY_LOGIN`/`ELIBRARY_PASSWORD`; без них — `LoginRequired` без запроса), `logout`,
`browser_session(purpose)` (окно браузера для человека), `clear_session()`; `cache_stats()` (страницы,
байты, тексты `cache_dir/fulltext`, PDF в `downloads`), `clear_cache(fulltext=False,
external=False)` (PDF не удаляются).

## 7. Справочники: `refdata.py`

Списки опций форм лежат в `data/authors_options.json`, `journals_options.json`, `orgs_options.json`
(собраны областями «авторы/журналы/организации»); опции `querybox.asp` (типы, где искать, период,
сортировка, флажки) — константами в `refdata.py`, значения — те, что принимает `PublicationQuery`
(подписи сверены с `tests/fixtures/html/querybox_form.html`).

* Имена списков — `"<область>.<поле>"` (`NAMES`, 30 штук): `publications.types|where|period|sort|order|flags`,
  `authors.codetype|countryid|authorboxid|rubriccode|metrics|sortorder|order`,
  `journals.countryid|rubriccode|language|translated|wos|scopus|white|vak|risc|ftopen|sortorder|order|checkboxes`,
  `orgs.countryid|regionid|sortorder|order`. Сокращения — `ALIASES` (`regions`, `vak`, `wos`,
  `countries` = страны организаций, `rubrics` = рубрики журналов, ...). Разделитель `.`, `/` или `:`.
* Ключ `_meta` в `orgs_options.json` пропускается (это не список).
* Каждая опция — `{"value", "label"[, "count"][, "site_code"][, "form_field"]}`, смысл одинаков во всех
  списках: `value` — то, что принимает поле модели запроса (`vak` → `"k1"`, сортировка авторов →
  `"citations"`, флажок журналов → `"core_rinc"`, типы публикаций → `"article"`; где читаемого значения
  нет — сам код сайта: `RUS`, `73`, `500000`); `site_code` — код формы сайта, если он отличается (`"2"`,
  `"rev"`), из таблиц кодов `models/authors.py` и `models/journals.py`; `form_field` — имя флажка формы
  (`type_article`, `search_morph`, `corerisc`). Прежнее поле `key` (то читаемое значение, то имя поля
  формы) убрано, подробнее — `docs/re/cli_mcp.md`, раздел «Справочники».
* `list_options(name)`, `find_options(name, text, limit=20)` (точное совпадение → начало подписи →
  подстрока, без учёта регистра и «ё»), `describe_options()` (имя, описание по-русски, поле запроса,
  число опций, сокращения).

## 8. Реэкспорт

* `elibrary_cli.models` — все модели `base`, `publications`, `authors`, `organizations`, `journals`
  (+ константы `ALL_TYPES`, `ALL_WHERE`, `DEFAULT_WHERE`); `FullText`, `FulltextRequest`,
  `FulltextAttempt` (`fulltext`) и `SessionInfo` (`auth`) — лениво через модульный `__getattr__`
  (прямой импорт дал бы цикл: `fulltext`/`auth` сами импортируют `models.base`).
* `elibrary_cli.parsers` — все `parse_*`, `parse_publication_row(s)`, `parse_reference_rows`,
  `infer_kind`, `row_year`, `normalize_kind`, `is_captcha_page`, `COLOUR_ACCESS`, `split_issue_label`,
  `journal_id_from_url`.
* `elibrary_cli.queries` — построители и пути всех областей; таблицы `*_CODES` остаются в своих модулях
  (имена пересекаются между областями).
* Wheel: `[tool.hatch.build.targets.wheel] packages = ["src/elibrary_cli"]` включает все файлы пакета,
  `.gitignore` не исключает `data/*_options.json` (исключены только `cookies*.json`,
  `storage_state*.json`) — менять `pyproject.toml` не нужно; сборка wheel в этой среде не запускалась
  (нет hatchling в venv).

## 9. Не проверено вживую

* Точные ответы коротких адресов `/<DOI|ISBN|SPIN|ORCID|ISSN|ROR>`: код статуса, вид `Location`
  (`item.asp?id=` или `?edn=`, `author_profile.asp?id=` или `?authorid=`, `title_about.asp` или
  `title_about_new.asp`, `org_profile.asp` или `org_about.asp`), ответ для неизвестного ISBN/ROR/SPIN,
  нужен ли `start_session.asp` для новой сессии. Код принимает все варианты.
* Что показывает `query_results.asp?pagenum=N`, если сессия потеряла запрос (принято: пустая
  страница или страница другого запроса — оба случая ведут к одному повторному прогону) и при `N`
  больше числа страниц (клиент такие запросы не отправляет).
* `GET` вместо `POST` для выполнения поиска (`changed=1`) взят из заметок RE; браузер шлёт POST.
* Листание `author_items.asp` POST-ом без предварительной первой страницы (пустой `authorhash`).
