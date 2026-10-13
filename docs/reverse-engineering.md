# Реверс-инжиниринг eLIBRARY.RU (октябрь 2026)

Публичного API у elibrary.ru нет (платный «API НЭБ» — см. раздел 9). Сайт — классический ASP на IIS,
все данные отдаются серверным HTML. Ниже — то, что установлено **вживую** (запросы с домашнего IP,
браузер + httpx, 13.10.2026) и сверено с кодом существующих парсеров. Сохранённые страницы лежат в
`tests/fixtures/html/`.

## 1. Транспорт: TLS-особенность

* `https://elibrary.ru` (IIS 7/10). Кодировка страниц — **UTF-8** (`Content-Type: text/html; Charset=UTF-8`),
  POST-формы тоже принимают UTF-8 (`application/x-www-form-urlencoded`).
* **TLS 1.2 и только CBC-SHA наборы шифров** (`ECDHE-RSA-AES256-SHA`, `AES256-SHA`). TLS 1.3 не
  поддерживается. Если клиент не предлагает CBC-SHA наборы, сервер **молча зависает** на рукопожатии
  (без TLS-alert) — это выглядит как таймаут.
* Python по умолчанию исключает SHA1-наборы (`!SHA1` в строке шифров `ssl.create_default_context()`),
  поэтому `requests`/`httpx` «из коробки» получают `The handshake operation timed out`. curl на Windows
  (Schannel) и браузеры работают. Решение: `ctx.set_ciphers("DEFAULT")` (см. `http.make_ssl_context`).

| Набор шифров клиента | Результат |
|---|---|
| Python default (`ECDH+AESGCM:...:!SHA1`) | зависание |
| `ECDHE-RSA-AES256-GCM-SHA384` / `AES128-GCM-SHA256` | зависание |
| `AES256-SHA` | OK, TLSv1.2 |
| `DEFAULT` / `HIGH:!aNULL` | OK, `ECDHE-RSA-AES256-SHA` |

## 2. Сессия, cookies, капча

* Cookies первого лица: `SCookieGUID` (выдаётся на первом запросе), `SUserID` (идентификатор сессии,
  выдаётся после `start_session.asp`). Оба **не HttpOnly** — их видно в `document.cookie`.
* Цепочка первого захода: `GET /defaultx.asp` → 302 `www.elibrary.ru/start_session.asp?rpage=...` →
  302 → `defaultx.asp` (или страница капчи).
* **Капча «Тест Тьюринга»** (reCAPTCHA v2): 302 на `/page_captcha.asp?rpage=<исходный URL>`, HTTP 200,
  ~1.1 КБ, `<title>Тест Тьюринга</title>`, форма `POST /check_captcha.asp` (`g-recaptcha-response`,
  `rpage`), sitekey `6Lc_wu8UAAAAAJnwHXzpTkFc2yDHTMMt0O9v4hU1`, текст «С Вашего IP-адреса поступает
  необычно много запросов». В `rpage` POST-запрос превращается в GET со всеми параметрами.
* Наблюдаемый порог для **анонимной** сессии: капча после **~10–13 страниц**, даже с паузами 4–8 с.
  Свежая сессия может получить капчу сразу.
* После прохождения капчи человеком сервер помечает `SUserID` как проверенный, и эти cookies
  работают **из любого HTTP-клиента с того же IP** (проверено: curl/httpx с `SUserID` + `SCookieGUID`
  из браузера получают страницы без капчи). На этом построен сценарий «капчу решает пользователь в
  браузере, CLI импортирует cookies».
* Другие «ворота» (по данным существующих проектов): `ip_blocked.asp` / `div#blockedip` («Из-за нарушения
  правил пользования сайтом eLIBRARY.RU…») — бан IP; `ip_restricted.asp?rpage=` — анонимный доступ с
  этого IP ограничен, нужно войти; IIS `403 - Forbidden: Access is denied.`; сброс TCP для
  зарубежных/облачных IP. Почти всё отдаётся с HTTP 200 — классифицировать нужно по URL и содержимому
  (`http.classify`).
* Вход: `POST /start_session.asp` с полями `login`, `password`, `knowme`, `rpage` (форма `login`, JS
  `check_all()`); выход — `POST /end_session.asp` (`saveuser=1`). Признак входа — блок `#win_session` с
  «Имя пользователя: …».

## 3. Поиск публикаций: `query_results.asp`

Форма `results` на `querybox.asp`, JS `query_message()` → `POST /query_results.asp`. Тот же запрос
принимается и GET-ом (classic ASP `Request()`), **но только с `changed=1`** — иначе сервер показывает
последний запрос сессии.

Поля в порядке браузера (`FormData`), значения чекбоксов — `on`:

