"""Source-preserving protocol data, validated proposals and deterministic rendering.

Offsets are Python/Unicode code points in the *original* utterance. Nothing a
model proposes is applied until the owner accepts it. ASR speaker labels remain
identifiers, even when a reviewer assigns a role or overrides one utterance.
"""
import copy
import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 1
TEMPLATE_VERSION = "review-1"
ROLES = (
    "Председательствующий", "Секретарь", "Государственный обвинитель",
    "Защитник", "Подсудимый", "Потерпевший", "Свидетель", "Эксперт",
    "Специалист", "Переводчик", "Представитель", "Роль не установлена",
)
Role = Literal[
    "Председательствующий", "Секретарь", "Государственный обвинитель",
    "Защитник", "Подсудимый", "Потерпевший", "Свидетель", "Эксперт",
    "Специалист", "Переводчик", "Представитель", "Роль не установлена",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Correction(StrictModel):
    utterance_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    original: str = Field(min_length=1, max_length=500)
    replacement: str = Field(min_length=1, max_length=500)
    category: Literal["punctuation", "spelling", "recognition", "meaning"]
    reason: str = Field(min_length=1, max_length=500)


class Evidence(StrictModel):
    utterance_id: str
    quote: str = Field(min_length=1, max_length=500)


class RoleProposal(StrictModel):
    speaker_id: str
    role: Role
    name: str = Field(max_length=150)
    evidence: list[Evidence] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=1, max_length=500)


class Concern(Evidence):
    reason: str = Field(min_length=1, max_length=500)


class ProposalBatch(StrictModel):
    reviewed_segments: list[str] = Field(max_length=500)
    corrections: list[Correction] = Field(max_length=100)
    roles: list[RoleProposal] = Field(max_length=100)
    concerns: list[Concern] = Field(max_length=100)


class Assignment(StrictModel):
    role: Role
    name: str = Field(default="", max_length=150)


class DocumentFields(StrictModel):
    court: str = Field(default="", max_length=250)
    city: str = Field(default="", max_length=100)
    hearing_date: str = Field(default="", max_length=100)
    case_number: str = Field(default="", max_length=100)
    judge: str = Field(default="", max_length=150)
    secretary: str = Field(default="", max_length=150)


class ReviewUpdate(StrictModel):
    revision: int = Field(ge=0)
    decisions: dict[str, Literal["accepted", "rejected"]] = Field(default_factory=dict)
    speakers: dict[str, Assignment] = Field(default_factory=dict)
    utterances: dict[str, Assignment] = Field(default_factory=dict)
    fields: DocumentFields = Field(default_factory=DocumentFields)
    manual_text: dict[str, str] = Field(default_factory=dict)
    resolved_concerns: list[str] = Field(default_factory=list)
    reviewed: bool = False


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def normalize_source(transcript: dict) -> dict:
    raw = transcript.get("utterances") or [{"speaker": "?", "text": transcript.get("text", "")}]
    utterances = []
    for index, item in enumerate(raw):
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError("Распознаватель вернул реплику без текста")
        entry = {"id": f"u{index + 1:06d}", "speaker_id": str(item.get("speaker") if item.get("speaker") is not None else "?"),
                 "text": text, "start_ms": None, "end_ms": None, "confidence": None}
        for src, dst in (("start", "start_ms"), ("end", "end_ms"), ("confidence", "confidence")):
            value = item.get(src)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                entry[dst] = value
        utterances.append(entry)
    if not any(u["text"].strip() for u in utterances):
        raise ValueError("Сервис распознавания вернул пустую стенограмму")
    return {"schema_version": SCHEMA_VERSION, "provider": "assemblyai",
            "provider_id": transcript.get("id"), "audio_duration": transcript.get("audio_duration"),
            "utterances": utterances, "sha256": digest(utterances)}


