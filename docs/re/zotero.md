# Экспорт ссылок и запись в Zotero (`exports.py`, `integrations/zotero.py`)

Источники: модели и парсеры области «Публикации» (`Publication`, `PublicationHit`), фикстуры
`item_article_46371463`, `item_book_76097861`, `item_chapter_76098153`, `item_disser_30339972`,
`item_patent_47430099`, строки `query_results_all_types`, `query_results_patents`, `author_items_3771`,
`contents_8610`; отчёт `research/zotero_mcp.md` (схема Zotero v42, шаблоны элементов, исходники zotero-mcp
0.14.1, pyzotero 1.15.2, Zotero 10.0.6: `server_connector.js`, `server_localAPI.js`, `translate_item.js`);
маппинг тегов RIS — транслятор Zotero `RIS.js`. Ни одного запроса к elibrary.ru и ни одной записи в
настоящую библиотеку Zotero при разработке не было: все тесты идут через `httpx2.MockTransport`.

## 1. Экспорт: общая схема

`exports.py` — чистые функции без сети. И `Publication` (карточка), и `PublicationHit` (строка списка)
сначала превращаются во внутреннюю запись `_Record`, из которой строятся все четыре формата, поэтому
форматы всегда согласованы.

| Функция | Результат |
|---|---|
| `to_zotero_item(pub, *, tags=(), keyword_tags=True, collections=(), fix_caps=True, access_date=None)` | dict — элемент Zotero API (`POST /users/<id>/items`) |
| `to_csl_json(pub, *, citekey=None, fix_caps=True, zotero_mcp=False)` | dict — элемент CSL-JSON |
| `to_bibtex(pub, *, citekey=None, with_edn_key=False, fix_caps=True)` | str — запись BibTeX/biblatex |
| `to_ris(pub, *, fix_caps=True)` | str — запись RIS `TY … ER` |
| `export(items, fmt, *, fix_caps=True, citekey_edn=False, tags=())` | str — пакет: BibTeX через пустую строку, RIS подряд, CSL/Zotero — JSON-массив |
| `citekey(pub, *, with_edn=False)` | ключ цитирования |
| `FORMATS`, `FORMAT_ALIASES`, `normalize_format()`, `FILE_EXTENSIONS`, `MEDIA_TYPES` | форматы |
| `split_person_name()`, `normalize_caps()`, `transliterate()`, `bibtex_escape()`, `zotero_item_type()` | вспомогательные |

### Типы

`Publication.kind` / `PublicationHit.kind` нормализуются: `dissertation` → `thesis`; `abstract` →
`conference`; `preprint` → `manuscript`, если `type_raw`/строка источника говорит «депонир…»/«Деп.» или есть
`dates.deposited`, иначе `preprint`; неизвестное или `None` → `other`.

| kind | CSL | Zotero | BibTeX | RIS |
|---|---|---|---|---|
| article | article-journal | journalArticle | @article | JOUR |
| conference | paper-conference | conferencePaper | @inproceedings | CPAPER |
| proceedings | book | book | @proceedings | BOOK (TY CONF Zotero импортирует как conferencePaper) |
| book | book | book | @book | BOOK |
| chapter | chapter | bookSection | @incollection | CHAP |
| thesis | thesis | thesis | @thesis | THES |
| patent (в т.ч. свидетельства о регистрации программ/БД) | patent | patent | @patent | PAT |
| report, grant | report | report | @techreport | RPRT |
| manuscript (депонированная рукопись) | manuscript | manuscript | @unpublished | MANSCPT |
| preprint | article | preprint | @misc | UNPB |
| dataset | dataset | dataset | @dataset | DATA |
| other | document | document | @misc | GEN |

### Поля Zotero

Используются только поля схемы v42, допустимые для типа (`_ZOTERO_FIELDS`, `_ZOTERO_CREATORS`):
Web API отвечает 400 на поле не того типа. Базовые имена переводятся в типовые (`publisher` →
`university` у thesis / `institution` у report и manuscript / `repository` у preprint и dataset;
`date` → `issueDate`, `number` → `patentNumber`, `authority` → `issuingAuthority` у patent;
`publicationTitle` → `bookTitle` / `proceedingsTitle`; `series` → `seriesTitle` у report; `type` →
`thesisType` / `reportType` / `manuscriptType` / `genre`). Значение, для которого у типа нет поля, уходит в `extra` строкой CSL-переменной
(`event-title: …`, `page: …`, `genre: патент на изобретение`) — так Zotero сам хранит такие данные.
Новые поля journalArticle (`publisher`, `place`, `section`, `PMID`…) сознательно не используются.
Пустые значения не выводятся. Поля `collections` (ключи) и `relations: {}` есть всегда.

