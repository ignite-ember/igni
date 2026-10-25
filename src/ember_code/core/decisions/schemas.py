"""Typed decisions, as igni asks them of Laya and answers them.

A decision is a few named questions about a context — named text fields
the questions refer to as `` `field` `` — each one of Laya's three
kinds:

* ``choice``: one of up to :attr:`DecisionLimits.OPTIONS` labelled options;
* ``score``: one level of an ordered scale, lowest first;
* ``noul``: yes or no (Laya's name for it).

Every answer carries the probabilities it came from, so the asker
decides how sure is sure enough. The limits are the host's, not
Laya's: they keep one extension from holding the model for long, and
they keep a question inside what the model can read at all.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: A context field's name, as a question quotes it: `` `diff` ``.
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class DecisionLimits:
    """How much one call may ask."""

    #: Named fields in one context.
    CONTEXT_FIELDS = 8
    #: Characters across one context's fields. Laya reads 512 tokens
    #: (English) or 1,024 (the others) including the questions; this
    #: refuses the obviously too long before the model is asked, and
    #: :class:`DecisionTooLong` refuses what the model would truncate.
    CONTEXT_CHARS = 6000
    #: Questions in one call.
    QUESTIONS = 16
    #: Characters in one question.
    QUESTION_CHARS = 500
    #: Options of a ``choice`` question.
    OPTIONS = 20
    #: Levels of a ``score`` question.
    LEVELS = 10
    #: Characters in an option's or a level's label.
    LABEL_CHARS = 64
    #: Characters in an option's description.
    DESCRIPTION_CHARS = 200
    #: Contexts in one batch.
    BATCH = 16


class QuestionKind(StrEnum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionQuestion(Strict):
    """One question, as an extension writes it."""

    id: str = Field(pattern=FIELD_NAME.pattern, max_length=64)
    kind: QuestionKind
    question: str = Field(min_length=1, max_length=DecisionLimits.QUESTION_CHARS)
    #: ``choice`` only: label → what it means ("" when the label says it).
    options: dict[str, str] | None = None
    #: ``score`` only: the levels, lowest first.
    levels: list[str] | None = None

    @model_validator(mode="after")
    def _fits_its_kind(self) -> DecisionQuestion:
        if self.kind is QuestionKind.CHOICE:
            if self.levels is not None:
                raise ValueError(f"{self.id}: a choice question has options, not levels")
            options = self.options or {}
            if not 2 <= len(options) <= DecisionLimits.OPTIONS:
                raise ValueError(
                    f"{self.id}: a choice question has 2 to {DecisionLimits.OPTIONS} options"
                )
            for label, description in options.items():
                self._label(label)
                if len(description) > DecisionLimits.DESCRIPTION_CHARS:
                    raise ValueError(
                        f"{self.id}: option {label!r}'s description is over "
                        f"{DecisionLimits.DESCRIPTION_CHARS} characters"
                    )
        elif self.kind is QuestionKind.SCORE:
            if self.options is not None:
                raise ValueError(f"{self.id}: a score question has levels, not options")
            levels = self.levels or []
            if not 2 <= len(levels) <= DecisionLimits.LEVELS:
                raise ValueError(
                    f"{self.id}: a score question has 2 to {DecisionLimits.LEVELS} levels"
                )
            if len(set(levels)) != len(levels):
                raise ValueError(f"{self.id}: every level is named differently")
            for label in levels:
                self._label(label)
        elif self.options is not None or self.levels is not None:
            raise ValueError(f"{self.id}: a noul (yes/no) question has no options or levels")
        return self

    def _label(self, label: str) -> None:
        if not label.strip() or len(label) > DecisionLimits.LABEL_CHARS:
            raise ValueError(
                f"{self.id}: a label is 1 to {DecisionLimits.LABEL_CHARS} characters, not {label!r}"
            )

    def laya(self) -> dict[str, Any]:
        """The question in Laya's own shape."""
        out: dict[str, Any] = {"type": self.kind.value, "instructions": self.question}
        if self.kind is QuestionKind.CHOICE:
            out["criteria"] = {
                label: (text or None) for label, text in (self.options or {}).items()
            }
        elif self.kind is QuestionKind.SCORE:
            out["criteria"] = list(self.levels or [])
        return out


