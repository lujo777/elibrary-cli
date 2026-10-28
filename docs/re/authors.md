# Авторы: `authors.asp`, `author_profile.asp`, `author_items.asp`

Заметки области «авторы» (октябрь 2026). Всё ниже установлено по сохранённым страницам
`tests/fixtures/html/` (`authors_form.html`, `authors_results_petrov.html`, `author_profile_3771.html`,
`author_items_3771.html`; после живой проверки 13.10.2026 — `author_profile_17783.html` и
`author_items_17783_p2.html`, вторая страница списка, полученная POST-ом), по телу живого POST-запроса,
которым была получена страница «Петров»
(`scratchpad/re/pages/authors_petrov.meta.json`), и по двум сторонним снимкам из открытых проектов
(профиль и список публикаций AuthorID 1012909, старая страница списка публикаций AuthorID 679517) —
последние использовались только для проверки устойчивости парсера, в тесты не входят.
**Ни одного запроса к elibrary.ru при разработке не отправлялось.**

Код: `models/authors.py`, `queries/authors.py`, `parsers/authors.py`, `data/authors_options.json`,
тесты — `tests/test_authors.py`.

## 1. Поиск авторов: `POST /authors.asp`

Форма `results` (JS `author_search()` — новый поиск, `goto_page(N)` — страница N). Состояние поиска на
сервере **не хранится**: каждая страница — тот же POST с `pagenum=N`. Живой запрос 13.10.2026 (страница 1):

```
authors_all=&pagenum=&authorbox_name=&selid=&orgid=&orgadminid=&townid=&regionid=&surname=Петров
&codetype=SPIN&codevalue=&countryid=&town=&orgname=&authorboxid=&rubriccode=&metrics=1&sortorder=2&order=1
```

Тег `<FORM>` стоит внутри `<table>`, поэтому HTML-парсер закрывает его сразу; браузер всё равно
связывает с формой все последующие элементы управления (form element pointer). Порядок полей
`FormData` = порядок элементов в DOM (проверено тестом по `authors_form.html`):

| № | Поле | Тип | Значение / смысл | `AuthorQuery` |
|---|---|---|---|---|
| 1 | `authors_all` | hidden | `""` (служебное, для перехода в `querybox.asp`) | — |
| 2 | `pagenum` | hidden | `""` для нового поиска, `N` для `goto_page(N)` | `page` |
| 3 | `authorbox_name` | hidden | `""` | — |
| 4 | `selid` | hidden | `""` | — |
| 5 | `orgid` | hidden | id организации (заполняется окном «Выбрать», `user_org_add3.asp`) | `org_id` |
| 6 | `orgadminid` | hidden | `""` | — |
| 7 | `townid` | hidden | id города из автодополнения `town_autocomplete2.asp?q=..&scountry=..` | `town_id` |
| 8 | `regionid` | hidden | id региона (оттуда же) | `region_id` |
| 9 | `surname` | text | фамилия / ФИО / фамилия и инициалы | `surname` |
| 10 | `codetype` | select | `SPIN`, `ORCID`, `ResearcherID`, `ScopusID`, `ArXivID`, `RSCI` (РИНЦ AuthorID) | `code_type` |
| 11 | `codevalue` | text | значение идентификатора | `code_value` |
| 12 | `countryid` | select | ISO3 (`RUS`, …), пусто — любая | `country` |
| 13 | `town` | text | название города | `town` |
| 14 | `inaffil` | checkbox | `on` — «Искать в аффилиациях авторов в публикациях» | `in_affiliation` |
| 15 | `orgname` | textarea (readonly) | название выбранной организации | `org_name` |
| 16 | `authorboxid` | select | референтная группа `r1`…`r5` | `author_group` |
| 17 | `rubricinreg` | checkbox | `on` — «Учитывать рубрики из анкеты автора» | `rubric_in_profile` |
| 18 | `rubriccode` | select | код ГРНТИ верхнего уровня (`290000` — Физика) | `rubric_code` |
| 19 | `metrics` | select | `0` ядро РИНЦ, `1` РИНЦ (по умолчанию), `2` elibrary.ru | `metrics` = core/rinc/elibrary |
| 20 | `show_nopubl` | checkbox | `on` — «показывать **только** авторов, имеющих публикации» | `only_with_publications` |
| 21 | `sortorder` | select | `0` фамилия, `1` публикации, `2` цитирования (по умолчанию), `3` индекс Хирша | `sort` = name/publications/citations/hindex |
| 22 | `order` | select | `0` возр., `1` убыв. (по умолчанию) | `order` = asc/desc |

