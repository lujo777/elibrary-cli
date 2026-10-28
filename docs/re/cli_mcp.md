# CLI `elibrary` и MCP-сервер `elibrary-mcp`

Заметки области «CLI + MCP» (октябрь 2026). Код: `src/elibrary_cli/cli/` (typer) и
`src/elibrary_cli/mcp_server.py` (официальный MCP SDK 2.3, `MCPServer`). Тесты: `tests/test_cli.py`
(`typer.testing.CliRunner`) и `tests/test_mcp.py` (in-memory `mcp.Client`). Оба слоя «тонкие»: ничего
не разбирают и не ходят в сеть сами, всё делает `ElibraryClient` (`client.py`). Ни один тест не
обращается к elibrary.ru, Zotero или открытым API: клиент строится поверх `FakeSite` из
`tests/conftest.py` с сохранёнными страницами.

## 1. CLI: устройство

| Файл | Что внутри |
|---|---|
| `cli/__init__.py` | корневое приложение `app`, корневые параметры `--offline`, `--refresh`, `--min-delay`, `--version`; `main()` — точка входа `elibrary` / `elibrary-cli` |
| `cli/common.py` | глобальные параметры (`OPTIONS`), `client_factory` / `zotero_factory` (подменяются в тестах), `open_client()`, `run()`, сопоставление ошибок с кодами выхода, вывод JSON/JSONL, `read_value()` (`@файл`, `-`), `entity_id()`, `target_hint()` (подсказка нужной команды к `client.WrongTarget`); карточку публикации по любому идентификатору даёт `ElibraryClient.get_publication_any()` |
| `cli/render.py` | таблицы rich для `--format table` (карточки и списки), русские подписи ключей (`LABELS`) |
| `cli/search.py` | `elibrary search articles|patents|books|dissertations|conferences|publications|authors|orgs|journals` |
| `cli/get.py` | `elibrary get publication|author|author-pubs|org|org-pubs|journal|issue` |
| `cli/session.py` | `elibrary auth status|check|cookies|captcha|login|logout|clear`, `elibrary cache stats|clear` |
| `cli/tools.py` | `elibrary resolve`, `fulltext`, `export`, `zotero add|status|authorize`, `refdata`, `mcp` |

Порядок работы команды: корневой callback записывает `--offline/--refresh/--min-delay` в
`common.OPTIONS` → команда собирает параметры запроса → `run(lambda c: ...)` открывает клиент
(`client_factory(make_settings())`, где `make_settings()` = `Settings.from_env()` + явные корневые
параметры; те же параметры накладываются и на `client.http.settings`), выполняет действие, закрывает
клиент → `emit()` печатает результат.

### Вывод

* Данные — в stdout, сообщения (ошибки, подсказки «Следующая страница: --page 2», предупреждения) — в
  stderr (`typer.echo(..., err=True)`, без rich-разметки, чтобы `[browser]` и квадратные скобки в
  названиях не съедались).
* `--format/-f table|json|jsonl`. Без параметра: `table`, если stdout — терминал, иначе `json`.
  JSON — `model_dump(mode="json")` тех же pydantic-моделей, что возвращает MCP (`ensure_ascii=False`,
  отступ 2). JSONL — по строке на элемент `items` страницы результатов / элемент списка; если элементов
  нет (поиск без результатов, пустой список), stdout остаётся **пустым** — раньше печаталась пустая строка
  `"\n"`, на которой читатели вида `for line in f: json.loads(line)` падали с `JSONDecodeError`.
* Исключения: `fulltext` по умолчанию печатает текст (`-f text|json`); `export` использует `-f` для
  формата записей (`bibtex|ris|csl|zotero`, по умолчанию `bibtex`).
