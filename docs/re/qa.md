# Итоговая проверка и интеграция (QA, 13.10.2026)

Запросов к elibrary.ru не было: всё проверено на фикстурах, фейковом сайте (`httpx2.MockTransport`) и
установленном колесе в режиме `ELIBRARY_OFFLINE=1` с `ELIBRARY_BASE_URL=http://127.0.0.1:9`.

## Как повторить проверку

```sh
.venv/Scripts/python.exe -m pytest -q          # весь набор тестов (сеть заблокирована conftest.py)
.venv/Scripts/ruff.exe check src tests
uv build                                       # sdist + wheel; wheel собирается из sdist
uv lock --check --offline
# чистое окружение
uv venv /tmp/qa-venv && VIRTUAL_ENV=/tmp/qa-venv uv pip install dist/elibrary_cli-0.1.0-py3-none-any.whl
ELIBRARY_HOME=/tmp/qa-home ELIBRARY_OFFLINE=1 ELIBRARY_BASE_URL=http://127.0.0.1:9 \
  /tmp/qa-venv/Scripts/elibrary --help            # а также: search articles --help, refdata, elibrary-mcp --help
```

Проверка stdio-сервера: `mcp.Client(StdioServerParameters(command="elibrary-mcp", env={...offline...}))`
→ `list_tools()` (22 инструмента, у всех есть output schema), `elibrary_session_status`,
`elibrary_reference_data` (без сети) и `elibrary_search_publications` (в offline даёт `ToolError` «Нет в
кэше…»). То же через `elibrary --offline mcp`. На stdout сервер ничего лишнего не пишет.

Набор тестов прогнан и на Python 3.10.21 против установленного колеса (не `src/`): данные
`data/*_options.json` находятся через `importlib.resources`, все тесты проходят.

## Сверка слоёв

* `models/__init__.py` переэкспортирует все pydantic-модели всех областей; `parsers/__init__.py` — все
  `parse_*`; `queries/__init__.py` — все построители (таблицы кодов остаются в модулях — имена пересекаются).
* MCP-инструменты и CLI берут перечисления (`Literal`) прямо из моделей запросов (`WhereField`, `PubType`,
  `Period`, `SortField`, `SortOrder`, `CodeType`, `MetricsBasis`, `AuthorSort`, `AuthorItemsSort`, `OrgSort`,
  фильтры журналов), имена полей совпадают с `PublicationQuery` / `AuthorQuery` / `OrgQuery` / `JournalQuery`.
* Каждое значение фильтров журналов, авторов и организаций превращается в код, который есть в сохранённом
  `<select>` формы (`data/*_options.json`); `ORG_SHOW` и сортировки `org_items.asp` совпадают с
  `queries/organizations.py`.
* `print()` / запись в stdout есть только в `cli/`; сообщения об ошибках — на русском (три внутренние
  английские строки переведены). Типы `kind` из `rows.infer_kind` / `item.normalize_kind` все известны
  экспорту.

## Исправлено

| Где | Что было | Что стало |
|---|---|---|
| `http.py` `_send` | При `follow_redirects=False` ответ 302 на `page_captcha.asp` / `ip_blocked.asp` / `ip_restricted.asp` считался обычной страницей и при `ttl > 0` попадал в кэш | `Location` классифицируется: предохранитель взводится, `CaptchaRequired` / `AccessBlocked` / `IpRestricted`, в кэш не попадает |
| `http.py` `_send`, `download` | Ловились только `TimeoutException`, `NetworkError`, `RemoteProtocolError`: `ProxyError`, `TooManyRedirects`, `UnsupportedProtocol`, `InvalidURL` выходили сырыми исключениями (трейсбек в CLI) | Повтор — только для временных ошибок; остальные сразу `NetworkError` (код 3) с русским текстом |
| `fulltext/net.py` | То же для внешних сайтов | `ExternalHTTPError` без повтора |
| `parsers/rows.py` | Блок «Версии:» (переводная версия) попадал в `source`; экспорт брал страницы перевода (1152–1164 вместо 1241–1258) | Блок исключён из `source`, id версий — в новом поле `PublicationHit.versions` |
| `parsers/rows.py` | Авторы — первый `<i>` ячейки; `<I>` внутри названия («AN <I>IN SILICO</I> PLATFORM») ломал строку: авторы терялись и попадали в `source` | Авторы — первый `<i>`, начинающийся сразу после названия |
| `parsers/rows.py` | `tr.css("td")` включал ячейки вложенной таблицы «Версии:» (колонка страниц не находилась) | Только собственные ячейки строки |
| `cli/common.py` | `publication_by_identifier` при ответе без id и EDN запрашивал `item.asp?edn=None` | `NotFound` |
| `cli/session.py`, `mcp_server.py` | После импорта cookies не было подсказки, что вход нужно подтвердить | Подсказка: `elibrary auth check` / `elibrary_session_check` (иначе PDF eLIBRARY.RU недоступны) |
| `tests/conftest.py` | Запрет сети держался на дисциплине | Автоматическая блокировка всех не-loopback соединений и DNS (снимается `ELIBRARY_LIVE=1`) |
| Документация | §3 и §6 `reverse-engineering.md` устарели | Порядок `where_*` в DOM, коды `white` 01–18 и `wos` 8; заметка о Lexbor в DEVELOPMENT.md |

## Чек-лист для живой проверки (сопровождающий, ≤ 10 страниц на сессию)

1. `elibrary auth check` → анонимная сессия; затем `elibrary search articles "нейронные сети" --year-from 2024`
   и `-p 2` (серверная пагинация `pagenum`). Сохранить страницу «ничего не найдено» (пустой поиск).
2. `elibrary resolve 10.<DOI>` и `elibrary resolve <SPIN>`: записать статус и `Location` коротких адресов.
3. Карточки без фикстур: доклад конференции с аннотацией, несуществующий `item.asp?id=`, `org_items.asp`,
   `org_about.asp`, `title_profile.asp`, вторая страница `author_items.asp` (POST).
4. После входа (`elibrary auth login` или cookies + `auth check`): `elibrary fulltext <id>` — что отдаёт
   `full_text.asp` (PDF или HTML со ссылкой); сохранить страницу `item.asp` вошедшего пользователя.
5. `start_session.asp` с верным и неверным паролем (для `auth.login`).