Чекбоксы уходят как `on` только когда отмечены, на своей позиции в DOM. Обратите внимание: имя
`show_nopubl` вводит в заблуждение — отмеченный чекбокс **скрывает** авторов без публикаций, поэтому
поле модели названо `only_with_publications`.

`reset_form()` выставляет `metrics=1`, `codetype=SPIN`; `onChange` у `sortorder` меняет `order`
(0→0, 1/2/3→1) и знает скрытое значение `4` (ставит `metrics=0`) — в списке его нет, не поддерживаем.

Валидация `AuthorQuery`: нужен хотя бы один селективный критерий — `surname`, `code_value`, `org_id` или
`author_group` (референтная группа — самостоятельная выборка). `org_name` без `org_id` сама модель не
считает критерием: сайт фильтрует **только** по `orgid`, название — лишь текст поля (проверено вживую
13.10.2026: `surname=Иванов` + `orgname=томский политехнический` без `orgid` вернул всех 8423 Ивановых —
из Гатчины, Пущино…). Поэтому `ElibraryClient.search_authors` при `org_name` без `org_id` валидирует запрос
с контекстом `CONTEXT_RESOLVE_ORG_NAME` (тогда название — критерий), делает **один** поиск организаций
(`orgs.asp`, `orgname=<название>`, сортировка по числу публикаций, по убыванию; кэш 24 ч) и берёт лучшее
совпадение: организацию, нормализованное название которой (регистр, «ё», кавычки, пунктуация не
важны) или сокращение в скобках («Российский университет транспорта (МИИТ)») совпадает с запросом, иначе —
организацию с наибольшим числом публикаций на первой странице (до 20). В форму уходят её `orgid` и её
название в `orgname` (как у браузера). Выбор возвращается в `SearchPage.query["org_resolved"]` =
`{id, name, town, publications, match: exact|most_publications, candidates: всего найдено организаций,
query: исходное название}`; если организаций не нашлось — `NotFound` («Организация «…» не найдена…»),
поиск авторов не отправляется. CLI печатает выбранную организацию в stderr, MCP-инструмент возвращает её
в `query.org_resolved`.

Страна (`countryid`) — код ISO 3166-1 alpha-3 (любой регистр) или название: русская подпись из формы
(`data/authors_options.json`: «Россия», «Республика Корея»), английское название или распространённое
сокращение из `models.authors.COUNTRY_ALIASES` («Russia», «Kazakhstan», «UK», «UAE», «Корея», «РФ»);
разбирает `models.authors.resolve_country` прямо в валидаторе модели (CLI и MCP больше не переводят
страну сами). Принимается и единственное название, начинающееся с текста / содержащее текст («Казах»);
неоднозначное («Ир» — Ирак, Иран, Ирландия) и неизвестное — ошибка с подсказкой; трёхбуквенный латинский
код, которого нет в снимке, передаётся как есть. Рубрика — 6 цифр (`int` дополняется нулями), `RSCI` —
только цифры.

Списки значений всех select-полей сохранены в `data/authors_options.json`
(`{"поле": [{"value", "label", "count"?}]}`; число в скобках у стран, рубрик и групп вынесено в `count`,
пустой вариант «любой» не включён, двоеточие в конце меток `codetype` убрано). Тест сверяет JSON с
фикстурой формы.

### Страница результатов

