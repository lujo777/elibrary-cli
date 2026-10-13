# elibrary-cli

Инструмент для работы с [eLIBRARY.RU](https://elibrary.ru) (Научная электронная библиотека, РИНЦ) из
командной строки **и** MCP-сервер для ИИ-ассистентов (Claude Code, Claude Desktop, Cursor и др.).

* **Поиск**: статьи, журналы, авторы, патенты, организации, книги (а также диссертации, материалы
  конференций и любые типы публикаций с расширенными фильтрами).
* **Карточки**: публикация (авторы с аффилиациями, источник, DOI/EDN, аннотация, ключевые слова, рубрики,
  показатели РИНЦ, данные патента/книги/диссертации), профиль автора, организации, журнала, оглавление
  выпуска, списки публикаций автора и организации.
* **Полный текст**: PDF eLIBRARY.RU (для вошедших читателей) или открытые источники — КиберЛенинка,
  Unpaywall, OpenAlex, Crossref, сайт издателя; извлечение текста из PDF/HTML, постраничная выдача.
* **Экспорт**: BibTeX, RIS, CSL-JSON, нативный JSON Zotero; **интеграция с Zotero** и с
  [zotero-mcp](https://github.com/54yyyu/zotero-mcp) (необязательная).
* **Резолвинг идентификаторов**: eLIBRARY ID, EDN, DOI, ISBN, SPIN, ORCID, ISSN, ROR, ссылки elibrary.ru.

Официального бесплатного API у eLIBRARY.RU нет, поэтому инструмент работает с HTML-страницами сайта.
Подробности реверс-инжиниринга: [docs/reverse-engineering.md](docs/reverse-engineering.md) и
[docs/re/](docs/re/).

> **Правила.** eLIBRARY.RU разрешает персональное некоммерческое использование и запрещает сплошную
> выгрузку роботами. `elibrary-cli` рассчитан на точечный поиск по вашему запросу: один поток, пауза
> ≥ 5 с между запросами, локальные лимиты (60 в час, 300 в сутки), кэш, остановка на первой капче.
> Капчу проходит только человек. Для массовой выгрузки используйте официальный API (api@elibrary.ru).

## Установка

Нужен Python 3.10+. Удобнее всего через [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "elibrary-cli[pdf] @ git+https://github.com/lujo777/elibrary-cli"
```

Появятся команды `elibrary` (CLI) и `elibrary-mcp` (MCP-сервер). Необязательные дополнения:

| extra | что даёт |
|---|---|
| `pdf` | извлечение текста из PDF (pypdf, pdfminer.six) |
| `zotero` | запись в Zotero через pyzotero (без него работают локальный API и коннектор Zotero) |
| `browser` | окно браузера для прохождения капчи и входа (Playwright) |
| `all` | всё перечисленное |

Например, всё сразу: `uv tool install "elibrary-cli[all] @ git+https://github.com/lujo777/elibrary-cli"`.
Для `browser` используется установленный Edge/Chrome; если их нет — `playwright install chromium`.

## Быстрый старт

```bash
elibrary search articles "нейронные сети" --year-from 2020 --sort cited
elibrary search patents "беспилотный летательный аппарат" --sort date
elibrary search books "машинное обучение"
elibrary search authors Иванов --org "Томский политехнический университет"
elibrary search orgs "томский политехнический"
elibrary search journals "вестник" --vak included --country RUS
elibrary get publication FYOAKL                # по EDN; также eLIBRARY ID, DOI или ссылка
elibrary get author 17783
elibrary get author-pubs 17783 -p 2
elibrary get issue --journal-id 8559           # последний выпуск журнала
elibrary fulltext 54076582 --max-chars 5000
elibrary export 46371463 54076582 -f bibtex -o refs.bib
elibrary resolve 10.47056/0365-9615-2021-172-7-98-101
```

* Вывод: таблица в терминале, JSON при перенаправлении (`-f table|json|jsonl`).
* `-p/--page` — страница результатов (100 записей на странице публикаций и авторов).
* Коды фильтров (страны, рубрики, ВАК/Scopus/WoS, Белый список...): `elibrary refdata`.
* Глобальные флаги: `--offline` (только кэш), `--refresh` (не брать из кэша), `--min-delay`.

## Капча, сессия и вход

После ~10 запросов анонимной сессии eLIBRARY.RU показывает проверку «Тест Тьюринга» (reCAPTCHA).
CLI сразу останавливается и подсказывает, что делать. Пройти проверку может только человек:

1. **Через окно браузера** (extra `browser`): `elibrary auth captcha` — откроется браузер с сессией CLI,
   пройдите проверку, окно закроется само.
2. **Через свой браузер**: откройте elibrary.ru, пройдите проверку, затем в консоли разработчика (F12)
   выполните `copy(document.cookie)` и передайте строку в CLI. Нужен тот же компьютер и та же сеть.

```bash
elibrary auth cookies "SUserID=...; SCookieGUID=..."
elibrary auth check
```

`elibrary auth status` показывает состояние сессии, лимиты и предохранитель, не обращаясь к сайту.

**Вход** (нужен для PDF с eLIBRARY.RU): `elibrary auth login` (логин из `ELIBRARY_LOGIN` или запрос,
пароль из `ELIBRARY_PASSWORD` или скрытый ввод) или `elibrary auth login --browser`. Пароль нигде не
сохраняется.

VPN, зарубежные и облачные IP eLIBRARY.RU блокирует почти сразу — используйте обычное подключение.

## Полный текст

`elibrary fulltext <id|EDN|DOI>` перебирает источники по порядку и возвращает текст частями
(`--offset`, `--max-chars`; `--all` — целиком; `-o file.txt` — в файл):

1. **eLIBRARY.RU** — PDF через `full_text.asp`, только для вошедших читателей и только для
   публикаций с открытым доступом к полному тексту;
2. **КиберЛенинка** — HTML-страница статьи (адрес строится по названию; `robots.txt` соблюдается,
   поиск через API КиберЛенинки выключен по умолчанию);
3. **Unpaywall** (нужен `ELIBRARY_CONTACT_EMAIL`), **OpenAlex**, **Crossref** — по DOI;
4. **сайт издателя** — мета-теги `citation_pdf_url` (с учётом `robots.txt`).

Порядок источников задаётся опцией `-s/--source`. Извлечённый текст кэшируется, поэтому листание не
скачивает документ повторно.

## Экспорт и Zotero

```bash
elibrary export 46371463 -f bibtex        # bibtex | ris | csl | zotero
elibrary zotero status                    # чем можно записывать в Zotero
elibrary zotero add 46371463 54076582 --collection "eLIBRARY" --tag ринц --with-pdf
```

Запись в Zotero выбирает первый доступный способ:

1. **Локальный API Zotero 10+** — ключ выдаётся командой `elibrary zotero authorize`;
2. **Коннектор Zotero** — нужен только запущенный Zotero, ключ не нужен;
3. **Web API** — нужны `ZOTERO_API_KEY` и `ZOTERO_LIBRARY_ID`.

Переменные окружения те же, что у **zotero-mcp** (`ZOTERO_LOCAL`, `ZOTERO_API_KEY`,
`ZOTERO_LIBRARY_ID`, `ZOTERO_LIBRARY_TYPE`), и читается его `config.json`. Одной настройки хватает обоим
серверам.

Если подключены оба MCP-сервера, ассистент может:

* вызвать `elibrary_export_citation(format="csl_json")` и передать результат в
  `zotero_add_item(source_type="csl_json")` из zotero-mcp;
* либо сразу вызвать `elibrary_zotero_add`.

Нативный JSON Zotero (`-f zotero`) сохраняет поля, которые CSL теряет: у патентов — номер заявки и
патентообладателей, у диссертаций — организацию.

## MCP-сервер

**Claude Code:**

```bash
claude mcp add --transport stdio --scope user elibrary -- uvx --from "elibrary-cli[pdf] @ git+https://github.com/lujo777/elibrary-cli" elibrary-mcp
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json` на Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` на macOS):

```json
{
  "mcpServers": {
    "elibrary": {
      "command": "uvx",
      "args": ["--from", "elibrary-cli[pdf] @ git+https://github.com/lujo777/elibrary-cli", "elibrary-mcp"],
      "env": { "ELIBRARY_MIN_DELAY": "5" }
    }
  }
}
```

Если `uvx` не находится, укажите полный путь к нему (`where uvx`). Для **Cursor** — тот же блок в
`.cursor/mcp.json`. Сервер можно запустить и как `elibrary mcp`, в том числе
`--transport streamable-http --port 8000`.

Инструменты:

| Группа | Инструменты |
|---|---|
| Поиск | `elibrary_search_articles`, `elibrary_search_patents`, `elibrary_search_books`, `elibrary_search_publications`, `elibrary_search_authors`, `elibrary_search_organizations`, `elibrary_search_journals` |
| Карточки | `elibrary_get_publication`, `elibrary_get_author`, `elibrary_get_author_publications`, `elibrary_get_organization`, `elibrary_get_organization_publications`, `elibrary_get_journal`, `elibrary_get_journal_issue`, `elibrary_resolve_identifier` |
| Полный текст | `elibrary_get_fulltext` (постранично: `offset`, `max_chars`, `next_offset`) |
| Экспорт, Zotero | `elibrary_export_citation`, `elibrary_zotero_add` |
| Сессия, справочники | `elibrary_session_status`, `elibrary_session_check`, `elibrary_set_cookies`, `elibrary_reference_data` |

Когда сайт просит капчу, инструмент возвращает понятную ошибку с инструкцией. Пройдите проверку
(`elibrary auth captcha` в терминале или в своём браузере) и передайте cookies командой
`elibrary auth cookies` или прямо в чат — ассистент вызовет `elibrary_set_cookies`. CLI и MCP-сервер
используют одну сессию, один кэш и общие лимиты.

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ELIBRARY_MIN_DELAY` | `5` | пауза между запросами к elibrary.ru, с |
| `ELIBRARY_JITTER` | `2` | случайная добавка к паузе, с |
| `ELIBRARY_MAX_PER_HOUR` / `ELIBRARY_MAX_PER_DAY` | `60` / `300` | локальные лимиты запросов |
| `ELIBRARY_BREAKER_MINUTES` | `15` | пауза после капчи (её снимает импорт cookies с `SUserID`; пауза после блокировки IP — 24 ч) |
| `ELIBRARY_CACHE_TTL` / `ELIBRARY_SEARCH_CACHE_TTL` | `72` / `24` | время жизни кэша, ч (у карточек своё: 30 дней) |
| `ELIBRARY_OFFLINE`, `ELIBRARY_REFRESH` | — | только кэш / без кэша |
| `ELIBRARY_COOKIES` | — | строка cookies (вместо сохранённой сессии) |
| `ELIBRARY_LOGIN`, `ELIBRARY_PASSWORD` | — | вход в eLIBRARY.RU |
| `ELIBRARY_USER_AGENT` | Chrome | User-Agent (лучше тот же, что у браузера с cookies) |
| `ELIBRARY_CONTACT_EMAIL` | — | e-mail для Unpaywall/Crossref/OpenAlex («вежливый» пул) |
| `ELIBRARY_HOME` | каталог данных ОС | где хранить сессию и кэш |
| `ELIBRARY_TIMEOUT` | `60` | таймаут запроса, с |
| `ELIBRARY_HEAVY_TIMEOUT` | `180` | таймаут медленных страниц (публикации организаций), с; после таймаута запрос не повторяется |
| `ELIBRARY_MAX_DOWNLOAD_MB` | `100` | предельный размер скачиваемого файла (PDF и др.), МБ |
| `ELIBRARY_CYBERLENINKA_SEARCH` | — | `1` — искать статью через API КиберЛенинки, если адрес по названию не подошёл (`/api/` закрыт в `robots.txt`, поэтому выключено) |
| `ZOTERO_*` | — | как у zotero-mcp |

Сессия (cookies) и кэш лежат в каталоге данных пользователя (`elibrary auth status` покажет путь).
Значения cookies никогда не выводятся.

## Как это устроено

```
cli/ (typer)   mcp_server.py (MCPServer)
        \        /
        client.py (ElibraryClient)
     /      |        \
queries/  parsers/  fulltext/, exports.py, integrations/zotero.py, auth.py
     \      |
      http.py — единственная точка выхода на elibrary.ru: TLS-совместимость, троттлинг,
                бюджеты, предохранитель, кэш, cookies (SQLite, общий для CLI и MCP)
```

Неочевидная находка: сервер elibrary.ru поддерживает только TLS 1.2 с наборами шифров CBC-SHA. Если
клиент их не предлагает, рукопожатие молча зависает, поэтому `requests`/`httpx` «из коробки» получают
таймаут. Подробнее — в [docs/reverse-engineering.md](docs/reverse-engineering.md).

## Разработка

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
```

Тесты не ходят в сеть (это проверяется). Они работают на настоящих страницах eLIBRARY.RU,
сохранённых в `tests/fixtures/html/`; IP-адрес в блоке сессии заменён на `192.0.2.1`. Соглашения по
коду — в [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Лицензия

MIT (см. [LICENSE](LICENSE)). Данные eLIBRARY.RU и полные тексты принадлежат их правообладателям.