* Таблицы: компактные столбцы (ID, название, авторы — первые три + «и др. (N)», источник, год,
  цитирования); у выпуска журнала — разделы строками и столбец «Стр.»; карточки — двухколоночные
  таблицы «поле — значение» (пустые поля пропускаются). Ячейки, **заголовки таблиц** (название
  публикации, ФИО автора, название организации/журнала, сводка страницы) и заголовки столбцов
  `records_table` передаются как `rich.text.Text` (`render._title`), поэтому скобки не читаются как
  разметка rich. Раньше заголовок-строка разбирался как markup: «Ionic liquid [bmim][BF4] … [in Russian]»
  терял «[bmim]» и «[in Russian]», а «Fe/Cr [/] multilayers» ронял `get publication -f table` с
  `rich.errors.MarkupError` (traceback: `emit()` вне `handle_errors`).
  rich берёт ширину терминала; при выводе не в терминал — 80 колонок или `COLUMNS`.
* Windows: при выводе не в терминал stdout переключается на UTF-8 (`reconfigure(encoding="utf-8",
  errors="replace")`); `main()` делает то же для stderr.

### Коды выхода

| Код | Когда |
|---|---|
| 0 | успех |
| 1 | неверные аргументы (`ValueError`, ошибки валидации pydantic — сообщение «Неверные параметры: …»), нет optional-зависимости (`DependencyMissing.exit_code`) |
| 2 | ошибки eLIBRARY.RU (`ElibraryError.exit_code`: капча, блокировка, нужен вход, не найдено, лимит, нет в кэше при `--offline`, ошибка разбора); ошибки использования typer (неизвестная опция/значение); `auth captcha`/`auth login --browser` без подтверждённого результата; `zotero add` с неудачными записями |
| 3 | `NetworkError` (нет связи, 5xx после повтора) |
| 130 | Ctrl+C |

Для `CaptchaRequired` печатается полный текст помощи (`errors.CAPTCHA_HELP` с адресом
`page_captcha.asp?rpage=…`): `elibrary auth captcha` или ручной путь через `copy(document.cookie)` +
`elibrary auth cookies`. Повторная команда при открытом предохранителе падает сразу, без запроса.
`elibrary auth status` при открытом предохранителе дополнительно пишет в stderr, что делать (после
блокировки IP — написать в support@elibrary.ru и не повторять запросы; `auth check` не предлагается).

### Идентификаторы

* `get publication`: число → `get_publication(id)`; EDN → `edn=`; DOI (в т. ч. `https://doi.org/…`)
  → `doi=`; ссылка `item.asp?id=` → id; ISBN и прочее → `resolve()`; ссылка на страницу другого типа →
  код 1 с подсказкой нужной команды.
* `get author|author-pubs|org|org-pubs|journal`, `get issue --journal-id`: число, ссылка на страницу,
  префикс (`author:3771`) — без запросов; SPIN/ORCID/ISSN/ROR — через `resolve()` (1 запрос, кэш на год).
  EDN, DOI и ISBN указывают только на публикации, поэтому `entity_id()` отклоняет их **без запроса**
  (`WrongTarget`, код 1, «… указывает на публикацию, а не на автора (используйте `elibrary get
  publication`)»). Любое слово из 6 латинских букв разбирается как EDN (`Ivanov`, `Nature`, `Yandex`):
  для такого ввода добавляется подсказка «Шесть латинских букв читаются как EDN публикации; если
  'Ivanov' — фамилия, используйте поиск: `elibrary search authors Ivanov`» (`search orgs` / `search
  journals` для организаций и журналов). Раньше уходил запрос `item.asp?edn=IVANOV` и печаталось
  «Страница не найдена» (код 2), а повтор стоил ещё один просмотр (ненайденное не кэшируется).
* `fulltext`, `export`, `zotero add` передают строки как есть в `ElibraryClient` (он понимает id, EDN,
  DOI, ссылки).