* Счётчик: `Всего найдено авторов: <b>4342</b> из <b>1170544</b>. Показано на данной странице: с <b>1</b>
  по <b>100</b>.` → `total`, `total_in_database`; диапазон используется для `per_page`.
* Пусто: «Не найдено авторов, удовлетворяющих условиям поиска» (так выглядит и сама форма без
  параметров) → `total=0`, `pages=0`.
* Таблица `table#restab`, строки `tr#a<AuthorID>`, 6 ячеек:
  1. номер `<b>1.</b>` → `position`;
  2. чекбокс `tid<ID>`;
  3. `<font><b>Петров&nbsp; Владимир&nbsp; Алексеевич</b></font>`, звёздочка
     `<span class="aster" title="Автор зарегистрирован в SCIENCE INDEX">*</span>`
     → `science_index_registered`, необязательная картинка `img[data-tooltip]` со ссылкой на Википедию
     → `wikipedia_url`, после `<br>` — «Организация (Город)»: город — последняя группа в скобках в
     конце (скобки внутри названия сохраняются: «Университет Уэйна (Детройт, США) (Детройт)»,
     «Российский университет транспорта (МИИТ) (Москва)»);
  4. `<a href="author_items.asp?authorid=ID&show_refs=1">1069</a>` + ссылка на профиль
     `author_profile.asp?id=ID` → `publications`, `publications_url`, `profile_url`;
  5. `<a href="javascript:show_author_refs(ID, 'risc')">86835</a>` → `citations`;
  6. индекс Хирша → `hindex`.
  Значения столбцов 4–6 рассчитаны по базе из `metrics` (заголовки: «Число публикаций автора в РИНЦ» и т.п.).
* После каждой строки — `<input type=hidden name="hid<ID>" value="Петров В А">` (фамилия и инициалы без
  точек; так сайт передаёт автора в фильтр `authors_all` поиска публикаций) → `short_name`.
  Эти hidden-поля тоже входят в форму `results`, но при листании их передавать не нужно (живой запрос
  страницы 2 без них дошёл до сервера и упёрся только в капчу).
* Пагинация `div#pages`: текущая страница — `<font color=#F26C4F>1</font>`, остальные —
  `javascript:goto_page(N)`, «В конец» — `goto_page(44)`; `pages` = максимум номеров. Если блока нет,
  `pages` считается из `total` и `per_page` (100).

## 2. Профиль автора: `GET /author_profile.asp?id=ID`

`<title>Петров Владимир Алексеевич / анализ публикационной активности</title>` — источник имени в
обычном регистре (запасной вариант — заглавное имя из шапки через `.title()`).

### Шапка

```html
<font color=#F26C4F><b>ПЕТРОВ ВЛАДИМИР АЛЕКСЕЕВИЧ</b></font> <span class="aster">*</span>
<br><a href="org_profile.asp?id=169">Институт физики высоких энергий ...</a> (Протвино)
<br>SPIN-код: <font>9846-9426</font>, AuthorID: <font>3771</font>
```

Шапка режется по `<br>`: строка с `SPIN-код:` / `AuthorID:` → `spin`, `id`; первая прочая строка →
`affiliation` (организация из ссылки, id, город из скобок, подразделение — текст между ссылкой и
скобками: «…РАН</a>, Институт социальной демографии (Москва)» → `department`). Остальные строки (если
появятся, например должность) — в `extra["Заголовок"]`. Если `AuthorID` в шапке нет — берётся hidden
`authorid` формы `results`, затем `id` из URL.

### «МЕСТО РАБОТЫ»

Таблица после заголовка `<b>МЕСТО РАБОТЫ</b>`: «Название организации | Период | Публ.»; строка —
`<a href="org_profile.asp?id=386">…</a> (Москва)` | `2021-2026` (или один год `2023`) |
`<a href="author_items.asp?orgsid=386">86</a>` → `affiliations[]` (`org_id`, `town`, `period`,
`year_from`, `year_to`, `publications`, `publications_url`). Ссылка `author_items.asp?orgsid=` идёт без
`authorid` — вероятно, автор берётся из сессии (не проверялось).

