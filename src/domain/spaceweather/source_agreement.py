"""Source agreement foundation (FN-47): согласие НЕЗАВИСИМЫХ источников
внутри ОДНОГО механизма — провайдер-агностичная основа, которую сможет
подключить FN-41 (строгая production-классификация Механизма 1), но которая
сама FN-41 не завершает и её механизм пока не заменяет.

Контракт (постановка FN-47, дословно)::

    source_agreement = CONSISTENT | CONFLICT | INSUFFICIENT_DATA

``CONSISTENT``
    Есть минимум две ПРИМЕНИМЫЕ независимые оценки, и их decision-relevant
    классификации совпадают.

``CONFLICT``
    Минимум две применимые оценки дают разные decision-relevant
    классификации.

``INSUFFICIENT_DATA``
    Применимых независимых оценок меньше двух.

«Применимая» оценка — та, у которой есть decision-relevant классификация
(:attr:`SourceAssessment.classification` не ``None``). Источник, который сам
по себе «оценить невозможно» (например архивный продукт со своим
собственным ``AssessmentStatus.INSUFFICIENT_DATA`` —
см. ``archive_assessment.py::source_assessment_for_agreement``), не участвует
в подсчёте применимых оценок, но не исчезает молча: он остаётся в
:attr:`SourceAgreementAssessment.assessments` целиком, вместе с provenance —
main-prompt.md §2 «не выбирать молча «лучший» источник: сохранить оценки и
provenance каждого источника».

**Provider-agnostic.** Этот модуль не знает ни про DONKI, ни про SWPC, ни про
GOES, ни про какой-либо конкретный продукт или мехнизм — только про строки
классификаций и ``source_id``. Единственная точка, где конкретная семантика
превращается в этот общий вид, — адаптер конкретного продукта (например
``archive_assessment.py::source_assessment_for_agreement``); этот модуль сам
таких адаптеров не содержит и не обязан знать об их существовании.

**Независимость источников не проверяется здесь.** Она устанавливается вне
этого модуля — сегодня прозой в ``sources.yaml`` (поле ``independence_note``
у каждого источника: main-prompt.md §5 «два сайта, перепечатывающих одно
измерение, не являются независимым подтверждением»). Единственная защита
этого модуля от заведомо НЕ независимого входа — отказ при повторяющемся
``source_id`` (:class:`DuplicateSourceError`): две оценки одного и того же
источника не могут быть двумя независимыми подтверждениями.

**freshness/coverage — отдельно и не смешивается с agreement.** Этот модуль
не читает и не производит ``coverage_fraction``/``critical_gap``/давность:
они остаются на исходном, более богатом объекте оценки каждого источника
(``ArchiveWindowAssessment`` и т.п.) и не влияют на итоговый
``source_agreement`` — постановка FN-47 требует хранить их отдельно.

**Не смешивается с COMPROMISE между механизмами.** ``CONFLICT`` здесь — это
разногласие МЕЖДУ ИСТОЧНИКАМИ ОДНОГО механизма (например DONKI против SWPC
Forecast Discussion внутри Механизма 1 — Space Weather). Разное
«предпочтение» окна разными МЕХАНИЗМАМИ (Space Weather против MMOD) — уже
существующее и отдельное понятие ``src/domain/windows/dominance.py``
(``PairVerdict = "conflict"``, «конфликт механизмов», main-prompt.md §11
«Правило предпочтения окон»), которое этот модуль не читает, не производит и
не заменяет.

**Без weights и общего risk score.** Результат — только классификация
согласия и сырые оценки источников с provenance. Понижение confidence или
``CHECK_REQUIRED`` на основании ``CONFLICT`` — решение потребителя (FN-41),
не этого модуля: здесь такого поля нет и не появится в рамках FN-47.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

#: Ровно три значения контракта FN-47 (постановка, дословно) — не
#: расширяются в одностороннем порядке.
SourceAgreement = Literal["CONSISTENT", "CONFLICT", "INSUFFICIENT_DATA"]


class DuplicateSourceError(ValueError):
    """Две оценки с одним и тем же ``source_id`` переданы как будто от двух
    независимых источников.

    Это ошибка сборки входа у вызывающей стороны, а не законный случай:
    модуль не может молча решить, какую из двух версий одного источника
    предпочесть (main-prompt.md §2 «не выбирать молча «лучший» источник»)."""

    def __init__(self, source_id: str) -> None:
        super().__init__(
            f"source_id={source_id!r} appears more than once in the same "
            "source_agreement computation — two assessments from the same "
            "source are not two independent sources (FN-47 contract)"
        )
        self.source_id = source_id


@dataclass(frozen=True)
class SourceAssessment:
    """Один источник в его общем, decision-relevant виде — вход
    :func:`compute_source_agreement`.

    ``classification`` — ``None`` означает «этот источник сам не даёт
    применимой decision-relevant классификации прямо сейчас» (например
    источник вернул собственное «оценить невозможно» по своим правилам).
    Такая оценка не участвует в подсчёте применимых независимых оценок
    ниже, но полностью сохраняется в результате — ``record_ids``/``notes``
    остаются provenance этого источника даже когда он не смог дать
    классификацию.
    """

    source_id: str
    classification: str | None
    record_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceAgreementAssessment:
    """Результат расчёта ``source_agreement`` — вместе с оценками ВСЕХ
    переданных источников (не только применимых), чтобы FN-41 мог
    восстановить полную картину происхождения, а не только итоговую букву.
    """

    agreement: SourceAgreement
    assessments: tuple[SourceAssessment, ...]
    #: Различные decision-relevant классификации среди ПРИМЕНИМЫХ оценок.
    #: Один элемент — все применимые оценки согласны на этом значении;
    #: пусто — применимых оценок не было вовсе.
    applicable_classifications: frozenset[str]
    notes: tuple[str, ...]


def compute_source_agreement(
    assessments: Iterable[SourceAssessment],
) -> SourceAgreementAssessment:
    """Провайдер-агностичный расчёт ``source_agreement`` (контракт FN-47,
    дословно — см. докстринг модуля).

    Не выбирает «лучший» источник, не взвешивает и не производит общий risk
    score — оба явно вне scope FN-47 (решение FN-41, если оно вообще будет
    принято).
    """
    materialized = tuple(assessments)

    seen_source_ids: set[str] = set()
    for item in materialized:
        if item.source_id in seen_source_ids:
            raise DuplicateSourceError(item.source_id)
        seen_source_ids.add(item.source_id)

    applicable = tuple(a for a in materialized if a.classification is not None)
    classifications = frozenset(
        a.classification for a in applicable if a.classification is not None
    )

    agreement: SourceAgreement
    notes: list[str] = []
    if len(applicable) < 2:
        agreement = "INSUFFICIENT_DATA"
        notes.append(
            f"{len(applicable)} применимая(ых) независимая(ых) оценка(и) из "
            f"{len(materialized)} переданных источников — меньше двух, "
            "source_agreement не определён (FN-47 контракт: «применимых "
            "независимых оценок меньше двух»)."
        )
    elif len(classifications) == 1:
        agreement = "CONSISTENT"
        (only,) = classifications
        notes.append(
            f"{len(applicable)} независимых оценок согласованы на "
            f"decision-relevant классификации {only!r}."
        )
    else:
        agreement = "CONFLICT"
        by_source = ", ".join(f"{a.source_id}={a.classification!r}" for a in applicable)
        notes.append(
            "Независимые оценки дают разные decision-relevant классификации: "
            f"{by_source}. Это конфликт источников ОДНОГО механизма — не "
            "COMPROMISE между Space Weather и MMOD (см. модульный докстринг)."
        )

    return SourceAgreementAssessment(
        agreement=agreement,
        assessments=materialized,
        applicable_classifications=classifications,
        notes=tuple(notes),
    )


__all__ = [
    "DuplicateSourceError",
    "SourceAgreement",
    "SourceAgreementAssessment",
    "SourceAssessment",
    "compute_source_agreement",
]
