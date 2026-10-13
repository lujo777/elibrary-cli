# Журналы: каталог, карточка, показатели, содержание выпуска

Область: `models/journals.py`, `queries/journals.py`, `parsers/journals.py`, `data/journals_options.json`,
тесты `tests/test_journals.py`. Всё разобрано по страницам, сохранённым 13.10.2026
(`tests/fixtures/html/titles_form.html`, `titles_results.html`, `title_about_8610.html`, `contents_8610.html`).
Во время разработки к elibrary.ru не было отправлено ни одного запроса.

| Страница | URL | Парсер | Модель |
|---|---|---|---|
| Каталог журналов (форма и результаты) | `POST /titles.asp` | `parse_journal_search` | `SearchPage[JournalHit]` |
| Опции формы каталога | `GET /titles.asp` | `parse_journal_search_options` | `dict` → `data/journals_options.json` |
| Карточка журнала | `GET /title_about_new.asp?id=TITLEID` (`&langid=2` — описание на английском) | `parse_journal_profile` | `JournalProfile` |
| Анализ публикационной активности | `GET /title_profile.asp?id=TITLEID` | `parse_journal_indicators` | `JournalIndicators` (**нет фикстуры**) |
| Содержание выпуска | `GET /contents.asp?titleid=TITLEID` (последний выпуск) или `?id=ISSUEID` | `parse_issue_contents` | `IssueContents` |

## 1. Форма поиска `titles.asp`

Форма `<FORM method=post name="results">`, `action` ставит JS (`title_search()` → `titles.asp`).
**Форма стоит внутри `<table>`**, поэтому HTML5-парсер (lexbor) выносит `<form>` из таблицы и элементы
формы оказываются вне неё — порядок полей определён по исходной разметке (тест
`test_form_checkbox_positions_match_the_dom` читает его регуляркой прямо из фикстуры).

Порядок полей (как в `FormData` браузера):

```
titles_all, pagenum, titlebox_name           hidden
titlename                                    текст: название журнала, издательства или ISSN
countryid, rubriccode, language, translated  select
wos, scopus, white, vak, risc, ftopen        select
rsci, doaj, corerisc, fulltext,              checkbox — отправляются ("on") только если отмечены
isactive, isscientific
sortorder, order                             select
```

Живой запрос, давший `titles_results.html` (совпадает байт в байт с `build_journal_search_form`, тест
`test_form_matches_live_request_byte_for_byte`):

```
titles_all=&pagenum=&titlebox_name=&titlename=вычислительные технологии&countryid=&rubriccode=&language=
&translated=&wos=&scopus=&white=&vak=&risc=&ftopen=&sortorder=1&order=1
```

* `title_search()` ставит `pagenum=""`, `goto_page(N)` — `pagenum=N` и отправляет ту же форму: **пагинация
  без состояния на сервере**, каждая страница заново отправляет весь запрос. Для страницы 1 билдер шлёт
  пустой `pagenum`, для N>1 — число.
* На странице результатов в форме ещё есть чекбоксы `tid<TITLEID>` (не отмечены, не отправляются).
* Поле `rpage` формы входа на странице результатов показывает, что сервер принимает тот же запрос и GET-ом
  (`titles.asp?rubriccode=&sortorder=1&...`), но мы используем POST, как браузер.
* JS `onChange` у `sortorder`: «по названию» → `order=0` (возр.), остальные → `order=1` (убыв.).
  `JournalQuery.effective_order` повторяет это поведение, если `order` не задан.

### Коды значений (проверены по фикстуре)