### «ОБЩИЕ ПОКАЗАТЕЛИ»

Таблица «Название показателя | Значение», в каждой строке 3 прямые ячейки `td`: иконка-подсказка
(`img.help`, `title` — пояснение), метка `<font color=#00008f>`, значение (иногда ссылка на список
публикаций/цитирований, иногда с процентом: `929 (86,9%)`, `<a>137</a> (12,8%)`). Строки-разделители
(`colspan=3`) пропускаются. «Основная рубрика (ГРНТИ/OECD)» лежат во **вложенной** таблице. Блок
«Участие в публикациях:» — строка с пустым значением, за ней строки без иконки (автор, редактор,
научный руководитель, научный консультант) → `roles` и метрики «Участие в публикациях: автор» и т.д.

Каждая строка сохраняется в `metrics[]` (`name`, `value` как на странице, `number`, `percent`, `url`).
Типизированные поля (метка → поле; хвост «(2021-2025)» у 5-летних показателей отрезается и кладётся в
`five_year_period`):

| Метка | Поле |
|---|---|
| Число публикаций на elibrary.ru / в РИНЦ / входящих в ядро РИНЦ | `publications_elibrary` / `publications_rinc` / `publications_core` |
| Число цитирований из публикаций на elibrary.ru / входящих в РИНЦ / в ядро РИНЦ | `citations_elibrary` / `citations_rinc` / `citations_core` |
| Индекс Хирша по всем публикациям на elibrary.ru / по публикациям в РИНЦ / по ядру РИНЦ | `hindex_elibrary` / `hindex_rinc` / `hindex_core` |
| Число публикаций, процитировавших работы автора | `citing_publications` |
| Число ссылок на самую цитируемую публикацию | `top_publication_citations` |
| Число публикаций автора, процитированных хотя бы один раз | `cited_publications` |
| Среднее число цитирований в расчете на одну публикацию | `citations_per_publication` (float) |
| Индекс Хирша без учета самоцитирований / по ядру РИНЦ без учета самоцитирований / с учетом только статей в журналах | `hindex_without_self_citations` / `hindex_core_without_self_citations` / `hindex_journal_articles` |
| Год первой публикации | `first_publication_year` |
| Число самоцитирований / цитирований соавторами / соавторов | `self_citations` / `coauthor_citations` / `coauthors` |
| Число статей в зарубежных / российских / из перечня ВАК / переводных / с ненулевым ИФ журналах | `articles_foreign_journals` / `articles_russian_journals` / `articles_vak_journals` / `articles_translated_journals` / `articles_impact_factor_journals` |
| Число цитирований из зарубежных журналов на публикации автора (на части страниц метка обрезана до «…автор») | `citations_from_foreign_journals` (по префиксу) |
| Число цитирований из российских / из перечня ВАК / переводных / с ненулевым ИФ журналов | `citations_from_russian_journals` / `citations_from_vak_journals` / `citations_from_translated_journals` / `citations_from_impact_factor_journals` |
| Средневзвешенный импакт-фактор журналов, в которых были опубликованы / процитированы статьи | `weighted_impact_factor_published` / `weighted_impact_factor_cited` |
| Число публикаций в РИНЦ / в ядре РИНЦ за последние 5 лет | `publications_rinc_5y` / `publications_core_5y` |
| Число ссылок из РИНЦ / из ядра РИНЦ на работы, опубликованные за последние 5 лет | `citations_rinc_5y` / `citations_core_5y` |
| Число ссылок на работы автора из всех публикаций за последние 5 лет | `citations_all_5y` |
| Основная рубрика (ГРНТИ) / (OECD) | `main_rubric_grnti` / `main_rubric_oecd` (строка «290000. Физика») |
| Процентиль по ядру РИНЦ | `core_percentile` |