def batches_for_source(source: dict, segment_chars=4000, batch_chars=12000) -> list[list[dict]]:
    """Bound requests while retaining absolute positions in oversized utterances."""
    segments = []
    for u in source["utterances"]:
        start = 0
        while start < len(u["text"]):
            end = min(start + segment_chars, len(u["text"]))
            if end < len(u["text"]):
                boundaries = list(re.finditer(r"[.!?…]\s+|\n", u["text"][start:end]))
                if boundaries and boundaries[-1].end() > segment_chars // 2:
                    end = start + boundaries[-1].end()
            segments.append({"id": f"{u['id']}:{start}:{end}", "utterance_id": u["id"],
                             "speaker_id": u["speaker_id"], "start": start, "end": end,
                             "text": u["text"][start:end]})
            start = end
        if not u["text"]:
            segments.append({"id": f"{u['id']}:0:0", "utterance_id": u["id"],
                             "speaker_id": u["speaker_id"], "start": 0, "end": 0, "text": ""})
    batches, current, size = [], [], 0
    for segment in segments:
        weight = len(json.dumps(segment, ensure_ascii=False))
        if current and (size + weight > batch_chars or len(current) >= 100):
            batches.append(current)
            current, size = [], 0
        current.append(segment)
        size += weight
    if current:
        batches.append(current)
    return batches


def validate_proposals(payload: dict, source: dict, segments: list[dict]) -> dict:
    batch = ProposalBatch.model_validate(payload)
    if batch.reviewed_segments != [s["id"] for s in segments]:
        raise ValueError("Ответ не подтверждает обработку всех фрагментов")
    utterances = {u["id"]: u for u in source["utterances"]}
    speakers = {u["speaker_id"] for u in source["utterances"]}
    occupied = {}
    for correction in batch.corrections:
        u = utterances.get(correction.utterance_id)
        if (not u or correction.end <= correction.start
                or u["text"][correction.start:correction.end] != correction.original
                or correction.end > len(u["text"])):
            raise ValueError("Правка не совпадает с исходной репликой")
        if not any(s["utterance_id"] == u["id"] and s["start"] <= correction.start < correction.end <= s["end"] for s in segments):
            raise ValueError("Правка находится вне обрабатываемого фрагмента")
        if correction.original == correction.replacement or not correction.replacement.strip():
            raise ValueError("Пустая правка или удаление текста запрещены")
        if "\n" in correction.replacement or "\r" in correction.replacement:
            raise ValueError("Правка не может добавлять структуру документа")
        ranges = occupied.setdefault(u["id"], [])
        if any(correction.start < end and start < correction.end for start, end in ranges):
            raise ValueError("Правки пересекаются или дублируются")
        ranges.append((correction.start, correction.end))
    evidence = list(batch.concerns)
    for role in batch.roles:
        if role.speaker_id not in speakers:
            raise ValueError("Неизвестный голос в предложении роли")
        evidence.extend(role.evidence)
    for item in evidence:
        if not item.quote.strip() or item.utterance_id not in utterances or item.quote not in utterances[item.utterance_id]["text"]:
            raise ValueError("Основание отсутствует в исходной расшифровке")
    return batch.model_dump()


def sensitive_change(correction: dict) -> bool:
    """A visible review hint, never evidence that an unflagged edit is safe."""
    before, after = correction["original"], correction["replacement"]
    return bool(correction["category"] == "meaning"
                or re.findall(r"\d+", before) != re.findall(r"\d+", after)
                or re.findall(r"\b(?:не|нет|ни|без)\b", before.lower()) != re.findall(r"\b(?:не|нет|ни|без)\b", after.lower())
                or re.search(r"[А-ЯЁ][а-яё]+", before + after)
                or len(after.strip()) < len(before.strip()) / 2)


def proposals(state: dict, key: str) -> list[dict]:
    unique = {}
    for batch in state["batches"].values():
        for value in batch["result"][key]:
            proposal_id = digest(value)[:24]
            unique[proposal_id] = {**value, "id": proposal_id}
    return list(unique.values())


def role_registry(source: dict, state: dict) -> list[dict]:
    registry = {u["speaker_id"]: [] for u in source["utterances"]}
    for role in proposals(state, "roles"):
        registry[role["speaker_id"]].append(role)
    return [{"speaker_id": speaker, "candidates": candidates,
             "status": "conflict" if len({(c["role"], c["name"]) for c in candidates}) > 1
             else "suggested" if candidates else "unknown"}
            for speaker, candidates in registry.items()]


