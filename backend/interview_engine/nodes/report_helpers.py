import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from interview_engine.llm import get_llm

logger = logging.getLogger(__name__)


def _clamp01(x: float) -> float:
    try:
        x = float(x)
    except Exception:
        return 0.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _grade_from_score(score01: float) -> str:
    s = _clamp01(score01)
    if s >= 0.95:
        return "A+"
    if s >= 0.9:
        return "A"
    if s >= 0.85:
        return "B+"
    if s >= 0.8:
        return "B"
    if s >= 0.75:
        return "C+"
    if s >= 0.7:
        return "C"
    if s >= 0.6:
        return "D"
    return "F"


def _safe_str(x: Any) -> str:
    return "" if x is None else str(x)


def _level_from_count(count: int) -> str:
    if count >= 20:
        return "확실"
    if count >= 10:
        return "의심"
    if count >= 5:
        return "주의"
    return "정상"


def _format_qa_history(qa_history: List[Dict]) -> str:
    if not qa_history:
        return "(질문/응답 내역 없음)"

    lines: List[str] = []
    for i, qa in enumerate(qa_history, 1):
        q = qa.get("question", "")
        a = qa.get("answer", "")
        lines.append(f"Q{i}: {q}")
        lines.append(f"A{i}: {a}")
        lines.append("")

    return "\n".join(lines)


def _parse_problem_eval_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    required = {
        "problem_understanding",
        "understanding_feedback",
        "approach_validity",
        "consistency_status",
        "consistency_feedback",
    }
    if not required.issubset(set(data.keys())):
        return None
    return {
        "problem_understanding": str(data.get("problem_understanding") or "").strip()
        or "평가 중",
        "understanding_feedback": str(data.get("understanding_feedback") or "").strip(),
        "approach_validity": str(data.get("approach_validity") or "").strip()
        or "평가 중",
        "consistency_status": str(data.get("consistency_status") or "").strip()
        or "분석 중",
        "consistency_feedback": str(data.get("consistency_feedback") or "").strip(),
    }


def _generate_problem_solving_evaluation(
    initial_strategy: str,
    final_code: str,
    problem_text: str,
    qa_history: List[Dict],
) -> Dict[str, Any]:
    system_prompt = """당신은 코딩 면접 평가 전문가입니다. 
응시자의 초기 전략 답변과 최종 코드를 비교하여 문제 해결 능력을 평가하세요.
출력은 반드시 JSON만 반환하세요."""

    qa_text = _format_qa_history(qa_history)

    user_prompt = f"""
## 문제
{problem_text[:800]}

## 초기 전략 답변
{initial_strategy if initial_strategy else "(전략 답변 없음)"}

## 최종 제출 코드
```python
{final_code[:1500]}
```

## 면접 중 질문/응답
{qa_text}

---

다음 JSON 형식으로만 출력하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "problem_understanding": "우수|양호|부족",
  "understanding_feedback": "문제 이해도 설명",
  "approach_validity": "우수|양호|부족",
  "consistency_status": "일치|개선하여 구현|불일치",
  "consistency_feedback": "일관성 설명"
}}
"""

    try:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        logger.info(
            "[LLM][_generate_problem_eval_with_llm] system_prompt=%s",
            system_prompt,
        )
        logger.info(
            "[LLM][_generate_problem_eval_with_llm] user_prompt=%s",
            user_prompt,
        )
        model = get_llm("report")
        response = model.invoke(messages)
        content = response.content
        parsed = _parse_problem_eval_json(content)
        if parsed:
            return parsed

        retry_messages = [
            SystemMessage(content="JSON만 출력하세요. 다른 텍스트, 설명, 코드블록 금지."),
            HumanMessage(content=user_prompt),
        ]
        response_retry = model.invoke(retry_messages)
        parsed_retry = _parse_problem_eval_json(response_retry.content)
        if parsed_retry:
            return parsed_retry

        result = {
            "problem_understanding": "평가 중",
            "understanding_feedback": "",
            "approach_validity": "평가 중",
            "consistency_status": "분석 중",
            "consistency_feedback": "",
        }

        def _extract_label(text: str) -> str:
            if not text:
                return "평가 중"
            t = text.strip()
            if "우수" in t:
                return "우수"
            if "양호" in t:
                return "양호"
            if "부족" in t:
                return "부족"
            if "보통" in t:
                return "양호"
            if "적절" in t:
                return "양호"
            if "부적절" in t:
                return "부족"
            return "평가 중"

        def _first_line(text: str) -> str:
            if not text:
                return ""
            return text.strip().splitlines()[0].strip()

        sections = content.split("###")
        for section in sections:
            section = section.strip()
            if section.upper().startswith("PROBLEM_UNDERSTANDING"):
                text = _first_line(section.replace("PROBLEM_UNDERSTANDING", "", 1))
                label = _extract_label(text)
                if label == "평가 중":
                    label = _extract_label(section)
                result["problem_understanding"] = label
            elif section.upper().startswith("UNDERSTANDING_FEEDBACK"):
                result["understanding_feedback"] = section.replace(
                    "UNDERSTANDING_FEEDBACK", "", 1
                ).strip()
            elif section.upper().startswith("APPROACH_VALIDITY"):
                text = _first_line(section.replace("APPROACH_VALIDITY", "", 1))
                label = _extract_label(text)
                if label == "평가 중":
                    label = _extract_label(section)
                result["approach_validity"] = label
            elif section.upper().startswith("CONSISTENCY_STATUS"):
                text = _first_line(section.replace("CONSISTENCY_STATUS", "", 1))
                result["consistency_status"] = text[:50]
            elif section.upper().startswith("CONSISTENCY_FEEDBACK"):
                result["consistency_feedback"] = section.replace(
                    "CONSISTENCY_FEEDBACK", "", 1
                ).strip()

        return result
    except Exception as e:
        logger.exception("problem solving evaluation failed: %s", e)
        return {
            "problem_understanding": "평가 오류",
            "understanding_feedback": "평가 시스템에 일시적인 문제가 있습니다.",
            "approach_validity": "평가 오류",
            "consistency_status": "분석 실패",
            "consistency_feedback": "평가를 완료하지 못했습니다.",
        }