* Статья: `publicationTitle` = `JournalRef.name_cased` (или `name` с исправленным регистром), `volume`,
  `issue`, `pages`, `date` (год), `ISSN` (печатный, иначе электронный), `DOI`.
* Книга: `publisher` = `BookInfo.publisher.name` (имя как на карточке), `place`, `numPages`, `ISBN`
  (несколько — через пробел), `series`, `edition`; редакторы (`role` содержит «редакт»/«ред.»/«составит»)
  → `editor`, переводчики → `translator`, прочие (рецензенты, руководители) → `contributor`.
* Глава: `bookTitle` = `ContainerRef.title`; авторы книги → `bookAuthor`; у главы без собственных авторов
  авторы книги становятся и `author` (монография). Место — из описания источника «Монография. Уфа, 2024».
* Доклад: `proceedingsTitle` (ИСТОЧНИК), `conferenceName`, `eventPlace` (КОНФЕРЕНЦИЯ), `publisher`
  (издательство источника), `volume` и `series` из описания источника (`Том 61`, `Сер. "…"`), даты
  конференции — строкой `Даты конференции: …` в `extra`.
* Диссертация: `thesisType` = «автореферат диссертации на соискание ученой степени доктора технических
  наук» (документ + степень в родительном падеже), `university` = место защиты **как на карточке**
  («Уфим. гос. авиац.-техн. ун-т», а не нынешнее название организации из подсказки), `place`, `numPages`,
  специальность — в `extra`.
* Патент: авторы → `inventor`; `patentNumber` «RU 2760636 C2», `applicationNumber`, `filingDate`
  (= «Дата регистрации» заявки), `issueDate` (= дата публикации), `priorityDate`, `assignee`
  (патентообладатели через «; »), `country` = код страны, `issuingAuthority` = «Роспатент» для RU; МПК — в
  `extra`. У patent нет `libraryCatalog` и `date`.
* Отчёт / грант / рукопись / препринт / датасет (фикстур нет): тип из `type_raw`, организация и номер —
  из `extra` карточки по ключам с «фонд», «депонир», «репозитор», «организац», «номер», иначе первая
  аффилиация (для отчёта/гранта).

`extra` начинается с `eLIBRARY ID: <id>`, `EDN: <EDN>`, затем `Цитирований в РИНЦ: N`, `УДК`, `МПК`,
особые строки типа и `Название на английском: …` (если `title_en` отличается). Ключевые слова → теги в
нижнем регистре (`γН2АХ` и аббревиатуры сохраняются), плюс теги вызывающего.

### CSL-JSON, BibTeX, RIS

* CSL: `id` = ключ цитирования, `source` = «eLibrary.ru», `note` = строки `extra`; патент: `number`,
  `authority`, `genre`, `issued` (полная дата), `submitted` (дата заявки), в `note` — номер заявки,
  патентообладатель, страна. `zotero_mcp=True` (по умолчанию в `elibrary_export_citation`) оставляет
  только ключи, которые `zotero_add_item` (zotero-mcp 0.11, `citation_import.csl_json_to_zotero`) **для
  этого типа** кладёт в поле, остальное переносит в `note` строками формата Extra (`ключ: значение`,
  имена `Фамилия || Имя`, даты ISO). Два случая: ключи вне его набора `handled` (`event-title`,
  `container-author`, `submitted`, `authority`…) zotero-mcp записал бы в Extra как JSON; ключи из
  `handled` он пишет только в поле с тем же именем в шаблоне типа (`/items/new` Web API: `university`,
  `institution`, `issueDate`…), а при его отсутствии **молча теряет**. Поэтому в Extra уходят: `publisher`
  у thesis/report/manuscript/preprint/dataset (`university`/`institution`/`repository`), `genre` у всех
  типов, кроме thesis (`reportType`, тип патента…), `issued` у patent (`issueDate`), `number` у
  manuscript/preprint/dataset, `container-title` у book/thesis/patent/…, `publisher-place` у dataset и т.п.
  (`exports._zotero_mcp_field`). Проверено прогоном настоящего `citation_import.py` zotero-mcp 0.11 на
  шаблонах из `schema.json` Zotero: ни одно значение `to_zotero_item` не теряется; тест
  `TestZoteroMcpCsl` повторяет этот конвертер.