def corrected_utterances(source: dict, state: dict) -> list[dict]:
    corrections = proposals(state, "corrections")
    result = []
    for u in source["utterances"]:
        text = u["text"]
        applied = sorted((c for c in corrections if c["utterance_id"] == u["id"]
                          and state["decisions"].get(c["id"]) == "accepted"),
                         key=lambda c: c["start"], reverse=True)
        for c in applied:
            text = text[:c["start"]] + c["replacement"] + text[c["end"]:]
        text = state.get("manual_text", {}).get(u["id"], text)
        assignment = state["utterances"].get(u["id"], state["speakers"].get(u["speaker_id"]))
        label = f"Спикер {u['speaker_id']}"
        if assignment and assignment["role"] != "Роль не установлена":
            label = assignment["role"] + (f" ({assignment['name']})" if assignment["name"] else "")
        result.append({**u, "original": u["text"], "text": text, "label": label})
    return result


def document_blocks(source: dict, state: dict) -> list[dict]:
    blocks = []
    if not state.get("reviewed"):
        blocks.append({"kind": "notice", "text": "ЧЕРНОВИК. Требуется проверка по аудиозаписи."})
    blocks.extend([{"kind": "title", "text": "ПРОТОКОЛ"},
                   {"kind": "title", "text": "судебного заседания"}])
    fields = state["fields"]
    for key, label in (("court", "Суд"), ("city", "Место"), ("hearing_date", "Дата"),
                       ("case_number", "Дело №"), ("judge", "Председательствующий"), ("secretary", "Секретарь")):
        if fields.get(key):
            blocks.append({"kind": "field", "text": f"{label}: {fields[key]}"})
    for u in corrected_utterances(source, state):
        blocks.append({"kind": "utterance", "utterance_id": u["id"],
                       "label": u["label"], "text": u["text"]})
    # Missing facts produce no invented opening, closing, date or CD-R claim.
    for key, label in (("judge", "Председательствующий"), ("secretary", "Секретарь")):
        if fields.get(key):
            blocks.append({"kind": "signature", "text": f"{label}\t{fields[key]}"})
    return blocks


def document_text(blocks: list[dict]) -> str:
    return "\n\n".join(f"{b['label']}: {b['text']}" if b["kind"] == "utterance" else b["text"] for b in blocks)


def apply_review(source: dict, state: dict, update: ReviewUpdate) -> dict:
    if state["stage"] not in ("ready", "error"):
        raise ValueError("Дождитесь завершения анализа")
    changes = {c["id"]: c for c in proposals(state, "corrections")}
    concerns = {c["id"] for c in proposals(state, "concerns")}
    speakers = {u["speaker_id"] for u in source["utterances"]}
    utterances = {u["id"] for u in source["utterances"]}
    if not set(update.decisions) <= changes.keys() or not set(update.resolved_concerns) <= concerns:
        raise ValueError("Неизвестная правка или замечание")
    if not set(update.speakers) <= speakers or not set(update.utterances) <= utterances or not set(update.manual_text) <= utterances:
        raise ValueError("Неизвестная реплика или голос")
    if any(not value.strip() or len(value) > 50000 for value in update.manual_text.values()):
        raise ValueError("Ручная правка не должна удалять реплику или превышать 50000 символов")
    if any(any(c in value for c in "\r\n\t") for value in update.fields.model_dump().values()):
        raise ValueError("Поля документа должны занимать одну строку")
    for assignment in [*update.speakers.values(), *update.utterances.values()]:
        if any(c in assignment.name for c in "\r\n\t"):
            raise ValueError("Имя участника должно занимать одну строку")
    if update.reviewed and (state["stage"] != "ready" or changes.keys() != update.decisions.keys()
                            or set(update.resolved_concerns) != concerns or set(update.speakers) != speakers):
        raise ValueError("Проверьте все правки, замечания и роли перед завершением")
    new = copy.deepcopy(state)
    new.update(update.model_dump(exclude={"revision"}))
    return new
