from __future__ import annotations

import ast
import re
from typing import Any, Dict, List

_PREFIX_PATTERN = re.compile(r"^\s*\[([A-Z]+)")


def _safe_str(x: Any) -> str:
    return "" if x is None else str(x)


def _coerce_list(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        out: List[str] = []
        for it in raw:
            if it is None:
                continue
            s = str(it).strip()
            if s:
                out.append(s)
        return out
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return []


def _extract_prefix(line: str) -> str:
    m = _PREFIX_PATTERN.match(str(line or ""))
    return m.group(1) if m else ""


def _pull_feedback(container: Any, key: str) -> List[str]:
    if not isinstance(container, dict):
        return []
    direct = _coerce_list(container.get(key))
    if direct:
        return direct
    meta_part = container.get("meta")
    if isinstance(meta_part, dict):
        return _coerce_list(meta_part.get(key))
    return []


def _pull_error(container: Any) -> str:
    if not isinstance(container, dict):
        return ""
    for key in ("ruff_error", "code_quality_error", "collaboration_error"):
        val = container.get(key)
        if val:
            return _safe_str(val)
    meta_part = container.get("meta")
    if isinstance(meta_part, dict):
        for key in ("ruff_error", "code_quality_error", "collaboration_error"):
            val = meta_part.get(key)
            if val:
                return _safe_str(val)
    return ""


def _count_prefixes(lines: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in lines:
        p = _extract_prefix(line)
        if not p:
            continue
        counts[p] = counts.get(p, 0) + 1
    return counts


def _has_entrypoint_solution(code: str, function_name: str = "solution") -> bool:
    name = function_name or "solution"
    pattern = rf"^\s*def\s+{re.escape(name)}\s*\("
    return bool(re.search(pattern, code or "", flags=re.M))


def _is_empty_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in (None, 0, 0.0, "", False)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        if isinstance(node, ast.Dict):
            return len(node.keys) == 0
        return len(node.elts) == 0
    return False


def _has_placeholder(code: str, function_name: str = "") -> bool:
    if not code:
        return True
    effective_lines = [
        ln for ln in (code or "").splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(effective_lines) <= 3:
        return True
    if re.search(r"^\s*pass\s*$", code, flags=re.M):
        return True
    if re.search(r"TODO|FIXME", code, flags=re.I):
        return True
    if re.search(r"NotImplementedError", code):
        return True
    if re.search(r"^\s*\.\.\.\s*$", code, flags=re.M):
        return True
    if re.search(
        r"^\s*return\s*(None|0|0\.0|1|True|False|\"\"|''|\[\s*\]|\{\s*\}|\(\s*\))?\s*$",
        code,
        flags=re.M,
    ):
        lines = [
            ln for ln in (code or "").splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        if len(lines) <= 3:
            return True
    try:
        tree = ast.parse(code)
    except Exception:
        return False

    def _body_is_placeholder(body: List[ast.stmt]) -> bool:
        if not body:
            return True
        if isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant
        ) and isinstance(body[0].value.value, str):
            body = body[1:]
        if not body:
            return True
        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, ast.Pass):
                return True
            if isinstance(stmt, ast.Return):
                if stmt.value is None:
                    return True
                return _is_empty_literal(stmt.value)
        if len(body) == 2:
            assign, ret = body
            if isinstance(assign, ast.Assign) and isinstance(ret, ast.Return):
                if len(assign.targets) == 1 and isinstance(assign.targets[0], ast.Name):
                    if _is_empty_literal(assign.value):
                        return isinstance(ret.value, ast.Name) and (
                            ret.value.id == assign.targets[0].id
                        )
        return False

    target = function_name.strip() if function_name else ""
    has_target = False
    if target:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == target:
                has_target = True
                break
        if not has_target:
            target = ""

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if target and node.name != target:
                continue
            if _body_is_placeholder(node.body):
                return True
    return False


def _count_function_lengths(code: str) -> Dict[str, Any]:
    lines = (code or "").splitlines()
    idxs: List[int] = []
    for i, ln in enumerate(lines):
        if re.match(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", ln):
            idxs.append(i)
    if not idxs:
        return {"fn_count": 0, "max_fn_lines": 0, "avg_fn_lines": 0.0}

    fn_lens: List[int] = []
    for k, start in enumerate(idxs):
        end = idxs[k + 1] if (k + 1) < len(idxs) else len(lines)
        fn_lens.append(max(1, end - start))

    mx = max(fn_lens) if fn_lens else 0
    avg = (sum(fn_lens) / len(fn_lens)) if fn_lens else 0.0
    return {"fn_count": len(fn_lens), "max_fn_lines": mx, "avg_fn_lines": round(avg, 2)}


def _pick_top_feedback(lines: List[str], limit: int = 12) -> List[str]:
    out: List[str] = []
    seen = set()
    for line in lines:
        s = (line or "").strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out
