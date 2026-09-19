"""Механизм 1: интерпретация космической погоды, уровни, прогноз.

Три независимые линии одного механизма, ни одна из которых не импортирует
другую — комбинирует их только ``src/api/service.py``:

- ``external_forecast.py`` (FN-31) — внешний суточный прогноз-вероятность
  NOAA 3-Day (``mode=current``);
- ``observed_classifier.py`` (FN-38) — классификация НАБЛЮДЕНИЯ (поток
  протонов GOES) по шкале S NOAA (``mode=current``);
- ``archive_assessment.py`` (FN-42) — provider-agnostic архивная СОБЫТИЙНАЯ
  оценка для исторических окон (``historical_analysis``/
  ``historical_forecast``), у которой нет ни измеренной величины, ни
  вероятности, только три состояния EVENT_PRESENT / NO_EVENT_DETECTED /
  INSUFFICIENT_DATA. Какой именно архивный продукт за ней стоит, модуль не
  знает: перечень событийных типов и фактический горизонт приходят
  политикой из ``sources.yaml`` (FN-41 подключает её к production API через
  ``src/sources/archive_ingest.py``, не через собственный коннектор).
"""