def _generate_detailed_feedback_with_llm(
    code: str,
    problem_text: str,
    code_score: float,
    problem_score: float,
    code_feedback: str,
    problem_feedback: str,
    evidence: Dict[str, Any],
) -> Dict[str, str]:
    hint_count = evidence.get("hint_count", 0)
    question_cnt = evidence.get("question_cnt", 0)

    system_prompt = """당신은 코딩 면접 평가 전문가입니다. 
응시자의 코드와 면접 과정을 분석하여 상세하고 건설적인 피드백을 제공하세요.
피드백은 구체적이고 실행 가능해야 하며, 응시자의 성장을 돕는 방향으로 작성하세요."""

    user_prompt = f"""
다음 코딩 면접 결과를 분석하고 상세한 피드백을 작성해주세요.

## 문제
{problem_text[:1000]}

## 제출 코드
```python
{code[:2000]}
```

## 평가 점수
- 코드 품질/협업 점수: {code_score:.2f}
- 문제 해결 점수: {problem_score:.2f}

## 자동 평가 피드백
### 코드 품질
{code_feedback[:500]}

### 문제 해결
{problem_feedback[:500]}

## 면접 진행 정보
- 힌트 사용 횟수: {hint_count}회
- 질문/응답 횟수: {question_cnt}회

---

다음 형식으로 피드백을 작성해주세요:

### STRENGTH
(2-3문장으로 응시자의 강점을 구체적으로 설명. 잘한 점, 좋은 접근법, 돋보이는 부분 등)

### IMPROVEMENT
(2-3문장으로 개선이 필요한 부분을 구체적으로 설명. 단순 지적이 아닌 개선 방향 제시)

### COMPREHENSIVE_EVALUATION
(5-7문장으로 종합적인 평가. 전반적인 문제 해결 능력, 코드 품질, 커뮤니케이션 능력, 향후 개발 방향 등을 포괄적으로 평가)

### ANNOTATED_CODE
(원본 코드에 주석을 달아서 제공. 각 부분에 대해 ✅ 좋은 점, ⚠️ 개선 필요, ❌ 감점 요소를 표시)
```python
# 주석이 달린 코드를 여기에 작성
```

### CHEATING_WARNING
(부정행위 의심 사항이 있다면 작성, 없으면 "없음"이라고만 작성)
"""

    try:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        logger.info(
            "[LLM][_generate_detailed_feedback_with_llm] system_prompt=%s",
            system_prompt,
        )
        logger.info(
            "[LLM][_generate_detailed_feedback_with_llm] user_prompt=%s",
            user_prompt,
        )
        model = get_llm("report")
        response = model.invoke(messages)
        content = response.content

        result = {
            "strength": "",
            "improvement": "",
            "comprehensive_evaluation": "",
            "annotated_code": "",
            "cheating_warning": "",
        }

        sections = content.split("###")
        for section in sections:
            section = section.strip()
            if section.upper().startswith("STRENGTH"):
                result["strength"] = section.replace("STRENGTH", "", 1).strip()
            elif section.upper().startswith("IMPROVEMENT"):
                result["improvement"] = section.replace("IMPROVEMENT", "", 1).strip()
            elif section.upper().startswith("COMPREHENSIVE_EVALUATION"):
                result["comprehensive_evaluation"] = section.replace(
                    "COMPREHENSIVE_EVALUATION", "", 1
                ).strip()
            elif section.upper().startswith("ANNOTATED_CODE"):
                code_section = section.replace("ANNOTATED_CODE", "", 1).strip()
                if "```python" in code_section:
                    code_section = code_section.split("```python", 1)[1]
                    if "```" in code_section:
                        code_section = code_section.split("```", 1)[0]
                result["annotated_code"] = code_section.strip()
            elif section.upper().startswith("CHEATING_WARNING"):
                warning = section.replace("CHEATING_WARNING", "", 1).strip()
                if warning and warning.lower() != "없음":
                    result["cheating_warning"] = warning

        if not result["strength"]:
            result["strength"] = "데이터를 불러오는 중 문제가 발생했습니다."
        if not result["improvement"]:
            result["improvement"] = "데이터를 불러오는 중 문제가 발생했습니다."
        if not result["comprehensive_evaluation"]:
            result["comprehensive_evaluation"] = "종합 평가 데이터를 불러오는 중입니다."
        if not result["annotated_code"]:
            result["annotated_code"] = code

        return result
    except Exception as e:
        logger.exception("detailed feedback generation failed: %s", e)
        return {
            "strength": "자동 평가 시스템에 일시적인 문제가 있습니다.",
            "improvement": "자동 평가 시스템에 일시적인 문제가 있습니다.",
            "comprehensive_evaluation": "자동 평가를 완료하지 못했습니다. 관리자에게 문의하세요.",
            "annotated_code": code,
            "cheating_warning": "",
        }