* BibTeX: UTF-8 (кириллица как есть), экранируются `\ { } & % $ # _ ~ ^`; фигурные скобки из данных —
  `\textbraceleft{}` / `\textbraceright{}`, а не `\{` / `\}`: BibTeX, biber (btparse) и bibtexparser
  считают скобки, не глядя на `\`, и одна непарная `\{` ломала запись (zotero-mcp терял её молча); в
  `url`/`doi` (дословные поля) непарные скобки кодируются `%7B`/`%7D`. Слова с двумя и более
  заглавными защищены скобками (`{γН2АХ-позитивных}`); страницы `98--101`; `langid` (babel), `edn`;
  патент: `number`, `type`, `holder`, `location`, `date`, а также нестандартные `applicationnumber`,
  `filingdate`, `ipc`; диссертация: `school` + `type` (zotero-mcp берёт `type` у `@thesis`, а у
  `@phdthesis` подставил бы «PhD thesis»).
* RIS — теги выбраны по таблицам импорта транслятора Zotero `RIS.js` (`fieldMap`), чтобы значения
  попадали в те же поля, что и у `to_zotero_item` (тест `TestRisZoteroMapping` — копия этих таблиц):
  статья — `T2` журнал, `T3` серия, `A2` редакторы, `SN` ISSN; доклад (`CPAPER`) — `T2` название
  конференции (`conferenceName`), `C3` название сборника (`proceedingsTitle`), `T3` серия, `C1` место
  издания (`place`), `CY` место проведения (у CPAPER Zotero берёт `CY` в `place`, только если нет `C1`,
  иначе в заметку); книга и сборник (`BOOK`) — `T2` серия, `A3` редакторы (`A2` у книги — редактор
  серии), `SP` число страниц; глава (`CHAP`) — `T2` книга, `C2` авторы книги (`A3` — редактор серии),
  `A2` редакторы; отчёт — `T2` серия (`seriesTitle`), `ED` редакторы, `SN` номер, `PB` организация;
  диссертация — `PB` вуз, `M3` тип, `A3` редакторы (contributor), `SP` число страниц; патент — `SN`
  номер, `M1` заявка, `C1` дата заявки, `DA` дата публикации, `PB` патентообладатель, `C3` страна, `A2`
  ведомство, `M3` тип (Zotero кладёт его в заметку). Значения без тега у типа (`IS` у главы — это
  `numberOfVolumes`, страницы книги, число страниц статьи, номер рукописи…) — строками `переменная:
  значение` в `N1`. `DB  - eLIBRARY.RU`, `AN  - <id>`, `N1` — строки `extra` через «; » (при импорте RIS
  в Zotero `N1` становится заметкой, не полем Extra).

### Ключ цитирования

`<фамилия первого автора транслитом><год><первое значимое слово заглавия>`: `blokhina2021dlitelnoe`,
`miftakhova2024obrabotka`, `sukhanova2021otkazoustoychivaya`. Транслитерация BGN/PCGN-подобная
(й→y, х→kh, ц→ts, щ→shch, ю→yu, я→ya, ъ/ь опускаются). Предлоги/союзы пропускаются. Без авторов —
два первых слова заглавия (`vstroennoe2026programmnoe`), без всего — `elibrary<id>`. `with_edn=True` /
`citekey_edn=True` добавляет `-<edn>` (у строк без EDN — id). В пакете `export()` совпадающие ключи
получают `-<edn>`, полные дубликаты — ещё `-2`, `-3`.

### Имена

`split_person_name(display, full_name)`: «БЛОХИНА Т.М.» + подсказка «Блохина (Трубченкова) Таисия
Михайловна» → `Блохина` / `Таисия Михайловна` (подсказка берётся, только если фамилия или девичья фамилия и
инициалы совпадают; иначе — то, что показано: «VAVILOVA D.» остаётся латиницей). Без подсказки инициалы
нормализуются к «Т. М.» (последний инициал может быть без точки: «Панова Е.Н»). Регистр фамилий в
капсе исправляется по частям (`АЛЬ-МЕШХЕДАНИ` → `Аль-Мешхедани`). «Иванов Иван Иванович» → фамилия +
имя-отчество; «ИПМ РАН», длинные названия → одно поле `name` (организация).

### Регистр заголовков (`normalize_caps`)

Заглавия строк списков и патентов приходят капсом. Если ≥ 90 % букв заглавные (и букв ≥ 4), текст
переводится в регистр предложения: первое слово и слова после «!», «?» и точки после слова длиннее
трёх букв — с заглавной («эл. энергии» не превращается в «эл. Энергии»). Сохраняются: слова с цифрами
(`γН2АХ`, `3D`), римские числа, латинские слова в кириллическом тексте (`PYTHON`, `MATLAB`), слова со
смешанной латиницей/кириллицей (`FАВДЕМ`), частые русские аббревиатуры (`_RU_ACRONYMS`: РАН, ЛЭП, БПЛА,
ГИС, ДЗЗ, ЭВМ…). Режим `title` (журналы, сборники, конференции) для латиницы — Title Case. Известное
ограничение: имена собственные и неизвестные аббревиатуры теряют заглавные («сети Кохонена» → «сети
кохонена», «TITDS» → «Titds»); `fix_caps=False` отключает исправление.

### Разбор строки источника `PublicationHit.source` (best effort)

* Статья: «Журнал. 2024. Т. 11. № 1 (14). С. 112-116.» — журнал до первого «. ГГГГ.», том `Т.`/`Том`,
  номер `№`/`Вып.`, страницы — последнее «С. N» (страницы обязаны начинаться с цифры, иначе «С. И.» из
  «под ред. С. И. Бугашева» принималось за страницы).
* «В сборнике: …» / «В книге: …»: убираются страницы и «Город, ГГГГ.», строка режется по «. » (не после
  инициалов и сокращений «ред.», «г.», «им.», «вып.»…); заглавие сборника = первый сегмент + второй, если
  он начинается со «сборник/материалы/тезисы/труды…» или со строчной буквы («…: материалы международной
  научной конференции»). Сегмент «Фамилия И.О.» после заглавия — авторы книги; «Ответственный редактор:
  …» — редакторы (именительный падеж; «под ред. Иванова» — родительный, не используется). Город — из
  «Москва, 2026» / «г. Донецк, 2020».
* Патент/свидетельство: «Патент на изобретение RU 2760636 C2, 29.11.2021. Заявка № 2018145179 от
  19.12.2018.» → тип, номер, дата публикации, номер и дата заявки.
* Диссертация: «… / Уфим. гос. авиац.-техн. ун-т. Уфа, 2009» → тип работы, вуз, город.
* Грант: «НИР: грант № 95-01-01081. Российский фонд фундаментальных исследований. 1995.» → отчёт
  (`reportType` «НИР: грант», номер, фонд).
* Книга: «Липецк, 2023.» → место; ISBN и «Изд-во …», если встречаются.
* Для отчётов, грантов, рукописей, датасетов, прочего и книг без города исходная строка сохраняется в
  `extra` как `Описание (eLIBRARY): …`. Язык строки угадывается по алфавиту заглавия (ru/en).

## 2. Запись в Zotero (`integrations/zotero.py`)

### Конфигурация — `ZoteroConfig.from_env_and_zotero_mcp(settings, *, environ=None, zotero_mcp_config=None)`

Те же переменные, что у zotero-mcp: `ZOTERO_LOCAL` (True/False, `None` — не задано), `ZOTERO_API_KEY`,
`ZOTERO_LIBRARY_ID`, `ZOTERO_LIBRARY_TYPE` (`user`/`group`), `ZOTERO_LOCAL_API_KEY`,
`ZOTERO_LOCAL_SERVER_ID`, `ZOTERO_LOCAL_WRITE` (не задано/`auto`/true — можно писать), `ZOTERO_LOCAL_PORT`.
Приоритет как в zotero-mcp: значения `Settings`/окружения процесса, затем `client_env` из
`~/.config/zotero-mcp/config.json` (на Windows `C:\Users\<u>\.config\zotero-mcp\config.json`). Ключ
локального API: `ZOTERO_LOCAL_API_KEY` → собственный ключ elibrary-cli (`<data_dir>/zotero.json`,
пишется `authorize_local`, права 0600 где возможно) → `local_api.key` zotero-mcp. `ZOTERO_LIBRARY_ID=0`
(заглушка zotero-mcp для локального режима) не считается настройкой Web API. Конфиг Claude Desktop не
читается. `describe()` отдаёт состояние без секретов.

### Способы записи (`ZoteroWriter(config, backend="auto", transport=None, use_pyzotero=None)`)

Порядок `auto`: **local → connector → web**, берётся первый доступный (`available_backends()`; проверка —
`GET /connector/ping` и `GET /api/` с таймаутом 3 с, результат кэшируется). При `ZOTERO_LOCAL=false`
локальные способы в `auto` не используются. Если local отказал по ключу (401/412) или оказался недоступен
**до** первой записи, `add()` переходит к следующему способу и пишет об этом в `message`.

1. **local** (Zotero 10+): `http://127.0.0.1:23119/api/users/0/...` (группа — `/api/groups/<id>`),
   запись с `Zotero-API-Key` (локальный ключ) и обязательным `Zotero-Server-ID` (из `GET /api/`),
   `Zotero-Write-Token`. Чтение без ключа. Ключ — `authorize_local()`: `POST /api/local/authorize
   {"appName": "elibrary-cli"}` показывает в Zotero окно «Разрешить / Всегда разрешать / Запретить»;
   ключ «Всегда разрешать» сохраняется, одноразовый (`remember: false`) — только в памяти и тратится на
   первую запись (вложение после него не загрузится — сообщение в `message`). Ответы: 401 → «ключ отозван
   или одноразовый», 403 «Local API is not enabled» → включить «Разрешить другим приложениям…», 412 —
   Server-ID другой установки, 428 — нет Server-ID, 429 на authorize — не больше 5 запросов в минуту.
2. **connector** (любой запущенный Zotero, ключ не нужен): `POST /connector/saveItems {"sessionID",
   "uri", "items": [... "id": "el<i>"]}` → 201 (поля `collections`, `relations`, `key`, `version`
   убираются; Zotero сам применяет `fromJSON` в нестрогом режиме); PDF — `POST
   /connector/saveAttachment?sessionID=…` с заголовком `X-Metadata` (`parentItemID` = `el<i>`, JSON в
   ASCII) и телом-файлом (Zotero 8+, на Zotero 7 — 404 → `attachment: "unsupported"`); коллекция и теги —
   `POST /connector/updateSession {"sessionID","target":"C<id>"|"L<id>","tags"}` (`target`
   обязателен, поэтому для одних тегов берётся выбранная в Zotero коллекция из
   `/connector/getSelectedCollection`). Теги из `saveItems` становятся «автоматическими» и могут быть
   отброшены настройкой Zotero, поэтому теги вызывающего идут через `updateSession` (ручные). Коллекция
   ищется по имени/пути среди `targets` (путь строится по `level`, коллекции групп — тоже); создать
   коллекцию коннектор не может — ошибка до сохранения. Все запросы с `X-Zotero-Connector-API-Version: 3`
   и `Zotero-Allowed-Request: 1`, User-Agent не начинается с `Mozilla/` (иначе Zotero отбрасывает
   запрос). Ключи созданных записей и дедупликация — через локальный API **на чтение** (без ключа), если
   он включён, и **той библиотеки, куда попадут записи**: `saveItems` пишет в библиотеку, выбранную в
   Zotero, `updateSession` переносит записи в библиотеку цели (`moveToLibrary`). Библиотека цели — запись
   уровня 0 (`L<libraryID>`) над коллекцией в `targets`, без `collection` — `libraryID` выбранной; «Моя
   библиотека» — первая запись уровня 0 (Zotero ставит её первой) → `/api/users/0`; группа ищется по имени
   в `GET /api/users/0/groups` → `/api/groups/<groupID>`. Поэтому `getSelectedCollection` вызывается при
   каждом сохранении. Если читать нечего (локальный API выключен — так по умолчанию; группа не нашлась),
   дубликаты не проверяются, `key: None`, а в `message` каждой сохранённой записи — «Проверка на дубликаты
   не выполнена: <причина>. Повторное сохранение создаст ещё одну копию записи.» Запись никогда не
   помечается `duplicate` по элементу из другой библиотеки.
3. **web**: `https://api.zotero.org/users/<ZOTERO_LIBRARY_ID>/...` с `Zotero-API-Key`. Коллекции
   читаются постранично (`limit=100&start=`).

local и web говорят на одном API v3: при установленном pyzotero (extra `zotero`) используется он
(`create_items`, `items(q=…)`, `everything(collections())`, `create_collections`, `upload_attachments`),
иначе встроенный клиент на `httpx2`. С переданным `transport` (тесты) pyzotero по умолчанию не
используется; `use_pyzotero=True` требует его (`DependencyMissing`). Для локального порта ≠ 23119
`zot.endpoint` переопределяется (pyzotero его жёстко прописывает).

### Загрузка файла (local/web, встроенный клиент)

1) `POST /items` с вложением `{"itemType":"attachment","parentItem":KEY,"linkMode":"imported_file",
"title":"Полный текст (PDF)","contentType","filename":<имя файла>}`; 2) `POST /items/<att>/file`
(`If-None-Match: *`, форма `md5, filename, filesize, mtime` в миллисекундах, `contentType`) → `{"exists":1}`
или `{url, uploadKey, contentType, prefix, suffix}`; 3) `POST url` с телом `prefix + файл + suffix`
(локально — `/api/local/uploads/<key>` с `Zotero-Server-ID`, в вебе — S3 без заголовков Zotero);
4) `POST /items/<att>/file` с `upload=<uploadKey>` → 204. PDF берутся только из локальных файлов;
ссылки на elibrary.ru в Zotero не передаются.