| Поле формы | Поле `JournalQuery` | Читаемое значение → код сайта |
|---|---|---|
| `countryid` | `country` | `RUS` Россия, `nRUS` зарубежные страны, ISO 3166-1 alpha-3 (`DEU`…); принимается и название («Германия») |
| `rubriccode` | `rubric_code` | 6-значный код ГРНТИ верхнего уровня (`500000`), `1`/`2`/`3` — мультидисциплинарные; `27` → `270000`; название рубрики |
| `language` | `language` | `RU`, `nRU` (иностранный), `ML` (несколько языков), `EN`, …, `UN` (не указан); название языка |
| `translated` | `translated` | `full`=1, `partial`=2, `no`=0 |
| `wos` | `wos` | `indexed`=1, `translated`=2, `partial`=3, `q1..q4`=4..7, `q1-q4`=8, `not`=9 |
| `scopus` | `scopus` | `indexed`=1, `translated`=2, `partial`=3, `q1..q4`=4..7, `not`=9 (кода 8 нет) |
| `white` | `white` | `included`=01, `l1..l4`=02..05, `not`=06, `egpni`=07, `egpni_l1..l4`=08..11, `egpni_not`=12, `any`=13 (ЕГПНИ/Белый список), `any_l1..l4`=14..17, `any_not`=18 |
| `vak` | `vak` | `included`=1, `k1`=2, `k2`=3, `k3`=4, `not`=9 |
| `risc` | `risc` | `indexed`=0, `not`=2, `q1..q4`=3..6 (квартиль рейтинга Science Index) |
| `ftopen` | `ftopen` | `open`=1, `partial`=2, `subscription`=3 |
| `sortorder` | `sort` | `name`=0, `articles`=3, `citations_rinc`=4, `citations_core`=1 (по умолчанию на сайте), `si_rating`=7 |
| `order` | `order` | `asc`=0, `desc`=1 |
| `rsci`, `doaj`, `corerisc`, `fulltext`, `isactive`, `isscientific` | `rsci`, `doaj`, `core_rinc`, `fulltext`, `active`, `scientific` | `on` |

Валидаторы `JournalQuery` принимают и читаемые значения, и «сырые» коды сайта (`vak="2"` ≡ `vak="k1"`,
`white="5"` ≡ `"05"` ≡ `"l4"`), хранят читаемую форму; неверное значение → `ValidationError` с русским
сообщением и списком допустимых значений.

**Расхождение с `docs/reverse-engineering.md`:** там для `white` указано `01…13`; в форме 2026-10 кодов **18**
(добавлены 13–18 «ЕГПНИ/Белый список»). Для `wos` есть ещё код `8` «квартили 1-4».

### `data/journals_options.json`

Сгенерирован `parse_journal_search_options(titles_form.html)` (тест проверяет, что файл совпадает с разбором
фикстуры). Формат как у других областей: `{"<поле формы>": [{"value", "label", "count"?}, ...]}`; пустая
опция «любое значение» не включается; числа в скобках (`Россия  (18260)`) вынесены в `count`; у
`sortorder`/`order` счётчиков нет. Дополнительный ключ `checkboxes` — шесть чекбоксов с подписями и
счётчиками. Объём: 158 стран, 72 рубрики, 95 языков. Поиск по подписи: `queries.journals.find_option`,
подпись по коду: `option_label`.

## 2. Результаты `titles.asp`

* Счётчик: `td.redref` — «Всего найдено журналов: **4** из **164076**. Показано на данной странице: с **1**
  по **4**» → `total`, `total_in_database`; «с a по b» → `per_page` (если `b < total`) и номер страницы.
* Таблица `table#restab`: первая строка — заголовок (`№ | (чекбокс) | Журнал | Вып. | Публ. | Цит.`), у
  «Вып./Публ./Цит.» есть `span[title]` с пояснением. Колонки сопоставляются **по заголовку**; неизвестные
  колонки попадают в `JournalHit.extra` (заголовок → текст). Если заголовка нет — позиционно.
* Строки `tr#a<TITLEID>`:
  * `a[href="title_about_new.asp?id=ID"] b` — название, `<font>` после `<br>` — издатель/учредитель
    (`publisher`, бывает пустым `<font></font>` → `None`);
  * «Вып.» — `a[href="contents.asp?titleid=ID"]` (`issues`, `contents_url`);
  * «Публ.» — `a[href="title_items.asp?id=ID"]` + ссылка на `title_profile.asp` (`articles`, `items_url`);
  * «Цит.» — `a[href="cit_title_items.asp?id=ID&pubcat=corerisc"]` (`citations`), что именно считается —
    `citations_kind` из `title` заголовка (при `sortorder=1`: «Число цитирований статей из ядра РИНЦ»).
  * У журналов без выпусков в ячейках просто `0` без ссылок.
  * ISSN в строке не показывается (поле `issn` заполняется, только если в ячейке встретится шаблон ISSN).
* Пагинация (по аналогии с `authors.asp`/`orgs.asp`, в фикстуре журналов её нет — всего 4 результата):
  ссылки `javascript:goto_page(N)`, максимальный N («В конец») → `pages`. Сколько журналов на странице —
  **неизвестно** (вычисляется из «с a по b»).
* Если `query` не передан, `SearchPage.query` восстанавливается из эха формы на странице (значение
  `titlename`, выбранные опции, отмеченные чекбоксы, hidden `pagenum`) в терминах `JournalQuery`.
