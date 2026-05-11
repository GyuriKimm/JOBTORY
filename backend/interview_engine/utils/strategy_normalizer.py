from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List
import re

from django.core.cache import cache

from .checkpoint_reader import load_chapter_channel_values


@dataclass
class StrategyNormalizationResult:
    raw_text: str
    normalized_text: str
    algorithm_tags: List[str] = field(default_factory=list)
    confidence: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "algorithm_tags": list(self.algorithm_tags or []),
            "confidence": round(float(self.confidence or 0.0), 4),
            "notes": list(self.notes or []),
        }


_STACK_ALIASES = [
    "스테이크",
    "스택",
    "stack",
    "lifo",
]
_STACK_CONTEXT = [
    "push",
    "pop",
    "top",
    "peek",
    "후입선출",
    "괄호",
    "자료구조",
    "단조스택",
    "단조 스택",
]
_QUEUE_ALIASES = ["큐", "queue", "deque", "enqueue", "dequeue"]
_QUEUE_CONTEXT = ["front", "rear", "bfs", "너비", "대기열", "FIFO"]
_DP_ALIASES = ["dp", "다이나믹 프로그래밍", "동적 계획법"]
_DP_CONTEXT = ["점화식", "상태", "전이", "메모이제이션", "memo"]
_Dijkstra_ALIASES = ["다익스트라", "다이크스트라", "dijkstra"]
_Dijkstra_CONTEXT = ["최단", "가중치", "그래프", "우선순위", "heap", "priority"]
_TWOPTR_ALIASES = ["투포인터", "투 포인터", "two pointer", "two-pointer", "2포인터", "2 포인터"]
_TWOPTR_CONTEXT = ["left", "right", "window", "윈도우", "슬라이딩"]
_HASH_ALIASES = ["해시테이블", "해시 테이블", "hash table", "hashmap", "hash map"]
_HASH_CONTEXT = ["dict", "dictionary", "set", "맵", "매핑", "hash"]
_HEAP_ALIASES = ["heap", "힙", "priority queue", "우선순위 큐"]
_HEAP_CONTEXT = ["heappush", "heappop", "pq", "우선순위"]
_BFS_ALIASES = ["bfs", "너비 우선 탐색", "너비우선탐색"]
_DFS_ALIASES = ["dfs", "깊이 우선 탐색", "깊이우선탐색"]
_GREEDY_ALIASES = ["greedy", "그리디", "탐욕"]
_SORT_ALIASES = ["정렬", "sort", "sorted", "sorting"]
_STRING_ALIASES = ["문자열", "string", "regex", "정규식", "kmp"]
_TREE_ALIASES = ["tree", "트리", "이진트리", "이진 트리", "bst"]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lower = (text or "").lower()
    return any((term or "").lower() in lower for term in terms if term)


