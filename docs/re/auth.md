# Сессия и вход (`auth.py`)

Источник — сохранённые 13.10.2026 анонимные страницы (`tests/fixtures/html/*`), заметки
`docs/reverse-engineering.md` §2 и исследование `official_api_and_antibot.md` §2.3, §4.4–4.5.
Ни одного запроса к elibrary.ru при разработке не отправлялось.

## 1. Где на странице сессия

Левая панель **каждой** полной страницы содержит два блока.

### `#win_login` («ВХОД» / «Вход в библиотеку») — только у анонимных

Строки таблицы «метка: / значение»:

| Метка | Пример | Поле `SessionInfo` |
|---|---|---|
| `IP-адрес компьютера:` | `192.0.2.1` | `ip_address` |
| `Название организации:` | `не определена` (или организация по IP) | `organization` (`None`, если «не определена») |
| `Имя пользователя или адрес эл. почты:` | `<input name=login>` | — (подпись поля, отбрасывается) |
| `Пароль:` | `<input type=password name=password>` | — |

Далее кнопка «Вход» (`<div class=butred onClick=check_all()>` в новой вёрстке, `<input type=submit
value="Вход">` без `name` в старой), чекбокс `knowme` «Запомнить меня», ссылки «Правила доступа»,
«Регистрация», «Забыли пароль?», «Вход через Вашу организацию» (SSO, `sso_orgs.asp`, не реализован).

### `#win_session` («СЕССИЯ» / «Текущая сессия»)

| Метка | Аноним | Вошедший | Поле |
|---|---|---|---|
| `Имя пользователя:` | `Незарегистрированный пользователь` | ссылка `<a href="author_info.asp?isold=1" title="Персональная карточка пользователя">ИМЯ&nbsp;</a>` | `user_name`, `user_card_url` |
| `IP-адрес компьютера:` | только в «старой» вёрстке | то же | `ip_address` |
| `Название организации:` | только в «старой» вёрстке | то же | `organization` |
| `Начало работы:` | `13.10.2026 14:43` | | `session_started` |
| `Время работы:` | `00:02` | | `session_duration` |

Затем ссылки «Личный кабинет» (`project_user_office.asp`) и «Закрыть сессию»
(`javascript:end_session()`) — они есть **и у анонимов**, поэтому признаком входа не являются.
Неизвестные метки попадают в `extra`.

Две вёрстки:

* «новая» (item, authors, titles, author_*, contents, org_profile, title_about): блоки `div.left-panel`,
  в `#win_session` только имя/начало/время, IP и организация — в `#win_login`;
* «старая» (querybox, orgs, query_results): синие таблицы `bgcolor=#5c6aa6`, вложенные `table`,
  в `#win_session` ещё IP и организация.

Парсер берёт «листовые» строки `tr` (без вложенных `tr`; в selectolax `node.css()` включает сам узел,
поэтому условие — `len(tr.css("tr")) == 1`) и спаривает строку, оканчивающуюся на `:`, со следующей.
Подпись, за которой идёт строка с `input`, отбрасывается. Значения из `#win_session` важнее, чем
из `#win_login`.

### Признак входа

`logged_in = True`, только если в `#win_session` после «Имя пользователя:» стоит не
«Незарегистрированный пользователь». Дополнительно собираются `anonymous_markers_found`:
`Незарегистрированный пользователь`, `Вы не авторизованы`, `необходимо вначале войти в библиотеку`
(страницы с ограничением доступа), `form[name=login]` (форма входа есть только у анонимов),
`ip_restricted.asp` (по URL). `state`: `logged_in` / `anonymous` / `captcha` (страница «Тест
Тьюринга», распознаётся `http.classify`) / `unknown` (нет блока сессии — AJAX-фрагмент вроде
`ispdf.asp`). Ошибку `ParseError` парсер не бросает: «не видно сессии» — это тоже ответ.

Вошедший вариант проверен на одной реальной странице 2025 г. из открытого репозитория
(`research/src/rawfiles/olegolas912_Python_codes_elibrary.html`, старая вёрстка): имя — ссылка
`author_info.asp?isold=1`, блока `#win_login` нет. В тестах вошедшие страницы получены из наших
анонимных фикстур подменой имени и удалением формы входа (`tests/test_auth.py::logged_in_page`).

## 2. Форма входа `login`

```html
<FORM method=post name="login" onSubmit="return false">         <!-- новая вёрстка -->
<FORM name="login" action="/start_session.asp" method=POST onSubmit="return check_all()">  <!-- старая -->
<input type=hidden name="rpage" value=https://elibrary.ru/item.asp?id=46371463>   <!-- без кавычек! -->
... <input type=text id="login" name="login" value="">
... <input type=password id="password" name="password" value="">
... <input type=checkbox name="knowme">
</FORM>
```