| Поле | Значения | Смысл |
|---|---|---|
| `querybox_name`, `authors_all`, `titles_all`, `rubrics_all` | `""` / `"Петров В А\|"` / `"8610\|"` / `"500000\|"` | фильтры по авторам (ФИО), журналам (titleid), рубрикам ГРНТИ, через `\|` |
| `changed` | `1` | **обязательно**: выполнить новый запрос |
| `queryid` | `""` | |
| `ftext` | текст | строка поиска (≥ 2 символов) |
| `where_name`, `where_affiliation`, `where_abstract`, `where_fulltext`, `where_keywords` | `on` | где искать: название, организации авторов, аннотация, полный текст, ключевые слова (порядок полей в DOM/`FormData`; на экране они расположены иначе). Отметка `where_fulltext` (onClick) отмечает и все остальные — `build_search_form` повторяет это |
| `where_references` | `""` | (скрытое поле, всегда пустое; onClick `where_fulltext` ставит ему `checked`, но у hidden это ни на что не влияет) |
| `type_article`, `type_disser`, `type_book`, `type_report`, `type_conf`, `type_patent`, `type_preprint`, `type_grant`, `type_dataset` | `on` | типы: статьи в журналах, диссертации, книги, отчёты, материалы конференций, патенты, депонированные рукописи, гранты, наборы данных |
| `search_itemboxid` | `""` | искать в подборке |
| `search_morph` | `on` | с учётом морфологии |
| `search_freetext` | `on` | «искать похожий текст» (нечёткий поиск, по умолчанию включён) |
| `search_fulltext` | `on` | только публикации с полным текстом на eLibrary.Ru |
| `search_open` | `on` | только доступные вам |
| `search_results` | `on` | искать в результатах предыдущего запроса |
| `begin_year`, `end_year` | `0` или год | годы публикации |
| `issues` | `all`, `m1`, `m3`, `m6`, `m12` | период поступления |
| `orderby` | `rank`, `date`, `insdate`, `name`, `title`, `cited` | сортировка: релевантность, дата выпуска, дата добавления, название статьи, название журнала, цитирования |
| `order` | `rev` (убыв.), `nrm` (возр.) | |
| `queryboxid`, `save_queryboxid` | `0` | сохранённые запросы |

Результаты: `table#restab`, строки `tr#a<ID>` (в списках автора/выпуска — `tr#arw<ID>`): ссылка
`/item.asp?id=ID` с названием, `<i>` с авторами, далее библиографическая строка, последний столбец —
цитирования. Счётчик: «ВСЕГО НАЙДЕНО ПУБЛИКАЦИЙ: **N** из **M**». **100 записей на страницу.**
В списках автора/организации с `show_refs=1` встречаются строки `tr#brw<ID>` — записи, известные только
из списков литературы (без карточки и без ссылки `item.asp`). У переведённых статей в ячейке названия
есть вложенная таблица «Версии:» со ссылкой на карточку другой версии (её название, авторы и выходные
данные — не часть библиографической строки самой записи). Курсив `<i>` бывает и внутри названия
(латинские названия видов, «IN SILICO»): список авторов — первый `<i>` сразу после названия.

**Пагинация серверная:** `GET /query_results.asp?pagenum=N` показывает страницу N **последнего запроса
этой сессии** (параметры запроса в GET без `changed=1` игнорируются). Поэтому клиент сначала выполняет
запрос (`changed=1`), затем листает `pagenum`, все запросы — последовательно в одной сессии.

## 4. Поиск авторов: `authors.asp`

`POST /authors.asp` (форма `results`, JS `author_search()`), пагинация — тем же POST с `pagenum=N`
(состояние не хранится на сервере). Поля:

`authors_all, pagenum, authorbox_name, selid, orgid, orgadminid, townid, regionid, surname, codetype,
codevalue, countryid, town, orgname, authorboxid, rubriccode, metrics, sortorder, order`
(+ чекбоксы `inaffil`, `rubricinreg`, `show_nopubl`).

* `codetype`: `SPIN`, `ORCID`, `ResearcherID`, `ScopusID`, `ArXivID`, `RSCI` (РИНЦ AuthorID); значение —
  `codevalue`.
* `countryid`: ISO3 (`RUS`, …); `town`/`townid` — автодополнение `town_autocomplete2.asp?q=..&scountry=..`.
* `metrics`: `0` — ядро РИНЦ, `1` — РИНЦ, `2` — elibrary.ru.
* `sortorder`: `0` фамилия, `1` публикации, `2` цитирования, `3` индекс Хирша; `order`: `0` возр., `1` убыв.
* Результат: «Всего найдено авторов: **N** из **M**. Показано на данной странице: с a по b», строки
  `tr#a<AUTHORID>`: ФИО, звёздочка «зарегистрирован в SCIENCE INDEX», организация (город), число
  публикаций (`author_items.asp?authorid=ID`), цитирований, индекс Хирша. 100 на страницу.