* `zotero add` **до** чтения карточек проверяет Zotero (`zotero_factory(...).ping()`, только 127.0.0.1;
  та же проверка, что у `elibrary_zotero_add`): без доступного способа записи (`--backend auto`) или
  если явно выбранного способа нет среди `available_backends` — код 2, «Нет доступного способа записи в
  Zotero; ничего не сохранено, запросов к eLIBRARY.RU не было.» + подсказки `ping()`. Исключение: при
  `ZOTERO_LOCAL=false` `ping()` не показывает локальные способы, и явный `--backend local|connector`
  проверяет уже `ZoteroWriter.add()`. Раньше команда сначала запрашивала каждую карточку (и полные
  тексты с `--with-pdf`) и только потом узнавала, что Zotero не запущен.

### Сессия и капча (без автоматизации проверки)

* `auth captcha` — `ElibraryClient.browser_session("captcha")`: видимое окно браузера с cookies и
  User-Agent CLI, человек проходит проверку сам, код только ждёт и сохраняет cookies. Без extra
  `[browser]` (Playwright) — код 1, сообщение `DependencyMissing` и инструкция ручного пути с адресом
  проверки из предохранителя.
* `auth cookies <строка|@файл|->` — `set_cookies()`; выводятся только имена cookies и предупреждения
  (нет `SUserID`, не передан `--user-agent`, пауза не снята), значения не печатаются. `--merge` —
  добавить к сохранённым. Паузу после капчи снимает только строка с `SUserID`; паузу после блокировки IP
  импорт не снимает (правила — `docs/re/auth.md` §4).
* `auth check [--force]` — `check_session()`: один запрос. Без `--force` пауза после капчи снимается,
  только если после неё импортированы cookies с `SUserID`, пауза после блокировки — никогда (ошибка без
  запроса). `--force` снимает любую паузу — только по решению человека; у MCP такого параметра нет.
* `auth login` — имя из `ELIBRARY_LOGIN` или запроса, пароль из `ELIBRARY_PASSWORD` или скрытого
  ввода (`typer.prompt(hide_input=True, err=True)`); пароль нигде не печатается. `--browser` — вход в
  окне браузера (`browser_session("login")`).
* `auth status` / `auth clear` / `cache …` / `refdata` — без запросов к сайту.

## 2. MCP-сервер

`mcp = MCPServer("elibrary", instructions=INSTRUCTIONS, version=__version__, lifespan=_lifespan)`.
`INSTRUCTIONS` — английский текст ≤ 2048 символов: что такое eLIBRARY.RU, какой инструмент когда,
стоимость запросов (ограниченный сайт, капча после ~10 просмотров, узкие запросы, не листать много
страниц, не перебирать много записей), что делать при капче, запросы можно писать по-русски.

* Все инструменты — обычные `def` (SDK выполняет их в рабочем потоке), параметры
  `Annotated[..., Field(description=...)]` с `Literal`-перечислениями из моделей, результат — pydantic-
  модель (она же output schema). У всех `meta={"anthropic/maxResultSizeChars": 200000}`.
* Аннотации: `read_only_hint=True, open_world_hint=True` у всех, кроме
  `elibrary_set_cookies` (`read_only=False, destructive=True, idempotent=True, open_world=False`:
  заменяет сохранённые cookies сессии), `elibrary_session_check` (`read_only=False, destructive=False,
  idempotent=False, open_world=True`: отправляет запрос и меняет локальное состояние — `account`,
  предохранитель) и `elibrary_zotero_add` (`read_only=False, destructive=False, open_world=True`);
  `elibrary_session_status` и `elibrary_reference_data` — `open_world_hint=False` (сеть не трогают).
* Агент не может сам снять паузу: `elibrary_session_check` никогда не передаёт `force`, после бана
  падает без запроса, после капчи — пока пользователь не передал cookies с `SUserID`;
  `elibrary_set_cookies` снимает паузу после капчи только строкой с `SUserID`, после бана — никогда
  (в ответе `breaker_open`, `breaker_gate`, `session_cookie_imported` и русское `message`). В
  `INSTRUCTIONS` есть правило для бана: остановиться на сутки, сообщить пользователю
  (support@elibrary.ru), не пытаться снять паузу.