* JS `check_all()` проверяет непустые поля (`alert("Не указано имя пользователя!")`,
  `alert("Не указан пароль!")`; в старой вёрстке `alert("Не указан пароль или имя читателя!")`),
  затем `frm.action="/start_session.asp"; frm.submit()`. Enter в полях — `login_enter()`.
* **Порядок полей (FormData, порядок документа): `rpage`, `login`, `password`, `knowme`.** `knowme`
  без `value` → при отметке отправляется `knowme=on`, без отметки не отправляется вовсе. Кнопка
  «Вход» не имеет `name` и не отправляется. Порядок одинаков во всех 4 проверенных фикстурах
  (`item_article`, `authors_form`, `querybox_form`, `titles_form`) и во всех остальных.
* `rpage` — адрес текущей страницы; для страниц результатов POST-формы он содержит GET-версию запроса.
  `build_login_form` по умолчанию ставит `https://elibrary.ru/defaultx.asp`.
* **Особенность HTML:** `<FORM>` стоит между строками таблицы, поэтому HTML5-парсер (Lexbor, браузер)
  создаёт пустой элемент `form`, а `input` оказываются вне его (связь только через form owner).
  `form[name=login] input` ничего не находит. Поэтому `login_form_fields()` читает сырую разметку
  между `<form name=login>` и `</form>` регулярными выражениями (скрипты предварительно вырезаются).

## 3. Вход и выход

* `login()` — **один** `POST /start_session.asp` (без предварительного GET), `Referer: rpage`,
  `Origin`, `application/x-www-form-urlencoded` UTF-8, кэш отключён (`TTL_NONE`). Ответ сайта
  (ожидается 302 на `rpage`) разбирается `parse_session_info`. Перенаправления POST-запроса
  `http.py` проходит сам, по одному: переход на `page_captcha.asp` / `ip_restricted.asp` /
  `ip_blocked.asp` **не запрашивается**, а распознаётся по `Location`.
  Успех → `http.state["account"] = имя`, сброс `query.current` (новая серверная сессия), сдвиг
  `cookies.version` (другие процессы пересоберут HTTP-клиент с новыми cookies).
  Пароль не логируется, не кэшируется и не сохраняется (тест проверяет таблицу состояния).
* **Капча в ответ на вход.** По `docs/reverse-engineering.md` §2 сайт превращает POST в GET со всеми
  полями внутри `rpage`: `page_captcha.asp?rpage=…/start_session.asp?rpage=…&login=…&password=…`.
  Такой адрес никогда не сохраняется и не показывается: для POST `http.py` строит адрес проверки из
  адреса запроса без query (`page_captcha.asp?rpage=https%3A%2F%2Felibrary.ru%2Fstart_session.asp`) —
  он попадает в `breaker.url`, в текст `CaptchaRequired`, в `elibrary auth status` /
  `elibrary_session_status` и в окно `elibrary auth captcha`. `login()` дополнительно (защита в
  глубину) ловит `CaptchaRequired` / `IpRestricted`, вычищает логин и пароль (в том числе
  URL-кодированные) из адреса ошибки и из `breaker.url` и бросает новое исключение `from None`.
* Вход не повторяется никогда: POST отправляется один раз даже после ошибки соединения или HTTP 5xx.
* Неудачный вход: **страница ошибки ни разу не сохранялась** — распознаётся защитно
  (`login_failure_reason`): слово-ошибка (`неверн`, `неправильн`, `ошибк`, `не найден`,
  `не зарегистрирован`, `не совпада`, `не существует`, `заблокирован`, …) рядом со словом о входе
  (`парол`, `логин`, `имя пользователя`, `читател`, `эл. почт`, `вход`, …) в видимом тексте вне
  левых блоков или в `alert("…")`. Если сообщения нет, но страница анонимная — тоже `LoginRequired`
  («не подтвердил вход»). Ложных срабатываний на 21 фикстуре и 57 сторонних сохранённых страницах нет.
* `login_from_env()` — `ELIBRARY_LOGIN` / `ELIBRARY_PASSWORD`, не более одной попытки за процесс
  (повторные входы похожи на перебор паролей); повторный вызов возвращает `None` без запроса.
