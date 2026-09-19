# NASA MEO — 2024 meteor shower activity forecast for LEO (FN-39)

Реальный первичный файл, полученный владельцем задачи FN-39 независимо от
той сессии (тогда прямая загрузка с `ntrs.nasa.gov` была недоступна) и
приложенный к Jira-задаче FN-39. В FN-40 официальный metadata endpoint
NTRS стал доступен и сохранён отдельно для доказательства времени выпуска.
Байты таблицы проверены: SHA-256 совпадает с
контрольной суммой, зафиксированной в комментарии владельца задачи, и с
контрольными строками (см. ниже) — до сохранения в репозиторий.

**Сам файл (`flux_data.txt`) в этой папке не лежит.** Единственная копия в
репозитории — `src/sources/data/mmod/nasa_meo_leo_forecast_2024/flux_data.txt.gz`
(под `src/`, не `tests/`/`data/` — оба исключены `.dockerignore`, а
работающему сервису файл нужен по-настоящему, см.
`src/sources/mmod.py::DEFAULT_DATA_PATH`) — хранится **gzip-сжатой**, не
plain text: построчный текстовый файл на ~8800 строк раздувал диф PR до
19933 строк при round 1 ревью (лимит 3000), и даже после дедупликации
(удаления второго, тестового экземпляра) — до 11154 строк при round 2,
потому что GitHub считает additions/deletions по собственному
content-sniffing содержимого, не по `.gitattributes` клиента (`-diff` на
текстовой копии не влияет на этот счётчик). Настоящий gzip — бинарный для
любого такого детектора, диф показывает `Bin … bytes`. `src.sources.mmod.fetch()`
распаковывает на лету и отдаёт те же самые байты, что проверены ниже;
`tests/sources/test_mmod.py` проверяет контрольную сумму РАСПАКОВАННОГО
содержимого против единственной копии (`src.sources.mmod.DEFAULT_DATA_PATH`
→ `fetch()`), не самого `.gz`-файла.

## Источник

| Поле | Значение |
| --- | --- |
| Документ | *The 2024 meteor shower activity forecast for low Earth orbit* |
| Поставщик | NASA Meteoroid Environment Office (MEO), Marshall Space Flight Center |
| NTRS record | 20230015158 |
| URL цитирования | `https://ntrs.nasa.gov/citations/20230015158` |
| URL файла | `https://ntrs.nasa.gov/api/citations/20230015158/downloads/flux_data.txt` |
| Published | `2023-11-02T05:00:00Z` — совпадающие официальные поля NTRS `distributionDate` и `publications[0].publicationDate`; это не полночь, восстановленная из надписи `Issued November 2, 2023` |
| Методика | Moorhead et al., *Meteor shower forecasting in near-Earth space*, Journal of Spacecraft and Rockets 56(5):1531–1545, 2019 (NTRS 20190030373) — заявлена в PDF как неизменная методика на этот выпуск |
| Лицензия | Данные NASA — как правило, public domain (U.S. Government work); MEO не указывает дополнительных ограничений в самом документе |

## Файл

`flux_data.txt` — почасовая таблица на весь 2024 календарный год (плюс одна
строка `2025-01-01 00:00`..`02:00`..`06:00` — фактически 8791 строка данных
после 5 строк заголовка), колонки (пробел-разделитель, фиксированный формат
NASA MEO):

```
UT date  UT time  Julian date  solar lon  zhr  flux[6.7J]  flux[105J]  flux[2.83kJ]  flux[105kJ]  factor[6.7J]  factor[105J]  factor[2.83kJ]  factor[105kJ]
```

`factor[E]` — безразмерное относительное повышение потока частиц с
кинетической энергией `E` над спорадическим фоном той же энергии
(`docs/mechanisms.md` §12; в PDF — Figure 3 «flux enhancement (%)», то же
число как доля, не проценты). Эта задача (FN-39) использует только колонку
`factor 1.05e+02 J` (105 Дж, ~0.1-см-эквивалентная частица) — в PDF это
«грубый порог структурного повреждения», единственная колонка, для которой
PDF приводит полный почасовой ряд на весь год (Figure 3 отображает только
6.7 Дж и 105 Дж; 2.83 кДж/105 кДж есть только в `flux_data.txt`, не
используются здесь).