Незнакомые метки попадают в `extra` (и, как все, в `metrics`). Целые числа с процентом берутся без
процента (`929 (86,9%)` → 929), десятичная запятая поддерживается.

### «ПОКАЗАТЕЛИ ПО ГОДАМ»

`div#years_ind table`: заголовок `<th>Название показателя</th>` + `<td>2025</td>…<td>1972</td>`
(годы по убыванию), далее строки «метка | значение по каждому году» (метки с `<br>` внутри склеиваются
пробелом; пустая ячейка → `None`). 11 строк: публикации и цитирования в РИНЦ/ядре, то же «за 5 лет»,
индекс Хирша в РИНЦ/по ядру, процентиль. → `years`, `yearly[] = {name, values: {год: число}}`.

### Прочее

* «СТАТИСТИЧЕСКИЕ ОТЧЕТЫ»: ссылки `javascript:author_profile("rubrics")` и т.п. (22 ключа), JS открывает
  в iframe `author_profile_new_<key>.asp?id=ID` → `reports[] = {key, title, url}`. Берутся только
  текстовые ссылки: у иконок в третьем блоке ключ иногда другой (`cit_rubrics` у картинки, `ref_rubrics`
  у текста — опечатка на сайте).
* Панель «ИНСТРУМЕНТЫ»: `author_items.asp?authorid=ID`, `cit_author_items.asp?id=ID`
  (→ `citing_publications_url`), внешние профили: `https://orcid.org/<ORCID>` (→ `orcid`),
  `https://www.scopus.com/authid/detail.url?authorId=<id>` (→ `scopus_id`),
  `https://www.webofscience.com/wos/author/record/<RID>` («Профиль автора в ResearcherID»,
  → `researcher_id`); все — в `external_profiles`.
* «Дата обновления показателей автора: 07.10.2026» → `metrics_updated` (date).
* Два абзаца-оговорки о расчёте показателей РИНЦ не сохраняются (не пары «метка: значение»).

## 3. Публикации автора: `author_items.asp`

* Первая страница — `GET /author_items.asp?authorid=ID&show_refs=1` (ссылка из результатов поиска).
  Профиль ссылается и на отфильтрованные GET-списки: `pubcat` (`risc`, `corerisc`, `vak`), `pubrole`
  (`100` автор, `0` редактор, `26` научный руководитель, `27` научный консультант), `pubyear` (год или
  годы через `|`) — см. `queries.authors.author_items_params`.
* Следующие страницы — **POST** `/author_items.asp` формы `results` (JS `goto_page(N)`; `pub_search()`
  отправляет то же с `pagenum=1`). Порядок полей (DOM, проверено тестом):
  `itembox_name, auth, authorid, authorhash, pagenum, add_all, paysum, slight, show_refs, hide_doubles,
  items_all, publs_all, did, urlnum, listid, rubric_order, title_order, org_order, author_order,
  year_order, type_order, cat_order, role_order, keyword_order, show_hash, [check_show_refs],
  [check_hide_doubles], sortorder, order`. Значения по умолчанию со страницы: `did=1`, `urlnum=1`,
  `year_order=1`, `cat_order=1`, остальные `*_order=0`, `show_hash=0` (1 — «непривязанные публикации,
  которые могут принадлежать автору»), чекбоксы `check_show_refs` / `check_hide_doubles` отмечены
  (их `onClick` синхронизирует hidden `show_refs` / `hide_doubles`). `authorhash` — «Петров В А».
  Чекбоксы строк `tid<ID>` / `pid<ID>` не отмечены и не отправляются.
* `sortorder`: `0` дата выпуска (по умолчанию, убыв.), `1` название, `2` год и первый автор, `6` год и
  название, `5` первый автор, `3` цитирования, `4` дата размещения, `7` тематическая близость; `order`
  `0`/`1`.