* Ошибки: `CaptchaRequired` → `ToolError` с английской инструкцией для агента (остановиться, попросить
  пользователя выполнить `elibrary auth captcha` или пройти проверку по адресу и передать
  `copy(document.cookie)` в `elibrary_set_cookies`, никогда не решать капчу) + русский текст помощи с
  адресом проверки; прочие `ElibraryError`, `ValueError`, `ValidationError` → `ToolError(str(err))`.
  SDK добавляет префикс «Error executing tool <name>: ».
* Общий клиент: один `ElibraryClient` на процесс, создаётся лениво под `RLock` (`get_client()`,
  фабрика `client_factory`), закрывается при остановке сервера (lifespan) — только если его создал
  сервер; `set_client()` ставит готовый клиент (тесты, встраивание), `configure(settings)` задаёт
  настройки (`elibrary mcp` передаёт корневые параметры CLI).
* `elibrary_zotero_add` сначала проверяет Zotero (`zotero_factory().ping()`), и только если есть
  доступный способ записи, читает карточки и пишет; иначе `ToolError` с подсказками и альтернативой:
  `elibrary_export_citation(format='csl_json')` → `zotero_add_item(source=…, source_type='csl_json')`
  zotero-mcp. Так при выключенном Zotero не тратятся запросы к elibrary.ru.
* `main()` — argparse `--transport stdio|streamable-http --host --port` → `run()`.

### Инструменты

| Инструмент | Метод клиента | Результат |
|---|---|---|
| `elibrary_search_publications` | `search_publications` (types, where, годы, period, sort, order, morphology, fuzzy, fulltext_only, open_only, authors, journal_ids, rubric_codes, page) | `SearchPage[PublicationHit]` |
| `elibrary_search_articles` / `_patents` / `_books` | `search_articles` / `search_patents` / `search_books` | `SearchPage[PublicationHit]` |
| `elibrary_search_authors` | `search_authors` (surname, identifier_type + identifier, country — ISO3 или название по-русски/по-английски, разбирает `AuthorQuery`, town, org_id, org_name — без org_id превращается в org_id одним поиском организаций, выбранная — в `query.org_resolved`, rubric_code, metrics, sort, order, page) | `SearchPage[AuthorHit]` |
| `elibrary_search_organizations` | `search_organizations` (name, town, region — название или id, country, sort, order) | `SearchPage[OrgHit]` |
| `elibrary_search_journals` | `search_journals` (name, country, rubric, language, vak, scopus, wos, white_list, risc, open_access, translated, флажки) | `SearchPage[JournalHit]` |
| `elibrary_get_publication` | `get_publication_any` (id / EDN / DOI / ссылка / ISBN; `WrongTarget` → подсказка нужного инструмента) | `Publication` |
| `elibrary_get_author`, `elibrary_get_author_publications` | `get_author`, `get_author_publications` | `AuthorProfile`, `AuthorPublicationsPage` |
| `elibrary_get_organization`, `elibrary_get_organization_publications` | `get_organization(with_card)`, `get_organization_publications` (show: all/rinc/core/vak/rsci/wos/scopus/wos_or_scopus/doi/isbn → коды `show_option`) | `OrgProfile`, `OrgPublicationsPage` |
| `elibrary_get_journal`, `elibrary_get_journal_issue` | `get_journal(english, with_indicators)`, `get_journal_issue` | `JournalProfile`, `IssueContents` |
| `elibrary_resolve_identifier` | `resolve` | `IdentifierResolution` |
| `elibrary_get_fulltext` | `get_fulltext` (offset, max_chars 500…100000, sources, refresh) | `FullText` (`next_offset`) |
| `elibrary_export_citation` | `export(as_text=True)`; csl_json/bibtex/ris/zotero_json; csl_json по умолчанию в варианте для zotero-mcp (`zotero_mcp=True`), `plain_csl=true` — обычный CSL JSON | `CitationExport` (`text`, `zotero_mcp_source_type`) |
| `elibrary_zotero_add` | `zotero_add` | `ZoteroAddResult` |
| `elibrary_session_status` / `elibrary_session_check` / `elibrary_set_cookies` | `status` / `check_session` / `set_cookies` | `SessionStatus` / `SessionInfo` / `CookieImportResult` |
| `elibrary_reference_data` | `refdata.describe_options/list_options/find_options` | `ReferenceData` |