* Страница формы без запроса («Укажите параметры запроса для поиска журналов») → пустой список, `total=None`.

## 3. Карточка `title_about_new.asp?id=ID`

* Верх: «eLIBRARY ID: `<a href=title_about_new.asp?id=8610>`», «Язык описания: русский, `<a ...&langid=2>`английский»
  (язык без ссылки — текущий; если текущий английский или в URL `langid=2`, имя кладётся и в `name_en`).
  Английский вариант страницы не сохранялся — предполагается та же разметка.
* Название: `<title>` «Вычислительные технологии / информация о журнале» (берётся часть до последнего « / »);
  `span.bigtext` даёт то же название ПРОПИСНЫМИ — используется как запасной вариант.
* Блоки: `<table><tr><td colspan=2><font color=#000000>ЗАГОЛОВОК:</font></td></tr><tr><td>&nbsp;</td><td>СОДЕРЖИМОЕ</td></tr></table>`.
  Заголовок распознаётся как `font[color=#000000]` с текстом в верхнем регистре, оканчивающимся на «:».
  Внутри — ячейки «Метка:&nbsp;`<font color=#00008f>`значение`</font>`» (иконка `but_orange_question.gif` с
  HTML-подсказкой в `title` в текст не попадает). Пары «метка: значение» разбираются по первому «:».

| Блок | Поля |
|---|---|
| ОСНОВНЫЕ ХАРАКТЕРИСТИКИ | `serial_type`, `serial_elements`, `purpose`, `distribution`, `fulltext_access` (+ `open_access`: open/partial/subscription по тексту), `funding`, `multidisciplinary`, `languages` |
| УЧРЕДИТЕЛИ / ИЗДАТЕЛ… / МЕСТО РАСПОЛОЖЕНИЯ РЕДАКЦИИ | `founders` / `publishers` (+ `publisher`) / `editorial_office`: `JournalOrgRef(id из org_profile.asp?id=, name, city из «(Город)»)` |
| СВЕДЕНИЯ ОБ ИЗДАНИИ | `issn`, `eissn`, `issues_per_year`, `foundation_year`, `articles_per_issue`, `pages_per_issue`, `publication_period` (+ `active`: «1996-...» → True), `archive`, `articles_total`, `issues_total`, `media_registration` |
| РЕДАКЦИОННАЯ ПОЛИТИКА | `editorial_policy` (абзацы `<p>` через пустую строку) |
| ИНДЕКСАЦИЯ | `indexing`: `elibrary` (+ `elibrary_contract` из «да (договор 290-10/2022)»), `risc`, `rsci`, `core_rinc`, `doaj`, `wos`/`wos_quartile`, `scopus`/`scopus_quartile`, `white_list`/`white_list_level`, `egpni`/`egpni_level`, `vak`/`vak_category`, `science_index`/`science_index_percentile`, `crossref`, `doi_prefix`, `databases` (список, запятые внутри скобок не режут) |
| КОНТАКТНАЯ ИНФОРМАЦИЯ | `country`, `city`, `address`, `email` (только из этого блока: «Официальный адрес электронной почты редакции»), `phone`, `website` (href ссылки) |
| ГЛАВНЫЙ РЕДАКТОР / ЗАМЕСТИТЕЛИ… / ОТВЕТСТВЕННЫЙ СЕКРЕТАРЬ / РЕДАКЦИОННАЯ КОЛЛЕГИЯ / прочие | `editor_in_chief`, `deputy_editors`, `executive_secretaries`, `editorial_board`, `other_staff[заголовок]`; у каждого `EditorialMember.role` = заголовок блока |
| РЕЦЕНЗИРОВАНИЕ | `peer_review`: `type`, `reviewers`, `rejection_rate` (%), `publication_time` + `publication_days`, `performed_by` |
| РУБРИКИ ГРНТИ / РУБРИКИ OECD / СПЕЦИАЛЬНОСТИ ВАК / прочие РУБРИКИ… | `rubrics_grnti`, `rubrics_oecd`, `vak_specialties`, `other_rubrics`: `Rubric(code без точки, name)` |
| ОСНОВНЫЕ РАЗДЕЛЫ | `sections`: `JournalSection(id из title_items.asp?id=..&section=ID, name, url)` |
| ДОПОЛНИТЕЛЬНЫЕ ФАЙЛЫ | `files`: `JournalFile(kind, name, file_id из show_titlefile(N))` — открыть файл можно только после входа |
| ИСТОРИЯ ПЕРЕИМЕНОВАНИЙ И ПЕРЕВОДА | `history` |
| правая панель | `last_modified` («Дата последнего изменения данных об издании: 10.03.2026») |
| неизвестный блок / метка | `extra[заголовок]` = текст блока, `extra[метка]` = значение; неизвестные метки индексации — `indexing.extra` |

