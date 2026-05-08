# backend/interview_engine/nodes/create_report_node.py
from __future__ import annotations

from typing import Any, Dict, List

from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)
from interview_engine.utils.checkpoint_reader import load_chapter_channel_values

from .report_helpers import (
    _clamp01,
    _grade_from_score,
    _safe_str,
    _level_from_count,
    _generate_problem_solving_evaluation,
    _generate_detailed_feedback_with_llm,
    _generate_recommendation_query,
)

def create_report_node(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[create_report_node] ENTER")
    try:
        # 점수 추출
        code_feedback = _safe_str(state.get("code_collab_feedback") or "")

        problem_score = _clamp01(
            state.get("problem_eval_score")
            if state.get("problem_eval_score") is not None
            else state.get("problem_score")
            or 0.0
        )
        problem_feedback = _safe_str(
            state.get("problem_eval_feedback")
            if state.get("problem_eval_feedback") is not None
            else state.get("problem_feedback")
            or ""
        )

        # evidence
        problem_evidence = dict(state.get("problem_evidence") or {})
        code_collab_evidence = dict(state.get("code_collab_evidence") or {})
        strategy_algorithms = problem_evidence.get("strategy_algorithms") or []

        # 35/30/35 기준 원점수 합산으로 최종 점수 계산
        code_quality_score_raw_35 = float(state.get("code_quality_score_35") or 0.0)
        code_collab_score_raw_30 = float(state.get("code_collab_score_30") or 0.0)
        problem_score_raw_35 = round(problem_score * 35.0, 2)

        code_quality_score_raw_35 = max(0.0, min(35.0, code_quality_score_raw_35))
        code_collab_score_raw_30 = max(0.0, min(30.0, code_collab_score_raw_30))
        problem_score_raw_35 = max(0.0, min(35.0, problem_score_raw_35))

        final_score_raw_100 = round(
            code_quality_score_raw_35 + code_collab_score_raw_30 + problem_score_raw_35,
            2,
        )
        code_quality_percent = round((code_quality_score_raw_35 / 35.0) * 100.0, 2)
        code_collab_percent = round((code_collab_score_raw_30 / 30.0) * 100.0, 2)
        problem_score_percent = round((problem_score_raw_35 / 35.0) * 100.0, 2)
        final_score01 = _clamp01(final_score_raw_100 / 100.0)
        final_grade = _grade_from_score(final_score01)

        # 코드와 문제 텍스트 추출
        code = _safe_str(problem_evidence.get("submitted_code") or "")
        problem_text = _safe_str(problem_evidence.get("problem_text") or "")
        
        # ✅ 세션 ID 가져오기
        meta = state.get("meta") or {}
        session_id = _safe_str(meta.get("session_id"))
        
        # ✅ 카테고리/난이도/알고리즘 초기화
        problem_category = "미분류"
        problem_difficulty = "미정"
        problem_id = None
        problem_algorithms: List[str] = []
        
        # ✅ problem_evidence가 비어있으면 직접 가져오기
        if not code and session_id:
            logger.info("[DEBUG] problem_evidence에 코드 없음, 직접 Redis에서 가져옴")
            code_key = f"livecoding:{session_id}:code"
            code_data = cache.get(code_key) or {}
            latest = code_data.get("latest") or {}
            code = _safe_str(latest.get("code") or "")
            logger.info(f"[DEBUG] Redis에서 가져온 코드 길이: {len(code)}")
        
        if not problem_text and session_id:
            logger.info("[DEBUG] problem_evidence에 문제 없음, Redis problem_payload에서 가져옴")
            # ✅ checkpoint 대신 problem_payload 사용!
            problem_key = f"livecoding:{session_id}:problem"
            problem_payload = cache.get(problem_key) or {}
            problem_text = _safe_str(problem_payload.get("problem") or "")
            logger.info(f"[DEBUG] Redis problem_payload에서 가져온 문제 길이: {len(problem_text)}")
            
            # ✅ 카테고리/난이도/알고리즘도 같이 가져오기!
            if "category" in problem_payload:
                problem_category = problem_payload.get("category") or "미분류"
                logger.info(f"[DEBUG] Redis에서 카테고리 가져옴: {problem_category}")
            
            if "difficulty" in problem_payload:
                problem_difficulty = problem_payload.get("difficulty") or "미정"
                logger.info(f"[DEBUG] Redis에서 난이도 가져옴: {problem_difficulty}")

            if "algorithm" in problem_payload:
                raw_algos = problem_payload.get("algorithm")
                if isinstance(raw_algos, list):
                    problem_algorithms = [str(a) for a in raw_algos if a]
                elif isinstance(raw_algos, str):
                    try:
                        import json as _json
                        parsed = _json.loads(raw_algos)
                        if isinstance(parsed, list):
                            problem_algorithms = [str(a) for a in parsed if a]
                    except Exception:
                        problem_algorithms = []
            
            # ✅ 그래도 없으면 checkpoint 시도
            if not problem_text:
                logger.info("[DEBUG] problem_payload도 없음, checkpoint에서 시도")
                chap1 = load_chapter_channel_values(session_id, "chapter1")
                problem_text = _safe_str(chap1.get("problem_data") or "")
                logger.info(f"[DEBUG] checkpoint에서 가져온 문제 길이: {len(problem_text)}")
        
        # ✅ 2순위: 여전히 "미분류"/"미정"이면 meta에서 problem_id 가져와서 DB 조회
        if (problem_category == "미분류" or problem_difficulty == "미정" or not problem_algorithms):
            # meta에서 problem_id 가져오기
            problem_id = meta.get("problem_id")
            
            # Redis meta에도 있을 수 있음
            if not problem_id and session_id:
                try:
                    meta_key = f"livecoding:{session_id}:meta"
                    cached_meta = cache.get(meta_key) or {}
                    problem_id = cached_meta.get("problem_id")
                    logger.info(f"[DEBUG] Redis meta에서 problem_id 가져옴: {problem_id}")
                except Exception as e:
                    logger.info(f"[WARNING] Redis meta 조회 실패: {e}")
            
            # DB 조회
            if problem_id:
                try:
                    from django.db import connection
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            SELECT category, difficulty, algorithm
                            FROM coding_problem
                            WHERE problem_id = %s
                            LIMIT 1
                        """, [problem_id])
                        row = cursor.fetchone()
                        if row:
                            if problem_category == "미분류":
                                problem_category = row[0] or "미분류"
                            if problem_difficulty == "미정":
                                problem_difficulty = row[1] or "미정"
                            if not problem_algorithms and row[2]:
                                if isinstance(row[2], list):
                                    problem_algorithms = [str(a) for a in row[2] if a]
                                else:
                                    try:
                                        import json as _json
                                        parsed = _json.loads(row[2])
                                        if isinstance(parsed, list):
                                            problem_algorithms = [str(a) for a in parsed if a]
                                    except Exception:
                                        problem_algorithms = []
                            logger.info(
                                f"[DEBUG] DB에서 가져온 정보: 카테고리={problem_category}, "
                                f"난이도={problem_difficulty}, 알고리즘={len(problem_algorithms)}",
                                flush=True,
                            )
                except Exception as e:
                    logger.info(f"[WARNING] DB 조회 실패: {e}")
                    
        # ✅ problem_evidence가 비어있으면 직접 가져오기
        if not code and session_id:
            logger.info("[DEBUG] problem_evidence에 코드 없음, 직접 Redis에서 가져옴")
            code_key = f"livecoding:{session_id}:code"
            code_data = cache.get(code_key) or {}
            latest = code_data.get("latest") or {}
            code = _safe_str(latest.get("code") or "")
            logger.info(f"[DEBUG] Redis에서 가져온 코드 길이: {len(code)}")
        
        if not problem_text and session_id:
            logger.info("[DEBUG] problem_evidence에 문제 없음, Redis problem_payload에서 가져옴")
            # ✅ checkpoint 대신 problem_payload 사용!
            problem_key = f"livecoding:{session_id}:problem"
            problem_payload = cache.get(problem_key) or {}
            problem_text = _safe_str(problem_payload.get("problem") or "")
            logger.info(f"[DEBUG] Redis problem_payload에서 가져온 문제 길이: {len(problem_text)}")
            
            # ✅ 그래도 없으면 checkpoint 시도
            if not problem_text:
                logger.info("[DEBUG] problem_payload도 없음, checkpoint에서 시도")
                chap1 = load_chapter_channel_values(session_id, "chapter1")
                problem_text = _safe_str(chap1.get("problem_data") or "")
                logger.info(f"[DEBUG] checkpoint에서 가져온 문제 길이: {len(problem_text)}")

        # ✅ checkpoint에서 데이터 가져오기
        initial_strategy = ""
        qa_history: List[Dict[str, Any]] = []
        
        if session_id:
            try:
                try:
                    # 1. checkpoint에서 시도
                    chap1 = load_chapter_channel_values(session_id, "chapter1")
                    initial_strategy = chap1.get("user_strategy_answer") or ""
                    
                    # 2. checkpoint에 없으면 Redis cache에서 시도
                    if not initial_strategy:
                        meta_key = f"livecoding:{session_id}:meta"
                        cached_meta = cache.get(meta_key) or {}
                        initial_strategy = cached_meta.get("strategy_answer") or ""
                        logger.info(f"[Fallback] Redis cache에서 전략 답변: {initial_strategy[:50] if initial_strategy else 'None'}")
                    
                except Exception as e:
                    logger.info(f"전략 답변 로드 실패: {e}")
        
                # chapter2에서 질문/응답 로그 가져오기
                chap2 = load_chapter_channel_values(session_id, "chapter2")
                logger.info(f"[DEBUG] chap2 keys: {chap2.keys() if chap2 else 'None'}")
        
                questions = chap2.get("question") or []
                answers = chap2.get("user_answers") or []
                logger.info(f"[DEBUG] questions: {len(questions)}, answers: {len(answers)}")

                # 첫 번째 답변이 전략 답변일 가능성
                if not initial_strategy and answers:
                    initial_strategy = answers[0]
                    logger.info(f"[Fallback] chapter2 첫 답변 사용: {initial_strategy[:50]}")
                
                # ✅ Redis code 데이터에서 확인 (가장 확실한 방법)
                if not initial_strategy:
                    code_key = f"livecoding:{session_id}:code"
                    code_data = cache.get(code_key) or {}
                    
                    # question_history에 있을 수 있음
                    question_history = code_data.get("question_history") or []
                    if question_history:
                        # 첫 질문의 답변이 전략일 수 있음
                        for item in question_history:
                            if isinstance(item, dict):
                                answer = item.get("answer") or item.get("stt_text")
                                if answer:
                                    initial_strategy = answer
                                    logger.info(f"[Fallback] question_history 사용: {initial_strategy[:50]}")
                                    break
                
                # ✅ 최후의 수단: 프론트엔드 localStorage
                # (프론트엔드에서 보낸 경우)
                if not initial_strategy:
                    # API를 통해 받았다면
                    initial_strategy = state.get("initial_strategy") or ""
                    
            except Exception as e:
                logger.info(f"[Data Load Error] {e}")
        
        # ✅ LLM을 사용한 상세 피드백 생성
        logger.info("[create_report_node] LLM 피드백 생성 시작...")
        llm_feedback = _generate_detailed_feedback_with_llm(
            code=code,
            problem_text=problem_text,
            code_score=code_collab_percent,
            problem_score=problem_score_percent,
            code_feedback=code_feedback,
            problem_feedback=problem_feedback,
            evidence={
                **problem_evidence,
                **code_collab_evidence,
            }
        )
        logger.info("[create_report_node] LLM 피드백 생성 완료")
        
        # ✅ 문제 해결 능력 평가 생성
        logger.info("[create_report_node] 문제 해결 능력 평가 시작...")
        ps_evaluation = _generate_problem_solving_evaluation(
            initial_strategy=initial_strategy,
            final_code=code,
            problem_text=problem_text,
            qa_history=qa_history,
        )
        logger.info("[create_report_node] 문제 해결 능력 평가 완료")
        recommendation_query = _generate_recommendation_query(
            improvement=llm_feedback.get("improvement", ""),
            consistency_feedback=ps_evaluation.get("consistency_feedback", ""),
            problem_feedback=problem_feedback,
            problem_algorithms=problem_algorithms or [],
            strategy_algorithms=strategy_algorithms or [],
        )

        collab_rule_20 = float(code_collab_evidence.get("collab_rule_20") or 0.0)
        collab_llm_score_10 = float(code_collab_evidence.get("collab_llm_score_10") or 0.0)

        anti_cheat_summary: Dict[str, Any] = {}
        if session_id:
            event_key = f"livecoding:{session_id}:anti-cheat-events"
            event_payload = cache.get(event_key) or {}
            typing = event_payload.get("typing") or {}
            camera = event_payload.get("camera") or {}
            typing_count = sum(int(v or 0) for v in typing.values())
            camera_count = sum(int(v or 0) for v in camera.values())
            anti_cheat_summary = {
                "typing": {
                    "count": typing_count,
                    "level": _level_from_count(typing_count),
                    "details": typing,
                },
                "camera": {
                    "count": camera_count,
                    "level": _level_from_count(camera_count),
                    "details": camera,
                },
            }

        md = f"""# 코딩 테스트 결과 리포트

## 요약
- 최종 점수: **{final_score_raw_100:.2f}**
- 최종 등급: **{final_grade}**
- 코드 품질 원점수: **{code_quality_score_raw_35:.2f}/35**
- 협업 능력 원점수: **{code_collab_score_raw_30:.2f}/30** (rule {collab_rule_20:.2f}/20 + LLM {collab_llm_score_10:.2f}/10)

## 강점
{llm_feedback['strength']}

## 개선점
{llm_feedback['improvement']}

## 부정행위 기록 (참고용, 점수 반영 없음)
- 캠 기반: {anti_cheat_summary.get('camera', {}).get('count', 0)}회 ({anti_cheat_summary.get('camera', {}).get('level', '정상')})
- 타이핑/화면 이탈: {anti_cheat_summary.get('typing', {}).get('count', 0)}회 ({anti_cheat_summary.get('typing', {}).get('level', '정상')})

## 종합 평가
{llm_feedback['comprehensive_evaluation']}
"""

        # ✅ FinalEvalState에 모든 필드 저장
        state["final_score"] = round(final_score_raw_100, 0)
        state["final_grade"] = final_grade
        state["final_report_markdown"] = md
        state["problem_eval_score"] = round(problem_score, 4)
        state["problem_eval_feedback"] = problem_feedback

        # ✅ graph_output에 프론트엔드가 필요한 모든 필드 포함
        state["graph_output"] = {
            # 점수들 (100점 만점)
            "prompt_score": round(code_quality_percent, 0),  # 프롬프트 점수 (코드 품질 점수 사용)
            "problem_solving_score": round(problem_score_percent, 2),
            "collaboration_score": round(code_collab_percent, 2),
            "code_quality_score": round(code_quality_percent, 2),
                        
            "collaboration_score_raw_30": round(code_collab_score_raw_30, 2),
            "code_quality_score_raw_35": round(code_quality_score_raw_35, 2),
            "problem_solving_score_raw_35": round(problem_score_raw_35, 2),
            "collaboration_rule_20": round(collab_rule_20, 2),
            "collaboration_llm_10": round(collab_llm_score_10, 2),

            "final_score": round(final_score_raw_100, 0),
            "final_grade": final_grade,
            
            # LLM 생성 피드백
            "strength": llm_feedback["strength"],
            "improvement": llm_feedback["improvement"],
            "comprehensive_evaluation": llm_feedback["comprehensive_evaluation"],
            "annotated_code": llm_feedback["annotated_code"],
            "cheating_warning": llm_feedback["cheating_warning"],
            "anti_cheat_summary": anti_cheat_summary,
            "recommendation_query": recommendation_query,

            "problem_category": problem_category,
            "problem_difficulty": problem_difficulty,
            "problem_algorithms": problem_algorithms,
            "problem_id": problem_id,
            "strategy_algorithms": strategy_algorithms,

            # 문제 해결 능력 평가 추가
            "problem_solving_evaluation": {
                "initial_strategy": initial_strategy or "초기 전략 답변이 기록되지 않았습니다.",
                "problem_understanding": ps_evaluation["problem_understanding"],
                "understanding_feedback": ps_evaluation["understanding_feedback"],
                "approach_validity": ps_evaluation["approach_validity"],
                "consistency_status": ps_evaluation["consistency_status"],
                "consistency_feedback": ps_evaluation["consistency_feedback"],
                "qa_history": qa_history,
            },
            
            # ✅ 문제 텍스트 추가
            "problem_text": problem_text,
            "submitted_code": code,
            
            # 추가 정보
            "code_feedback": code_feedback,
            "problem_feedback": problem_feedback,
        }

        # 완료 상태
        state["step"] = "saved"
        state["status"] = "done"
        state["error"] = None

        logger.info(f"[create_report_node] 완료 - 최종점수: {final_score_raw_100:.2f}, 등급: {final_grade}")
        return state

    except Exception as e:
        import traceback
        traceback.print_exc()

        state["step"] = "error"
        state["status"] = "error"
        state["error"] = f"{type(e).__name__}: {e}"
        state["graph_output"] = {
            "error": str(e),
            "strength": "평가 중 오류가 발생했습니다.",
            "improvement": "평가 중 오류가 발생했습니다.",
            "comprehensive_evaluation": "시스템 오류로 평가를 완료하지 못했습니다.",
            "annotated_code": "# 오류 발생",
        }
        return state