* Профиль: `author_profile.asp?id=ID`; публикации: `author_items.asp?authorid=ID&show_refs=1` (первая
  страница — GET; следующие — POST формы страницы с `pagenum`, проверено вживую).
* **Фильтр по организации работает только через `orgid`** (выбирается во всплывающем окне
  `user_org_add3.asp`); поле `orgname` — только отображаемый текст. Проверено вживую: `surname=Иванов` +
  `orgname=…` без `orgid` возвращает всех Ивановых. Поэтому `elibrary-cli` при `--org <название>`
  сначала находит организацию одним запросом к `orgs.asp` и подставляет её ID.

## 5. Организации: `orgs.asp`

`POST /orgs.asp`: `pagenum, orgname, town, regionid, countryid, sortorder, order`.
`sortorder`: `0` название, `1` город, `3` число публикаций, `4` цитирования; `order` `0`/`1`.
Строки ведут на `org_profile.asp?id=ID` и `org_items.asp?orgsid=ID` (список публикаций, те же строки
`tr#arw<ID>`, проверено вживую; у крупных организаций страница тяжёлая — ответ > 60 с).

## 6. Журналы: `titles.asp`

`POST /titles.asp`: `titles_all, pagenum, titlebox_name, titlename, countryid, rubriccode, language,
translated, wos, scopus, white, vak, risc, ftopen, sortorder, order` (+ чекбоксы `rsci`, `doaj`,
`corerisc`, `fulltext`, `isactive`, `isscientific`).

* `countryid`: `RUS`, `nRUS` (зарубежные), ISO3; `language`: `RU`, `nRU`, `EN`, `ML`, …
* `wos`/`scopus`: `1` индексируется, `2` переводная версия, `3` частично, `4..7` квартиль 1–4, `9` нет;
  только у `wos` есть ещё `8` — «квартили 1–4» (любой квартиль).
* `white`: `01`…`18` — `01`–`06` Белый список (входит, уровни 1–4, не входит), `07`–`12` ЕГПНИ (то же),
  `13`–`18` «ЕГПНИ/Белый список» (то же); `vak`: `1` входит, `2..4` категория К1–К3, `9` нет;
  `risc`: `0` в РИНЦ, `2` нет, `3..6` квартиль рейтинга Science Index; `ftopen`: `1` открытый доступ,
  `2` частично, `3` по подписке.
* `sortorder`: `0` название, `3` число статей, `4` цитирования в РИНЦ, `1` цитирования из ядра,
  `7` рейтинг Science Index.
* Карточка: `title_about_new.asp?id=TITLEID`; содержание выпуска: `contents.asp?titleid=TITLEID`
  (последний выпуск) или `contents.asp?id=ISSUEID`.

## 7. Карточка публикации: `item.asp`

`GET /item.asp?id=ID` или `GET /item.asp?edn=EDN`. Короткие адреса: `https://elibrary.ru/<EDN>` (302 на
`item.asp?edn=`), а также `/<DOI>`, `/<ISBN>`, `/<ORCID>`, `/<SPIN>`, `/<ROR>`, `/<ISSN>`.

Страница построена из пар «метка: значение»: `eLIBRARY ID`, `EDN`, `DOI`, `Тип`, `Язык`, `Том`, `Выпуск`,
`Год`, `Страницы`, `Поступила в редакцию`, блок `ЖУРНАЛ` (ISSN, eISSN, Web of Science, Scopus, Белый
список, ЕГПНИ, Перечень ВАК, RSCI, Рейтинг Science Index), `КЛЮЧЕВЫЕ СЛОВА`, `АННОТАЦИЯ`,
`БИБЛИОМЕТРИЧЕСКИЕ ПОКАЗАТЕЛИ` (Входит в РИНЦ, Цитирований в РИНЦ, ядро РИНЦ…), `ТЕМАТИЧЕСКИЕ
НАПРАВЛЕНИЯ` (OECD, ASJC, ГРНТИ, ВАК), `АЛЬТМЕТРИКИ`, `ОПИСАНИЕ НА АНГЛИЙСКОМ ЯЗЫКЕ`. У патентов:
`Номер патента`, `Патентное ведомство`, `Номер заявки`, `Дата регистрации`, `Дата публикации`,
`Патентообладатели`, `МЕЖДУНАРОДНАЯ ПАТЕНТНАЯ КЛАССИФИКАЦИЯ`.

## 8. Полный текст