* Фасеты (тематика, журналы, организации, авторы, годы, типы, категории, участие, ключевые слова)
  подгружаются AJAX-ом (`author_items_rubrics.asp?id=ID&order=…&selids=…&show_hash=0&show_refs=1&hide_doubles=1`
  и т.п.) как чекбоксы `rubrics_<id>`, `titles_<id>`, …; их выбор **не поддерживается** (нет фикстуры
  фрагментов).

### Разбор страницы

* `<title>Петров Владимир Алексеевич - Список публикаций</title>` (старый вариант:
  `eLIBRARY.RU - Имя - Список публикаций`) → `author_name`; hidden `authorid`, `authorhash`.
* Шапка как в профиле, но ссылка организации `org_about.asp?orgsid=169` → `affiliation`.
* `Всего найдено <b>1094</b> публикации с общим количеством цитирований: <span id="totalcited"></span>.
  Показано на данной странице: с 1 по 100.` — `span#totalcited` заполняет скрипт после таблицы
  (`document.getElementById('totalcited').innerHTML=86020;`); в сохранённых браузером страницах число уже
  в span. Согласуется **и глагол, и существительное**: «Всего найдено 1094 публикации», но «Всего найдена
  2581 публикация» (`author_items_17783_p2.html`). Живая проверка 13.10.2026 вернула `total=None` для
  AuthorID 17783 именно из-за «найдена»: теперь принимается любая форма «найден…» (и вариант
  «Всего найдено публикаций: N»).
* Строки `tr#arw<ID>` разбирает `parsers.rows.parse_publication_rows` (99 на странице фикстуры).
  Строки `tr#brw<ID>` — «Публикация из списков цитируемой литературы» (иконка `/images/ref.gif`): у них
  нет `item.asp`, id — номер ссылки, не публикации. Они идут в `references[]`
  (`AuthorReferenceHit`), а не в `items`, но занимают номер в нумерации (88 в фикстуре).
  Год такой строки — `parsers.rows.source_year`, как у остальных строк: страницы «С. 1901-1909»
  годом не считаются (раньше здесь стоял `first_year`, и год мог стать 1909).
* Пагинация — как у поиска авторов (`div#pages`, 11 страниц в фикстуре). На старой странице было 20
  записей на страницу («с 41 по 60» на 3-й странице): `per_page` вычисляется из диапазона.
* Вторая страница (`author_items_17783_p2.html`, POST с `pagenum=2`): «с 101 по 200», подсвечена `2`,
  «В конец» — `goto_page(26)`; 100 строк `tr#arw…` с номерами 101–200, итого 2581, цитирований 19381.

## 4. Не проверено / открытые вопросы

* Поиск по `codetype`/`codevalue`, по `orgid` / `inaffil`, по городу без `townid`, по стране, рубрике
  и референтной группе живьём не выполнялся — порядок и имена полей взяты из формы. Живьём проверено
  только, что `orgname` без `orgid` не фильтрует (см. §1); поиск с подставленным `orgid` после
  исправления живьём ещё не запускался.
* Ответ `town_autocomplete2.asp` (источник `townid`/`regionid`) не сохранён.
* Как выглядит профиль несуществующего автора — неизвестно; парсер бросает `NotFound` при тексте
  «автор не найден»/«не существует», иначе `ParseError`.
* Листание `author_items.asp` POST-ом работает (живьём 13.10.2026, страница 2 из 26 AuthorID 17783);
  неизвестно, сохраняются ли GET-фильтры `pubcat`/`pubyear`/`pubrole` при POST-листании.
* Страницы отчётов `author_profile_new_<key>.asp` (распределения по рубрикам, журналам, соавторам…) не
  сохранены и не разбираются: в модели только ссылки.
* Подразделение после организации в шапке есть в живом профиле 17783 («…СО РАН</a>, лаборатория плазменной
  эмиссионной электроники (Томск)» → `department`), там же 21 строка «МЕСТО РАБОТЫ» (116, 573, 915, 2378…),
  ResearcherID и Scopus ID. Профиль без SPIN и с должностью видели только в синтетических тестах (по
  разметке сторонних снимков).