**Люди** (блоки редакции): содержимое — плоская последовательность
`<font><a href=/author_profile.asp?authorid=N>ФИО</a></font> <a href='mailto:…'><img mail1.gif></a><img about_author.gif title='д.т.н.'><br><font><a href=/org_profile.asp?id=N>Организация</a></font> (Город)<br>`,
члены коллегии разделены `<div style="height:…">`. Первый `<font>` после разделителя — человек (даже без
ссылки на профиль: у части членов коллегии `author_id=None`), следующие `<font>` — места работы (тоже бывают
без ссылки). `degree` — из `title` иконки `about_author.gif` («академик РАН», «д.ф.-м.н.», «professor»),
`role` — заголовок блока («ГЛАВНЫЙ РЕДАКТОР», «РЕДАКЦИОННАЯ КОЛЛЕГИЯ»).

**Конфиденциальность (исправлено 13.10.2026).** Раньше `EditorialMember.email` хранил адрес из
`mailto:` у каждого члена редакции (в фикстуре 8610 — 27 личных адресов, в т. ч. `@gmail.com`,
`@mail.ru`), и он уходил в JSON CLI/MCP. Теперь поля нет: ссылка `mailto:` с конвертом лишь помогает
узнать строку человека, сам адрес не сохраняется (имя, роль, степень и места работы остаются).
Единственный e-mail профиля — `JournalProfile.email` из блока «КОНТАКТНАЯ ИНФОРМАЦИЯ» (публичный адрес
редакции); пара «Email:» в любом другом блоке отбрасывается, а из `extra`, `indexing.extra`,
`peer_review.extra`, `editorial_policy`, `history` адреса вырезаются (`strip_emails`).

**Показатели** (импакт-фактор и т. п.) на `title_about_new.asp` **не показываются** — они на
`title_profile.asp`. Поля `impact_factor_2y`, `impact_factor_5y`, `science_index_rating`, `h_index`,
`metrics`, `metrics_year` в `JournalProfile` заполняются `JournalProfile.with_indicators(parse_journal_indicators(...))`.
Ссылки на последний выпуск на карточке тоже нет (`latest_issue=None`); последний выпуск даёт
`contents.asp?titleid=ID`.

## 4. Показатели `title_profile.asp?id=ID` (без фикстуры)

Страница не сохранялась (бюджет запросов). Парсер универсальный: ищет таблицы, у которых в заголовочной
строке ячейка «Название показателя», и берёт строки «(иконка) | название | значение» — так устроены
`org_profile.asp` и `author_profile.asp` (на `org_profile_2541.html` тест проверяет, что читаются
«ОБЩИЕ ПОКАЗАТЕЛИ», «ПОКАЗАТЕЛИ ЗА 5 ЛЕТ» и годовые таблицы «название (год)»). Заголовок таблицы — текст
ближайшего предыдущего элемента (`<div><b>ОБЩИЕ ПОКАЗАТЕЛИ</b></div>`). Все строки → `metrics`
(повтор названия → ключ «РАЗДЕЛ: название»), известные → типизированные поля по регуляркам
`models.journals.INDICATOR_FIELDS`: «Двухлетний импакт-фактор РИНЦ», «… без самоцитирования», «Пятилетний
импакт-фактор РИНЦ», «Двухлетний импакт-фактор по ядру РИНЦ», «Показатель журнала в рейтинге SCIENCE INDEX»,
«Место в общем рейтинге SCIENCE INDEX», «Индекс Хирша», «Суммарное число цитирований журнала в РИНЦ»,
процент самоцитирования. Год — из выбранной опции `select[name*=year]` или «за NNNN год».
Названия строк взяты из сторонних парсеров и памяти о сайте — **проверить вживую**.

## 5. Содержание выпуска `contents.asp`