* Признак наличия: `GET /ispdf.asp?itemid=ID` → фрагмент
  `<a href="javascript:load_article(ID)"><img src="/images/download_green.png" title="Доступ к полному
  тексту открыт"></a>`. Легенда значков: `download_green` — открыт (`load_article`), `download_blue` —
  на сайте издателя (`url_article(id, urlnum)` → `url_article.asp`), `download_yellow` — можно заказать
  (`order_item(id, цена)` → `buy_item.asp`; **никогда не автоматизируется**), `download_orange` —
  доступ закрыт; нет значка — полного текста в НЭБ нет.
* Для **анонимного** пользователя все функции полного текста (`load_article`, `url_article`,
  `file_article`) выдают «необходимо вначале войти в библиотеку». Скачивание PDF — только после входа.
* У вошедшего читателя `load_article(id)` отправляет форму `results` на **`full_text.asp?id=<id>`**
  (работает и GET); ответ — PDF или ссылка/редирект на `/download/elibrary_<id>_<число>.pdf` (число
  выдаётся сервером, вычислить его нельзя). По коду сторонних проектов; без учётной записи вживую не
  проверялось.
* Поэтому `fulltext` использует: (1) PDF elibrary при наличии вошедшей сессии; (2) открытые источники
  по DOI/названию (КиберЛенинка, Unpaywall, OpenAlex, Crossref, `citation_pdf_url` издателя).

## 9. Официальные интерфейсы

* **API НЭБ** (платный, по договору, только с IP из договора): `http://elibrary.ru/projects/API-NEB/API_NEB.aspx?ucode=<GUID>&sid=<NNN>&…`;
  известные сервисы `011` (ID публикаций по автору/журналу/организации/году), `013` (авторы
  организации), `018` (поиск автора), `024` (профиль автора), `026` (полная запись публикации), XML.
* **find_edn.asp** (бесплатный ключ после регистрации на `support.elibrary.ru/openapi`):
  `https://elibrary.ru/find_edn.asp?apikey=<key>&ref=<ссылка>` → короткий XML с EDN/ID/DOI/ISBN.
* Вспомогательные фрагменты сайта: `for_reference.asp?id=` (готовая ссылка для цитирования),
  `similar_items.asp?itemid=`, `translate_abstract.asp?id=`, `town_autocomplete2.asp`.
* Экспорта в BibTeX/RIS у сайта нет — `elibrary-cli` строит их сам.

## 10. Правила использования

Правила сайта разрешают персональное некоммерческое использование и запрещают сплошное копирование
роботами. `elibrary-cli` рассчитан на ручной, точечный поиск: один поток, пауза ≥ 5 с, локальные
лимиты (60/ч, 300/сут), кэш, остановка на первой капче, капчу решает только человек. Для массовой
выгрузки используйте официальный API (api@elibrary.ru).

## 11. Проверено вживую (13.10.2026, анонимная сессия, 17 запросов без капчи)

| Сценарий | Запросы | Результат |
|---|---|---|
| `search articles "графен" --year-from 2023`, затем `-p 2` | `query_results.asp?…changed=1`, затем `?pagenum=2` | total 1051, 11 страниц, на стр. 2 другие 100 записей |
| `search patents …`, пустой поиск | `query_results.asp` | 43 415 патентов; пустой результат → total 0 |
| `search authors`, `search orgs`, `search journals --vak included` | POST `authors.asp` / `orgs.asp` / `titles.asp` | 8423 / 2 / 14 записей; фильтр по организации — только `orgid` (см. §4) |
| `get publication FYOAKL` | `item.asp?edn=FYOAKL` | полная карточка, DOI, авторы с аффилиациями |
| `resolve <DOI>` | `/<DOI>` | eLIBRARY ID 46371463 |
| `get author 17783`, `author-pubs` стр. 1 и 2 | `author_profile.asp`, `author_items.asp` (GET, затем POST) | SPIN, ORCID, аффилиации; 26 страниц |
| `get org-pubs 18078`, `get issue --journal-id 8559` | `org_items.asp`, `contents.asp` | 49 публикаций; 29 статей выпуска с рубриками и страницами |
| `fulltext 54076582` | `item.asp` + КиберЛенинка | 22 700 символов HTML-текста, совпадение названия 1.0 |

Капча на этой сессии появлялась после ~13 запросов при паузах 4–8 с; с паузой 6–8 с и без лишних
фрагментов (`ispdf.asp`) 17 запросов прошли без неё.

## 12. Подробные заметки по областям

[публикации](re/publications.md) · [авторы](re/authors.md) · [организации](re/organizations.md) ·
[журналы](re/journals.md) · [полный текст](re/fulltext.md) · [вход и сессия](re/auth.md) ·
[Zotero](re/zotero.md) · [клиент](re/client.md) · [CLI и MCP](re/cli_mcp.md) · [QA и чек-лист](re/qa.md)
