# NASA MEO — 2024 meteor shower activity forecast for LEO (FN-39)

Реальный первичный файл, полученный владельцем задачи FN-39 независимо от
этой сессии (прямая загрузка с `ntrs.nasa.gov` недоступна из песочницы —
сетевой прокси отвечает `403 connect_rejected` на этот и другие домены
первоисточников, см. `docs/mechanisms.md` §11.3/§12) и приложенный к
Jira-задаче FN-39. Байты проверены в этой сессии: SHA-256 совпадает с
контрольной суммой, зафиксированной в комментарии владельца задачи, и с
контрольными строками (см. ниже) — до сохранения в репозиторий.

**Сам файл (`flux_data.txt`) в этой папке не лежит.** Единственная копия в
репозитории — `src/sources/data/mmod/nasa_meo_leo_forecast_2024/flux_data.txt`
(под `src/`, не `tests/`/`data/` — оба исключены `.dockerignore`, а
работающему сервису файл нужен по-настоящему, см.
`src/sources/mmod.py::DEFAULT_DATA_PATH`). Изначально этот README лежал
рядом со вторым, тестовым экземпляром файла; он был удалён (round 1 ревью
PR #31 — дублирование ~8800-строчного файла в двух местах раздувало диф PR
до 19933 строк, нечитаемо для ревью). Эта папка документирует происхождение
и контрольную сумму; `tests/sources/test_mmod.py` проверяет их против
единственной копии напрямую (`src.sources.mmod.DEFAULT_DATA_PATH`).

## Источник

| Поле | Значение |
| --- | --- |
| Документ | *The 2024 meteor shower activity forecast for low Earth orbit* |
| Поставщик | NASA Meteoroid Environment Office (MEO), Marshall Space Flight Center |
| NTRS record | 20230015158 |
| URL цитирования | `https://ntrs.nasa.gov/citations/20230015158` |
| URL файла | `https://ntrs.nasa.gov/api/citations/20230015158/downloads/flux_data.txt` |
| Issued | 2023-11-02 (титульный лист сопроводительного PDF `LEO_Forecast_2024.pdf`, тоже приложенного к FN-39) |
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
independently вычислена в этой сессии над сохранёнными байтами.

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
  `LEO_Forecast_2024.pdf` §2 «Details») прямо указывает: «for a surface
  directly facing the shower, this can further boost the significance… by
  another factor of approximately 2», «it is possible for the Earth to
  shield the spacecraft from all or part of a shower» — то есть `factor`
  уже соответствует худшему случаю (полностью открытый, направленный на
  радиант приёмник), а не траекторной оценке конкретной станции. Поэтому
  `ratio_to_background` этой задачи НЕ умножается на
  `effective_flux_ratio`/результат экранирования
  (`src/domain/mmod/geometry.py`, FN-32) — они выводятся отдельно как
  `trajectory_context`, результат помечается `worst_case_unshielded_leo` и
  `not_spacecraft_surface_specific` (решение владельца задачи, FN-39).