* `<title>`: «Вычислительные технологии / содержание выпуска  Т.31 № 4 за 2026 год» → `journal_name`, `label`.
* Шапка: «eLIBRARY ID: `<a href=contents.asp?id=91982811>`» → `issue_id`; «EDN: `<a href=/mabysk>`MABYSK» → `edn`;
  hidden `input[name=titleid]` и ссылка `title_about_new.asp?id=8610` → `journal_id`; под названием
  `<span><font><a href='org_profile.asp?id=34'>…</a></font> <i>(Новосибирск)</i></span>` → `publisher`;
  `<div>Том: <b>31</b> Номер: <b>4</b> Год: <b>2026</b></div>` → `volume`, `number`, `year` (строки, т. к.
  номера бывают `S1`).
* `table#restab` (столбцы «Название статьи | Стр. | Цит.»): строки рубрик `<tr><td colspan=4><b>РУБРИКА</b></td></tr>`
  → `sections` и `PublicationHit.section`; строки `tr#arw<ITEMID>` разбирает `parsers.rows.parse_publication_rows`
  (`pages` из колонки «Стр.», `citations`). Библиографической строки нет, поэтому `kind` ставится
  `"article"`, `year` — год выпуска, `position` — порядковый номер (в первой ячейке только чекбокс).
* Правая панель «Список выпусков»: `Online First` (до первого года, `year=None`), затем для каждого года
  строка-заголовок и `<tbody id="issueyearYYYY">` со строками `<a href="contents.asp?id=N">Т. 31 № 1</a> (9 ст.)`.
  Текущий выпуск — без ссылки (`<span><font color=#F26C4F>Т. 31 № 4</font></span> (10 ст.)`), ему
  присваивается `issue_id` и `current=True`; его «(N ст.)» → `articles_count`. В фикстуре 223 элемента
  (Online First + 222 выпуска, как «Всего выпусков на eLIBRARY.RU: 222»), годы 2026→1996, спецвыпуски `№ S1..S3`.
* `extra`: «Стоимость выпуска» («Приобрести этот выпуск за 1633 руб.»), «Подписка на журнал» (опции `subs_year`).
* `IssueContents.url` — всегда канонический `contents.asp?id=ISSUEID` (даже если загружали по `titleid`).
* Живая проверка 13.10.2026 (`contents_8559.html`, «Вестник Томского государственного университета»,
  последний выпуск по `titleid=8559`): `<title>` «… / содержание выпуска № 526 за 2026 год», тома нет
  (шапка «Номер: 526 Год: 2026» → `volume=None`, `number="526"`), EDN у выпуска нет (`edn=None`),
  издатель — `org_profile.asp?id=366` «(Томск)». 29 статей в 7 рубриках, включая англоязычную рубрику
  «PHILOLOGY» с одной статьёй (это отдельная строка-рубрика на странице, не ошибка разбора); авторы в
  содержании — полные ФИО («Агнистикова Ольга Игоревна»); страницы «5-12» … «267-273». Список выпусков:
  279 элементов (Online First + 278 выпусков 2026→1998, внутри года — в порядке страницы, № 522…526).

## 6. Особенности и ловушки

* В lexbor `node.css(sel)` / `css_first(sel)` **включают сам узел**, если он подходит под селектор:
  `td.css_first("td")` никогда не `None`. Для проверки «лист без вложенных ячеек» используется `_has_desc`.
* Фикстуры с переводами строк CRLF — тесты, которые правят HTML строкой, должны это учитывать.
* Названия в шапках страниц — ПРОПИСНЫМИ; нормальный регистр есть только в `<title>`.
* Имя журнала может содержать « / », поэтому `<title>` режется по последнему вхождению.
* Ошибки: страница не того типа → `ParseError`; текст «не найден»/«не существует» на карточке/выпуске без
  данных → `NotFound` (точный текст страницы «журнал не найден» неизвестен — эвристика).

## 7. Не проверено / открытые вопросы

* `title_profile.asp` — нет фикстуры, парсер написан по аналогии (см. §4).
* Английская версия карточки (`&langid=2`) — нет фикстуры.
* Пагинация каталога журналов и число журналов на странице — в фикстуре 4 результата, пагинатора нет.
* Многостраничное содержание больших выпусков (конференций) — разбирается по общим правилам, на реальной
  странице не проверено. Выпуск без тома («№ 526») проверен на `contents_8559.html`.
* Блоки карточки, которых нет у журнала 8610 (ИЗДАТЕЛЬСТВО, РЕДАКЦИОННЫЙ СОВЕТ, рубрики ASJC, категория ВАК
  «К1»), — обработаны по названиям заголовков, вживую не проверены.