### Справочники (`elibrary refdata`, `elibrary_reference_data`)

Опция справочника — `{"value", "label"[, "count"][, "site_code"][, "form_field"]}`, и смысл полей одинаков
во всех 30 списках:

* `value` — то, что принимает поле модели запроса из `query_field` (и одноимённый или близкий параметр
  CLI/MCP: `code_type` → `identifier_type`, `white` → `white_list`, `ftopen` → `open_access`, `rubric_code`
  → `rubric`, `region_id` → `region`): читаемое значение, где оно есть (`journals.vak` → `k1`,
  `authors.sortorder` → `citations`, `publications.types` → `article`, `publications.order` → `desc`),
  иначе код сайта (`RUS`, `73`, `500000`, `SPIN`); у списков флажков (`publications.flags`,
  `journals.checkboxes`) — имя булева поля (`fuzzy`, `core_rinc`);
* `site_code` — код, который отправляет форма сайта, только если он отличается от `value` (`k1` → `2`,
  `desc` → `rev`); справочно, построители запросов переводят `value` сами;
* `form_field` — имя флажка формы для `publications.types|where|flags` (`type_article`, `where_name`,
  `search_morph`) и `journals.checkboxes` (`corerisc`).

Раньше смысл был перевёрнут между списками: у `publications.*` в `key` лежало имя поля формы, а в `value`
— значение модели, у `authors.sortorder|metrics`, `orgs.sortorder` и фильтров журналов в `value` был код
сайта, а значение модели — в `key`; описание `ReferenceOption` в MCP («value — site code», «key — readable
value») было неверно для половины списков, и `AuthorQuery(sort="2")`, `PublicationQuery(types=["type_article"])`
не проходили валидацию. Поле `key` убрано. `find_options` ищет точное совпадение по `value`, `label`,
`site_code` и `form_field` (так `refdata vak --find 2` по-прежнему находит «категория 1»). В таблице CLI
столбцы подписаны «Значение», «Название», «Количество», «Код на сайте», «Поле формы». Контракт проверяет
`tests/test_refdata.py`: каждое `value` каждого списка проходит валидацию своей модели, а построитель
формы отправляет `site_code` (или флажок `form_field`).

## 3. Что проверено и что нет

Проверено тестами (fixtures 2026-10-13): JSON/JSONL/таблицы всех основных команд, параметры запросов
(поля GET/POST), кэширование, вторая страница поиска (запрос + `pagenum`), несогласованная страница,
капча (код 2, текст помощи, предохранитель без запросов, сброс импортом cookies), сеть (код 3),
`--offline`, вход с паролем (скрытый ввод, пароль не попадает в вывод), импорт cookies (строка, `@файл`,
stdin), Zotero через подставной writer (и настоящий `ZoteroWriter` на `MockTransport` с отказом
соединения: `zotero add` без запросов к eLIBRARY.RU), MCP: список инструментов, аннотации, output schema, `_meta`,
вызовы поиска/карточек/экспорта/полного текста, капча → `is_error` с инструкцией, ленивое создание
общего клиента из нескольких потоков, `main()`. Вручную: `elibrary-mcp` по stdio (список инструментов,
вызов в режиме offline).

Не проверено: реальный терминал (ширина и цвета rich, кодировки консоли cp866/cp1251), окно
Playwright (`auth captcha`, `auth login --browser` — подменены в тестах), настоящий Zotero, транспорт
streamable-http, страница `org_items.asp` (нет fixture; проверены только параметры запроса),
`zotero authorize` (диалог Zotero).