### `add(items, *, collection, tags, pdf_paths, dedupe=True, backend=None, create_collection=True, attachment_title)`

`items` — dict'ы `to_zotero_item` (объекты `Publication`/`PublicationHit` конвертируются сами).
`collection` — ключ, имя или путь «A/B» (без учёта регистра; точный путь от корня важнее совпадения
имени; несколько одинаковых имён → ошибка с просьбой указать путь; отсутствующий путь создаётся на
local/web, если `create_collection`). Дедупликация: поиск `q=<EDN>`, `q=<DOI>` и всегда
`q=item.asp?id=<eLIBRARY ID>` (`qmode=everything`): транслятор eLibrary.ru самого Zotero (кнопка в
браузере) сохраняет только URL страницы и «Цитируемость в РИНЦ», без EDN, а DOI у многих русских работ
нет. Кандидат подтверждается сравнением DOI / `EDN:` / `eLIBRARY ID:` в Extra / URL; eLIBRARY ID и EDN
берутся и из URL `elibrary.ru/item.asp?id=…` / `?edn=…` (вложения, заметки и удалённые пропускаются);
найденные — `status: "duplicate"` с ключом, не изменяются. Повторы внутри одного вызова (одна карточка по
ID и по EDN, одинаковые идентификаторы в списке MCP) пишутся один раз: остальные — `duplicate` с ключом
первой копии и сообщением «Та же публикация, что и №N этого запроса…» (PDF повтора прикрепляется к первой
копии). `dedupe=False` отключает обе проверки. Запись пачками по 50. MCP-инструмент `elibrary_zotero_add`
поэтому не помечен `idempotent_hint`: без проверки (коннектор без локального API) повтор вызова создаёт
копию.
Результат на каждый элемент: `{index, title, url, key, backend, status: created|duplicate|failed,
message, attachment: None|uploaded|exists|failed|unsupported, collection}`.

