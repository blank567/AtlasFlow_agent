from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class QueryExpansion:
    """Bounded recall variants; the original query always occupies position zero."""

    queries: list[str]
    matched_terms: list[str]
    method: str = "bilingual-terms-v1"


class QueryExpander(Protocol):
    async def expand(self, query: str, *, limit: int) -> QueryExpansion: ...


@dataclass(frozen=True)
class _Term:
    id: str
    zh: tuple[str, ...]
    en: tuple[str, ...]


class BilingualQueryExpander:
    """Deterministic glossary matching, not task-intent classification or translation.

    The first alias in each language is its canonical expansion phrase. Custom
    entries replace built-in entries with the same id, or add new concepts. All
    configuration is read and compiled at construction, never during retrieval.
    """

    MAX_QUERY_LENGTH = 4500
    MAX_MATCHED_TERMS = 8
    MAX_TERMS = 1000
    MAX_ALIASES_PER_LANGUAGE = 32
    MAX_ALIAS_LENGTH = 120
    MAX_FILE_BYTES = 1024 * 1024

    def __init__(self, terms: Sequence[Mapping[str, object]] | None = None) -> None:
        builtin = resources.files(__package__).joinpath("bilingual_terms.json").read_text(
            encoding="utf-8"
        )
        merged = {term.id: term for term in self._parse_payload(json.loads(builtin))}
        if terms is not None:
            for term in self._parse_payload({"terms": terms}):
                merged[term.id] = term
        if len(merged) > self.MAX_TERMS:
            raise ValueError(f"bilingual glossary must contain at most {self.MAX_TERMS} terms")
        self._terms = tuple(merged.values())

        # One longest-first matcher prevents nested aliases (self-attention /
        # attention, supervised fine-tuning / fine-tuning) expanding twice.
        aliases: dict[str, tuple[str, int, bool]] = {}
        for index, term in enumerate(self._terms):
            for is_english, values in ((False, term.zh), (True, term.en)):
                for alias in values:
                    key = self._dedupe_key(alias)
                    previous = aliases.get(key)
                    if previous is not None and previous[1] != index:
                        raise ValueError(f"bilingual glossary alias is shared by multiple ids: {alias}")
                    aliases[key] = (alias, index, is_english)
        ordered = sorted(aliases.values(), key=lambda value: (-len(value[0]), value[0].casefold()))
        patterns = []
        self._groups: dict[str, int] = {}
        for alias_index, (alias, term_index, is_english) in enumerate(ordered):
            expression = r"\s+".join(re.escape(part) for part in alias.split())
            if is_english:
                expression = rf"(?<![A-Za-z0-9_]){expression}(?![A-Za-z0-9_])"
            group = f"alias_{alias_index}"
            self._groups[group] = term_index
            patterns.append(f"(?P<{group}>{expression})")
        self._matcher = re.compile("|".join(patterns), re.IGNORECASE)

    @classmethod
    def from_file(cls, path: str | Path) -> BilingualQueryExpander:
        """Load an explicit glossary overlay; invalid configuration fails startup."""
        glossary_path = Path(path)
        if glossary_path.stat().st_size > cls.MAX_FILE_BYTES:
            raise ValueError(f"bilingual glossary exceeds {cls.MAX_FILE_BYTES} bytes")
        try:
            payload = json.loads(glossary_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(f"bilingual glossary is not valid UTF-8 JSON: {glossary_path}") from exc
        # Parsing here validates the top-level object, not just its terms member.
        cls._parse_payload(payload)
        return cls(terms=payload["terms"])

    async def expand(self, query: str, *, limit: int = 2) -> QueryExpansion:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("knowledge query is empty after normalization")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
            raise ValueError("query expansion limit must be an integer between 1 and 4")
        # Never silently truncate the original. Oversized input remains searchable
        # as-is, but cannot grow through this optional expansion stage.
        if len(query) > self.MAX_QUERY_LENGTH:
            return QueryExpansion(queries=[query], matched_terms=[])

        matched: list[_Term] = []
        seen_terms: set[str] = set()
        searchable = self._without_urls(query)
        for match in self._matcher.finditer(searchable):
            term = self._terms[self._groups[match.lastgroup]]
            if term.id not in seen_terms:
                matched.append(term)
                seen_terms.add(term.id)
            if len(matched) == self.MAX_MATCHED_TERMS:
                break

        queries = [query]
        seen_queries = {self._dedupe_key(query)}
        for language in ("en", "zh"):
            suffixes: list[str] = []
            for term in matched:
                canonical = getattr(term, language)[0]
                if self._contains_alias(searchable, canonical, is_english=language == "en"):
                    continue
                candidate = f"{query} {'; '.join([*suffixes, canonical])}"
                if len(candidate) <= self.MAX_QUERY_LENGTH:
                    suffixes.append(canonical)
            if not suffixes:
                continue
            variant = f"{query} {'; '.join(suffixes)}"
            key = self._dedupe_key(variant)
            if key not in seen_queries:
                queries.append(variant)
                seen_queries.add(key)
            if len(queries) == limit + 1:
                break
        return QueryExpansion(queries=queries, matched_terms=[term.id for term in matched])

    @classmethod
    def _parse_payload(cls, payload: object) -> list[_Term]:
        if not isinstance(payload, dict) or set(payload) != {"terms"}:
            raise ValueError("bilingual glossary must be an object containing only 'terms'")
        records = payload["terms"]
        if not isinstance(records, (list, tuple)) or len(records) > cls.MAX_TERMS:
            raise ValueError(f"bilingual glossary 'terms' must be a list of at most {cls.MAX_TERMS}")
        result: list[_Term] = []
        ids: set[str] = set()
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"id", "zh", "en"}:
                raise ValueError("bilingual glossary entries require exactly id, zh and en")
            term_id = record["id"]
            if not isinstance(term_id, str) or re.fullmatch(r"[a-z][a-z0-9_\-]{0,63}", term_id) is None:
                raise ValueError("bilingual glossary id must be a lowercase identifier")
            if term_id in ids:
                raise ValueError(f"bilingual glossary contains duplicate id: {term_id}")
            ids.add(term_id)
            languages: dict[str, tuple[str, ...]] = {}
            for language in ("zh", "en"):
                values = record[language]
                if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= cls.MAX_ALIASES_PER_LANGUAGE:
                    raise ValueError(f"bilingual glossary {term_id}.{language} must be a nonempty alias list")
                aliases: dict[str, str] = {}
                for value in values:
                    if not isinstance(value, str) or not value.strip() or len(value) > cls.MAX_ALIAS_LENGTH:
                        raise ValueError(f"bilingual glossary {term_id}.{language} contains an invalid alias")
                    normalized = re.sub(r"\s+", " ", value).strip()
                    aliases.setdefault(cls._dedupe_key(normalized), normalized)
                languages[language] = tuple(aliases.values())
            result.append(_Term(id=term_id, zh=languages["zh"], en=languages["en"]))
        return result

    @staticmethod
    def _dedupe_key(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    @staticmethod
    def _without_urls(value: str) -> str:
        # A term in a resource path/query is not a semantic mention. Mask URLs
        # only in matching text, preserving the original query and all positions.
        return re.sub(
            r"(?:https?://|www\.)[^\s<>\"']+",
            lambda match: " " * len(match.group()),
            value,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _contains_alias(query: str, alias: str, *, is_english: bool) -> bool:
        expression = r"\s+".join(re.escape(part) for part in alias.split())
        if is_english:
            expression = rf"(?<![A-Za-z0-9_]){expression}(?![A-Za-z0-9_])"
        return re.search(expression, query, flags=re.IGNORECASE) is not None