* `logout(http, remember=False)` — `POST /end_session.asp` (форма `session`, JS `end_session()`).
  Удаляет `account` и `query.current`, cookies не трогает (сервер сам выдаст анонимную сессию),
  сдвигает `cookies.version`.
  **Проверено вживую 14.10.2026:** на вошедшей странице JS спрашивает «Сохранить Ваши данные на этом
  компьютере?» и ставит `saveuser` = `1` (ОК) или `0` (Отмена). С `saveuser=1` после входа с
  «Запомнить меня» сервер на следующем запросе **снова** показывает вход, то есть выхода нет. Поэтому по
  умолчанию шлётся `saveuser=0` (настоящий выход); `remember=True` / `elibrary auth logout --keep` —
  `saveuser=1`.

## 4. Проверка сессии и cookies

Паузу предохранителя снимает только человек. Правила (`http.py`, `auth.py`):

| Пауза (`breaker.gate`) | Импорт cookies с `SUserID` | Импорт без `SUserID` | `check_session()` | `check_session(force=True)` (CLI `elibrary auth check --force`) |
|---|---|---|---|---|
| `captcha`, `ip_restricted` | снимает | не снимает | снимает, только если cookies с `SUserID` импортированы **после** срабатывания (`cookies.imported_at > breaker.tripped_at`), иначе ошибка без запроса | снимает |
| `ip_blocked`, `forbidden` (24 ч) | не снимает | не снимает | никогда не снимает: `RateLimited` с текстом о блокировке, без запроса | снимает |

* `check_session(http, force=False)` — `http.release_breaker_for_check()` (снять паузу по правилам
  выше или бросить её ошибку: `CaptchaRequired` с адресом проверки, `RateLimited` о блокировке),
  затем **один** некэшируемый GET (`defaultx.asp` по умолчанию). Капча → `CaptchaRequired`
  пробрасывается, предохранитель снова срабатывает в `http.py` (с новым `tripped_at`, поэтому
  повторная проверка снова ничего не отправит). Обновляет `account`. MCP-инструмент
  `elibrary_session_check` вызывает её **всегда без** `force`; `--force` есть только в CLI.
* `import_cookies()` — обёртка над `http.set_cookies`. Строка с `SUserID` заменяет набор cookies
  (`--merge` — дополняет), удаляет `account` до следующей проверки, записывает
  `cookies.imported_at` и снимает паузу после капчи / `ip_restricted`. Строка **без** `SUserID` не
  удаляет сохранённую сессию (дописывается к ней, `account` остаётся, предупреждение) и ничего не
  снимает — так «мусорные» cookies не превращаются в новую анонимную сессию в обход капчи.
  Возвращает только **имена**: `cookie_names`, `count`, `has_session_cookie` (`SUserID` в итоговом
  наборе), `has_guid_cookie` (`SCookieGUID`), `session_cookie_imported` (`SUserID` был в строке),
  `ignored` (аналитика `_ym*`, `_ga*`, `tmr_*`, отброшенная `http.set_cookies`), `user_agent_saved`,
  `breaker_open` / `breaker_gate` (пауза, которая осталась), `warnings` (по-русски: нет `SUserID`,
  нет `SCookieGUID`, не передан User-Agent, сохранённая сессия оставлена, пауза не снята).
* `http.clear_session()` (`elibrary auth clear`) забывает cookies, `account`, `query.current` и UA,
  но паузу **не** снимает: новая анонимная сессия — не способ обойти капчу или блокировку.

## 5. Окно браузера (`browser_session`, extra `[browser]`)

* Playwright импортируется лениво; нет пакета → `DependencyMissing("browser", "playwright")`.
* Запуск **с окном** (`headless=False`): канал `msedge`, затем `chrome`, затем встроенный Chromium
  (или только указанный `channel`). Контекст: `user_agent` = UA из состояния `http` или настроек,
  `locale=ru-RU`, cookies CLI с доменом `.elibrary.ru`.
* Адрес по умолчанию: `breaker.url` (страница `page_captcha.asp?rpage=…`, на которой сработал
  предохранитель), иначе `defaultx.asp`. Чужие домены запрещены.
* Раз в секунду **только читается** `page.url`, `page.content()` (главный фрейм) и
  `context.cookies()`; цель — страница больше не капча (`purpose="captcha"`) или в блоке сессии
  появилось имя (`purpose="login"`); также выход по закрытию окна и по `timeout_s`. Код **никогда** не
  кликает капчу, не трогает iframe reCAPTCHA и не вводит логин/пароль — это делает человек.