### Состояние машины пользователя (из отчёта)

Zotero 10.0.6 запущен, локальный API включён и отдаёт `Zotero-Server-ID`; zotero-mcp 0.11.0 с
`ZOTERO_LOCAL=true`, в его `config.json` нет `local_api`. Значит `auto` сейчас выберет **connector**; после
`elibrary zotero authorize` («Всегда разрешать») — **local** (ключи записей, создание коллекций).

## 3. Что не проверено вживую

* Ни один способ записи не запускался против настоящего Zotero: поведение взято из исходников Zotero
  10.0.6/zotero-mcp/pyzotero и проверено фейковым сервером в `tests/test_zotero.py`.
* Путь через pyzotero проверен только подставным модулем (pyzotero в dev-окружении не установлен).
* Web API: протокол загрузки в S3 (prefix/suffix) и постраничный список коллекций — по документации.
* Поиск `qmode=everything` локального API по полю Extra (для поиска по EDN) — по исходникам
  (`quicksearch-everything`), не вживую.
* Карточки типов без фикстур (доклад, сборник, отчёт, грант, препринт, рукопись, датасет) покрыты
  синтетическими `Publication` в `tests/test_exports.py`; разбор строк отчётов/препринтов/датасетов
  ограничен сохранением исходной строки в `extra`.
* Импорт наших CSL/BibTeX в zotero-mcp проверен только его конвертером `citation_import.py` (0.11) на
  шаблонах из `schema.json`, без записи в Zotero; сам `zotero_add_item` не запускался.
* Поиск группы для коннектора по имени (`/api/users/0/groups`) и «Моя библиотека» = первая библиотека в
  `targets` — по исходникам Zotero (`server_localAPI.js`, `server_connector.js`, `Zotero.Libraries.getAll`),
  не вживую; при двух группах с одинаковым именем дедупликация пропускается.