def _generate_recommendation_query(
    improvement: str,
    consistency_feedback: str,
    problem_feedback: str,
    problem_algorithms: List[str],
    strategy_algorithms: List[str],
) -> str:
    system_prompt = (
        "너는 코딩 테스트 추천을 위한 요약 문장을 만든다. "
        "입력된 정보에서 핵심 부족 스킬/개선점을 뽑아 1~2문장으로 요약해라. "
        "알고리즘 키워드를 포함하고, 120자 이내로 작성한다. "
        "불필요한 수식, 코드, 따옴표, 목록 표시는 금지."
    )
    algo_text = ", ".join([str(a) for a in (problem_algorithms or []) if a])
    strategy_text = ", ".join([str(a) for a in (strategy_algorithms or []) if a])
    user_prompt = f"""
알고리즘(문제): {algo_text or "없음"}
알고리즘(전략): {strategy_text or "없음"}
개선점: {improvement[:500]}
일치도 피드백: {consistency_feedback[:500]}
문제 풀이 피드백: {problem_feedback[:500]}

요약 문장 하나로 출력.
"""
    try:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        model = get_llm("report")
        response = model.invoke(messages)
        content = (response.content or "").strip()
        if not content:
            return ""
        return content.splitlines()[0].strip()[:120]
    except Exception as e:
        logger.exception("recommendation query failed: %s", e)
        return ""