def _replace_terms(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    out = text
    for src, dst in replacements:
        if not src:
            continue
        out = re.sub(re.escape(src), dst, out, flags=re.IGNORECASE)
    return out


def _extract_problem_algorithms(problem_algorithms: Any) -> List[str]:
    if isinstance(problem_algorithms, list):
        return [str(a).strip().lower() for a in problem_algorithms if str(a).strip()]
    if isinstance(problem_algorithms, str) and problem_algorithms.strip():
        text = problem_algorithms.strip()
        try:
            import json as _json

            parsed = _json.loads(text)
            if isinstance(parsed, list):
                return [str(a).strip().lower() for a in parsed if str(a).strip()]
        except Exception:
            pass
        return [text.lower()]
    return []


def _detect_tags(
    raw_text: str,
    problem_text: str = "",
    problem_algorithms: Any = None,
) -> tuple[List[str], float, List[str]]:
    raw = _normalize_space(raw_text)
    combined = f"{raw} {_normalize_space(problem_text)}"
    lower = combined.lower()
    problems = set(_extract_problem_algorithms(problem_algorithms))

    tags: List[str] = []
    confidence = 0.0
    notes: List[str] = []

    def add(tag: str, alias_hit: bool, context_hit: bool, problem_hit: bool, note: str) -> None:
        nonlocal confidence
        if not alias_hit and not (problem_hit and context_hit):
            return
        if tag not in tags:
            tags.append(tag)
        if alias_hit:
            confidence += 0.4
        if problem_hit:
            confidence += 0.35
        if context_hit:
            confidence += 0.2
        if note:
            notes.append(note)

    stack_alias = _contains_any(raw, ["스택", "stack"])
    stack_false_friend = _contains_any(raw, ["스테이크"])
    stack_context = _contains_any(lower, _STACK_CONTEXT)
    stack_problem = "stack" in problems
    stack_signal = stack_alias or (stack_false_friend and (stack_context or stack_problem))
    add("stack", stack_signal, stack_context, stack_problem, "stack")

    queue_alias = _contains_any(raw, _QUEUE_ALIASES)
    queue_context = _contains_any(lower, _QUEUE_CONTEXT)
    queue_problem = "queue" in problems or "deque" in problems
    add("queue", queue_alias, queue_context, queue_problem, "queue")

    dp_alias = _contains_any(raw, _DP_ALIASES)
    dp_context = _contains_any(lower, _DP_CONTEXT)
    dp_problem = "dp" in problems
    add("dp", dp_alias, dp_context, dp_problem, "dp")

    dij_alias = _contains_any(raw, _Dijkstra_ALIASES)
    dij_context = _contains_any(lower, _Dijkstra_CONTEXT)
    dij_problem = "graph" in problems or "shortest path" in problems or "dijkstra" in problems
    add("graph", dij_alias, dij_context, dij_problem, "dijkstra")

    twoptr_alias = _contains_any(raw, _TWOPTR_ALIASES)
    twoptr_context = _contains_any(lower, _TWOPTR_CONTEXT)
    twoptr_problem = "two_pointer" in problems
    add("two_pointer", twoptr_alias, twoptr_context, twoptr_problem, "two_pointer")

    hash_alias = _contains_any(raw, _HASH_ALIASES)
    hash_context = _contains_any(lower, _HASH_CONTEXT)
    hash_problem = "hash_table" in problems
    add("hash_table", hash_alias, hash_context, hash_problem, "hash_table")

    heap_alias = _contains_any(raw, _HEAP_ALIASES)
    heap_context = _contains_any(lower, _HEAP_CONTEXT)
    heap_problem = "heap" in problems
    add("heap", heap_alias, heap_context, heap_problem, "heap")

    bfs_alias = _contains_any(raw, _BFS_ALIASES)
    bfs_context = _contains_any(lower, ["너비", "level", "queue", "graph"])
    bfs_problem = "graph" in problems
    add("graph", bfs_alias, bfs_context, bfs_problem, "bfs")

    dfs_alias = _contains_any(raw, _DFS_ALIASES)
    dfs_context = _contains_any(lower, ["깊이", "재귀", "recursion", "backtracking"])
    dfs_problem = "graph" in problems or "tree" in problems
    add("graph", dfs_alias, dfs_context, dfs_problem, "dfs")

    greedy_alias = _contains_any(raw, _GREEDY_ALIASES)
    greedy_context = _contains_any(lower, ["최적", "탐욕", "선택"])
    greedy_problem = "greedy" in problems
    add("greedy", greedy_alias, greedy_context, greedy_problem, "greedy")

    sort_alias = _contains_any(raw, _SORT_ALIASES)
    sort_context = _contains_any(lower, ["오름차순", "내림차순", "정렬"])
    sort_problem = "sort" in problems
    add("sort", sort_alias, sort_context, sort_problem, "sort")

    string_alias = _contains_any(raw, _STRING_ALIASES)
    string_context = _contains_any(lower, ["슬라이싱", "substring", "패턴", "문자열"])
    string_problem = "string" in problems
    add("string", string_alias, string_context, string_problem, "string")

    tree_alias = _contains_any(raw, _TREE_ALIASES)
    tree_context = _contains_any(lower, ["노드", "루트", "left", "right", "재귀"])
    tree_problem = "tree" in problems
    add("tree", tree_alias, tree_context, tree_problem, "tree")

    confidence = max(0.0, min(0.99, confidence))
    notes = list(dict.fromkeys(notes))
    return tags, confidence, notes


def normalize_strategy_answer(
    raw_text: str,
    problem_text: str = "",
    problem_algorithms: Any = None,
) -> StrategyNormalizationResult:
    raw = _normalize_space(raw_text)
    if not raw:
        return StrategyNormalizationResult(raw_text="", normalized_text="", algorithm_tags=[], confidence=0.0, notes=["empty"])

    tags, confidence, notes = _detect_tags(raw, problem_text=problem_text, problem_algorithms=problem_algorithms)
    normalized = raw

    if "stack" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("스테이크", "스택"), ("stack", "스택"), ("lifo", "스택")],
        )
    if "queue" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("deque", "큐"), ("queue", "큐"), ("enqueue", "큐"), ("dequeue", "큐")],
        )
    if "dp" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("다이나믹 프로그래밍", "동적 계획법"), ("dynamic programming", "동적 계획법"), ("dp", "DP")],
        )
    if "graph" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("다이크스트라", "다익스트라"), ("dijkstra", "다익스트라")],
        )
    if "two_pointer" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("투포인터", "투 포인터"), ("two-pointer", "two pointer"), ("2포인터", "2 포인터")],
        )
    if "hash_table" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("해시테이블", "해시 테이블"), ("hashmap", "hash map")],
        )
    if "heap" in tags and confidence >= 0.55:
        normalized = _replace_terms(
            normalized,
            [("우선순위큐", "우선순위 큐"), ("priorityqueue", "priority queue")],
        )

    normalized = _normalize_space(normalized)
    return StrategyNormalizationResult(
        raw_text=raw,
        normalized_text=normalized,
        algorithm_tags=tags,
        confidence=confidence,
        notes=notes,
    )