**SHA-256:** `c7b1abc031c04f04255f9c7f40ec2f1612ce2f921e06c5d34be5b39149700a1f`

Соответствует контрольной сумме из комментария владельца задачи в FN-39 и
независимо вычислена в этой сессии над сохранёнными байтами (SHA-256
распакованного содержимого — сжатие gzip побайтово обратимо).

## Доказательство времени публикации

Официальный JSON NTRS `GET /api/citations/20230015158` получен с HTTP 200
2026-09-19 и сохранён как нормализованный HTTP metadata-sidecar:
`src/sources/data/mmod/nasa_meo_leo_forecast_2024/ntrs-citation-20230015158.meta.json`.
Он фиксирует URL, HTTP status/content type/size/ETag, SHA-256 сырого тела,
поля `distributionDate`/`publicationDate` и ссылки на PDF/таблицы.

- SHA-256 сохранённого sidecar:
  `e448b15818e22455a0324c19226a81ac53ca001c5ff30b497bba7c0d4d2b7846`;
- SHA-256 сырого HTTP response body, записанный в sidecar:
  `42eeb330bfc9064c2d2de10388c6088ad6f51c88098cefbe37c57e26c99b1d30`;
- доказанный `published_at`: `2023-11-02T05:00:00Z`.

Checksum sidecar входит в `source_version`; ссылка и оба checksum едут в
`spatial_context` записи. Поэтому от любой записи можно дойти не только до
таблицы через `raw_ref`, но и до отдельного доказательства времени выпуска.

## Контрольные строки (проверены в этой сессии посимвольно)

| UT дата/время | `factor 1.05e+02 J` | `ratio_to_background = 1 + factor` |
| --- | --- | --- |
| 2024-05-05 14:00:00 | `3.165592e-01` | `1.3165592` → Повышенный (elevated) |
| 2024-06-09 23:00:00 | `5.820643e-01` | `1.5820643` → Повышенный (elevated) |

Оба радианта соответствуют максимумам потоков обязательного периода —
эта-Аквариды (пик 2024-05-05 13:53 UT, Table 2 PDF) и дневные Ариетиды
(пик 2024-06-09 22:39 UT, Table 2 PDF) — той же паре потоков, что уже
зарегистрирована качественно в `sources.yaml#imo-shower-calendar-eta-aquariids`
и упомянута в `.ai/main-prompt.md` §11 «Потоки в обязательном периоде».

## Что важно для реализации (`src/sources/mmod.py`, `src/domain/mmod/background.py`)

- Таблица покрывает СТРОГО 2024-01-01T00:00Z .. 2025-01-01T06:00Z (последняя
  строка). Запрос за пределами этого диапазона — явный critical gap
  (`missing_data`), без экстраполяции (main-prompt.md §2, FN-39 приёмка п.3).
- Между соседними часовыми узлами — явно версионированная линейная
  интерполяция (`src/domain/mmod/background.py::_INTERPOLATION_VERSION`),
  зафиксированная решением владельца задачи в FN-39.
- PDF (`meteor-shower-forecasting-near-earth-space.pdf`, методика; и
  `LEO_Forecast_2024.pdf` §2 «Details») прямо указывает влияние ориентации
  поверхности и экранирования Землёй. Поэтому `factor` хранится как
  опубликованный unshielded/radiant-facing reference, но НЕ называется
  абсолютным worst case для конкретной поверхности: `orientation_unmodeled`,
  `damage_response_unmodeled`, `earth_shielding_unmodeled` и
  `not_spacecraft_surface_specific` явно равны `true`.
- `ratio_to_background` этой задачи НЕ умножается на
  `effective_flux_ratio`/результат экранирования
  (`src/domain/mmod/geometry.py`, FN-32) — они выводятся отдельно как
  `trajectory_context`: для корректного численного объединения агрегированного
  NASA factor с конкретным радиантом, поверхностью и damage response данных
  пока недостаточно.