* Затем cookies сайта (при дублях одного имени на разных доменах побеждает значение, изменённое
  сервером) и `navigator.userAgent` сохраняются `http.set_cookies`. Если цель не достигнута
  (`closed`/`timeout`), cookies всё равно сохраняются, но состояние предохранителя восстанавливается
  (ключи `http.BREAKER_KEYS`: `breaker.until`, `.gate`, `.url`, `.tripped_at`), чтобы не слать
  запросы в непройденную капчу. `cookies.imported_at` при этом остаётся новым: человек мог пройти
  проверку и закрыть окно раньше времени, поэтому один `elibrary auth check` после этого разрешён.
* Внутри работающего asyncio-цикла (MCP-сервер) sync API Playwright не работает — функция сама уходит
  в рабочий поток, но блокирует вызывающего; в MCP вызывать через `anyio.to_thread.run_sync`.

## 6. Общее состояние CLI и MCP-сервера (`http.py`)

Cookies, часы паузы, бюджеты и предохранитель лежат в общем `state.sqlite3`; несколько процессов
(CLI рядом с MCP-сервером, `xargs -P`) согласуются только через него.

* **Предохранитель перед каждым запросом.** `_check_breaker` вызывается до бронирования слота и ещё раз
  **после** паузы троттлинга, прямо перед отправкой, на каждой попытке (и перед повтором после ошибки
  соединения): если другой процесс за это время получил капчу или блокировку, запрос не уходит.
* **Одна схема «страница-ворота → пауза + исключение»** (`_raise_for_gate`) для страниц и для
  скачивания PDF: капча → `CaptchaRequired`, пауза `ELIBRARY_BREAKER_MINUTES`; `ip_blocked` / IIS 403
  → `AccessBlocked`, пауза 24 ч; `ip_restricted` → `IpRestricted`, пауза `ELIBRARY_BREAKER_MINUTES`.
  Раньше `download()` открывал предохранитель только на капче, и бан при пакетном скачивании PDF
  (`zotero add --with-pdf`) его не останавливал. Текст паузы после бана — без `elibrary auth check`:
  написать в support@elibrary.ru, не повторять, кэш через `--offline`.
* **`--offline` / `ELIBRARY_OFFLINE=1` — ни одного запроса**, в том числе `download()`
  (`OfflineCacheMiss`; уже скачанный `elibrary_<id>.pdf` используется как прежде).
* **Версия cookies.** `cookies.version` — счётчик в `state.sqlite3` (атомарный `StateStore.incr`),
  сдвигается в `set_cookies`, `clear_session`, `auth.login`/`logout` (`http.cookies_changed()`) и когда
  сервер прислал новые cookies. Каждый процесс помнит версию, из которой собран его httpx-клиент, и
  пересобирает клиент перед запросом, если версия сдвинулась: долгоживущий MCP-сервер сразу берёт
  cookies, импортированные в терминале (`elibrary auth cookies`).
* **`_save_cookies` пишет только `Set-Cookie` этого ответа и его перенаправлений** поверх сохранённых,
  а не весь jar клиента (устаревший jar затирал импортированный `SUserID`). Если версия сдвинулась,
  пока запрос был в пути, ответ относится к старой сессии и ничего не пишется.
* **Повторы.** GET повторяется один раз (через 10 с) только после ошибок до отправки запроса
  (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) и после HTTP 5xx. `ReadTimeout`, `WriteTimeout`,
  `ReadError`, `WriteError`, `RemoteProtocolError` не повторяются (сервер мог уже выполнить тяжёлую
  страницу), POST не повторяется никогда. Медленным страницам (`org_items.asp`) клиент передаёт
  `http.get(..., timeout=...)` = `ELIBRARY_HEAVY_TIMEOUT` (по умолчанию 180 с, не меньше
  `ELIBRARY_TIMEOUT`).
* **Права файлов (POSIX).** Каталоги данных и кэша создаются с правами 0700 (существующие сужаются до
  0700, кроме домашнего каталога и общих каталогов со sticky-битом вроде `/tmp`), файлы
  `state.sqlite3`, `http-cache.sqlite3`, `external-cache.sqlite3` создаются заранее с 0600 (SQLite даёт
  `-wal`/`-shm` те же права), существующие сужаются до 0600. На Windows ничего не меняется (ACL профиля).

## 7. Что не проверено вживую

* Ответ `start_session.asp` при верном и неверном пароле (редирект? своя страница? `alert`?).
* Как выглядит `#win_session` вошедшего пользователя в «новой» вёрстке (проверена только старая).
* Влияние `knowme` на срок cookies (смысл `saveuser=0/1` выяснен, см. §3).
* Блок организации для вошедших через организацию (SSO) и «удалённый доступ» по IP организации.
* `browser_session` проверялся только на имитации Playwright (без настоящего браузера).