class DecisionRequest(Strict):
    """Contexts and the questions asked of each."""

    contexts: list[dict[str, str]] = Field(min_length=1, max_length=DecisionLimits.BATCH)
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=DecisionLimits.QUESTIONS)

    @field_validator("contexts")
    @classmethod
    def _contexts(cls, contexts: list[dict[str, str]]) -> list[dict[str, str]]:
        for context in contexts:
            if not 1 <= len(context) <= DecisionLimits.CONTEXT_FIELDS:
                raise ValueError(f"a context has 1 to {DecisionLimits.CONTEXT_FIELDS} named fields")
            for name in context:
                if not FIELD_NAME.match(name):
                    raise ValueError(f"a context field is a lowercase name, not {name!r}")
            if sum(len(text) for text in context.values()) > DecisionLimits.CONTEXT_CHARS:
                raise ValueError(
                    f"a context is at most {DecisionLimits.CONTEXT_CHARS} characters; "
                    "ask about less of it"
                )
        return contexts

    @field_validator("questions")
    @classmethod
    def _distinct(cls, questions: list[DecisionQuestion]) -> list[DecisionQuestion]:
        ids = [q.id for q in questions]
        if len(set(ids)) != len(ids):
            raise ValueError("every question has its own id")
        return questions

    def laya_questions(self) -> dict[str, Any]:
        return {q.id: q.laya() for q in self.questions}


class DecisionAnswer(BaseModel):
    """One question's answer."""

    kind: QuestionKind
    #: The option chosen, the level reached, or ``yes`` / ``no``.
    answer: str
    #: The probability of that answer.
    confidence: float
    #: Every option's, level's, or ``yes`` and ``no``'s probability.
    probabilities: dict[str, float]
    #: ``score`` only: the expected level, 0 for the lowest.
    score: float | None = None


class Decision(BaseModel):
    """Every question's answer about one context."""

    answers: dict[str, DecisionAnswer]
    #: The Laya checkpoint that answered.
    model: str


class DecisionsUnavailable(Exception):
    """Laya could not be loaded or could not run; nothing was decided."""


class DecisionTooLong(Exception):
    """The context and questions do not fit what Laya reads."""


class LayaReading:
    """One Laya result read back into a :class:`Decision`."""

    def __init__(self, questions: list[DecisionQuestion]) -> None:
        self._questions = {q.id: q for q in questions}

    def decision(self, result: dict[str, Any]) -> Decision:
        usage = result.get("usage") or {}
        if usage.get("truncated") or usage.get("truncated_questions"):
            dropped = int(usage.get("state_tokens_dropped") or 0)
            raise DecisionTooLong(
                f"the context and questions are longer than Laya reads ({dropped} tokens of "
                "the context would be dropped); ask about less, or fewer questions at once"
            )
        answers = result.get("answers") or {}
        return Decision(
            answers={qid: self._answer(q, answers[qid]) for qid, q in self._questions.items()},
            model=str((result.get("routing") or {}).get("model") or ""),
        )

    @staticmethod
    def _answer(question: DecisionQuestion, raw: dict[str, Any]) -> DecisionAnswer:
        if question.kind is QuestionKind.NOUL:
            yes = float(raw["noul"])
            return DecisionAnswer(
                kind=question.kind,
                answer="yes" if yes >= 0.5 else "no",
                confidence=max(yes, 1.0 - yes),
                probabilities={"yes": yes, "no": 1.0 - yes},
            )
        if question.kind is QuestionKind.SCORE:
            levels = question.levels or []
            by_index = raw.get("probabilities") or {}
            probabilities = {
                level: float(by_index.get(str(i), 0.0)) for i, level in enumerate(levels)
            }
            top = max(probabilities, key=probabilities.__getitem__)
            return DecisionAnswer(
                kind=question.kind,
                answer=top,
                confidence=probabilities[top],
                probabilities=probabilities,
                score=float(raw.get("score", 0.0)),
            )
        probabilities = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
        chosen = str(raw.get("choice"))
        return DecisionAnswer(
            kind=question.kind,
            answer=chosen,
            confidence=probabilities.get(chosen, 0.0),
            probabilities=probabilities,
        )


__all__ = [
    "Decision",
    "DecisionAnswer",
    "DecisionLimits",
    "DecisionQuestion",
    "DecisionRequest",
    "DecisionTooLong",
    "DecisionsUnavailable",
    "LayaReading",
    "QuestionKind",
]