def resolve_strategy_answer_bundle(
    state: Dict[str, Any] | None = None,
    session_id: str = "",
) -> StrategyNormalizationResult:
    state = state or {}

    def _first(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    meta = state.get("meta") or {}
    cached_meta = {}
    chap1 = {}
    chap2 = {}
    if session_id:
        cached_meta = cache.get(f"livecoding:{session_id}:meta") or {}
        chap1 = load_chapter_channel_values(session_id, "chapter1")
        chap2 = load_chapter_channel_values(session_id, "chapter2")

    problem_text = _first(
        state.get("problem_text"),
        state.get("problem_description"),
        meta.get("problem_text"),
        meta.get("problem_description"),
        cached_meta.get("problem_text"),
        cached_meta.get("problem_description"),
        chap1.get("problem_data"),
    )

    problem_algorithms = (
        state.get("problem_algorithms")
        or meta.get("problem_algorithms")
        or cached_meta.get("problem_algorithms")
        or (state.get("problem_evidence") or {}).get("strategy_algorithms")
    )

    raw_candidates = [
        state.get("initial_strategy_raw"),
        state.get("strategy_answer_raw"),
        state.get("user_strategy_answer_raw"),
        state.get("initial_strategy"),
        state.get("strategy_answer"),
        state.get("user_strategy_answer"),
        chap1.get("user_strategy_answer_raw"),
        chap1.get("user_strategy_answer"),
        cached_meta.get("strategy_answer_raw"),
        cached_meta.get("strategy_answer"),
    ]
    normalized_candidates = [
        state.get("initial_strategy_normalized"),
        state.get("strategy_answer_normalized"),
        state.get("user_strategy_answer_normalized"),
        chap1.get("user_strategy_answer_normalized"),
        cached_meta.get("strategy_answer_normalized"),
    ]

    normalized = _first(*normalized_candidates)
    raw = _first(*raw_candidates)

    if normalized:
        if not raw:
            raw = normalized
        tags = []
        confidence = float(state.get("strategy_confidence") or cached_meta.get("strategy_confidence") or 0.0)
        notes: List[str] = []
    else:
        if session_id and not raw:
            answers = chap2.get("user_answers") or []
            if isinstance(answers, list) and answers:
                for answer in answers:
                    if isinstance(answer, str) and answer.strip():
                        raw = answer.strip()
                        break
            if not raw:
                code_data = cache.get(f"livecoding:{session_id}:code") or {}
                question_history = code_data.get("question_history") or []
                if isinstance(question_history, list):
                    for item in question_history:
                        if isinstance(item, dict):
                            answer = item.get("answer") or item.get("stt_text")
                            if isinstance(answer, str) and answer.strip():
                                raw = answer.strip()
                                break
        result = normalize_strategy_answer(raw, problem_text=problem_text, problem_algorithms=problem_algorithms)
        raw = result.raw_text
        normalized = result.normalized_text
        tags = result.algorithm_tags
        confidence = result.confidence
        notes = result.notes

    if not tags:
        tags = _extract_strategy_algorithms(normalized or raw)

    return StrategyNormalizationResult(
        raw_text=raw or normalized or "",
        normalized_text=normalized or raw or "",
        algorithm_tags=tags,
        confidence=confidence,
        notes=notes,
    )


def _extract_strategy_algorithms(strategy_text: str) -> List[str]:
    if not strategy_text:
        return []
    lower = strategy_text.lower()
    tags: List[str] = []
    for tag, aliases in {
        "stack": _STACK_ALIASES,
        "queue": _QUEUE_ALIASES,
        "dp": _DP_ALIASES,
        "graph": _Dijkstra_ALIASES + _BFS_ALIASES + _DFS_ALIASES,
        "two_pointer": _TWOPTR_ALIASES,
        "hash_table": _HASH_ALIASES,
        "heap": _HEAP_ALIASES,
        "greedy": _GREEDY_ALIASES,
        "sort": _SORT_ALIASES,
        "string": _STRING_ALIASES,
        "tree": _TREE_ALIASES,
    }.items():
        if any(alias.lower() in lower for alias in aliases):
            tags.append(tag)
    return sorted(dict.fromkeys(tags))
