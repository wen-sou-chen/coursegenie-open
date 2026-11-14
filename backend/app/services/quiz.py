"""习题生成：选择题/填空题/简答题（含答案与解析），支持难度分级。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.course import Course, OutlineNode, QuizItem
from app.services import usage as usage_log
from app.services.kb import sync_content_vault
from app.services.llm import build_llm_client
from app.services.quiz_rules import DIFFICULTIES, resolve_quiz_list, row_meta, text_of

logger = logging.getLogger(__name__)

QUIZ_SYSTEM = """QUIZ_JSON
你是出题老师。为知识点生成习题，只输出 JSON：
{"items": [{"qtype": "choice|blank|short|calc|draw|material|experiment",
  "difficulty": "easy|medium|hard", "level": "基础|提升|挑战", "minutes": 3,
  "question": "题干", "options": ["选项...（choice 必填）"], "answer": "答案或选项序号",
  "rubric": "开放题（draw/material/experiment）的评分要点", "explanation": "解析"}]}
要求：① 基础 / 提升 / 挑战三层都要有，难度递增；② 每题给 minutes（预计用时，分钟）；
③ 混合题型，保证答案与解析正确；④ 开放题必须给 rubric（看什么、给几分），不写 answer。"""


def generate_quizzes(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    count: int,
    difficulty: str,
) -> list[QuizItem]:
    client = build_llm_client()
    topic = node.title if node else course.title
    user = f"{topic}\n题量：{count}\n难度：{difficulty}"
    data = usage_log.complete_json(client, QUIZ_SYSTEM, user,
                                   max_tokens=2500, purpose="quiz")

    rows: list[QuizItem] = []
    raw_items = list(data.get("items") or [])[:count]
    # 任务 56：与讲义路径（lecture._persist_quizzes）共用同一套归一 + 落库前校验 ——
    # answer=0 不再被吞成空串、也不再以 int 落库（前端 QuizOut 要求字符串）；
    # 不合规先就这道题重问一次，仍不合规则题面加「【答案待人工确认】」并记日志，不静默。
    fields_list, issues = resolve_quiz_list(raw_items, client=client, limit=count)
    for raw, fields in zip(raw_items, fields_list):
        raw_difficulty = text_of(raw.get("difficulty")).lower() if isinstance(raw, dict) else ""
        row = QuizItem(
            course_id=course.id,
            node_id=node.id if node else None,
            qtype=fields["qtype"],
            difficulty=fields["difficulty"] if raw_difficulty in DIFFICULTIES else difficulty,
            question=fields["question"],
            options=fields["options"],
            answer=fields["answer"],
            explanation=fields["explanation"],
            # 任务 55：分层 / 预计用时 / 开放题评分要点
            meta=row_meta(fields),
        )
        db.add(row)
        rows.append(row)
    for issue in issues:
        logger.warning("习题答案待人工确认：独立出题第 %s 题 —— %s（%s）",
                       issue["index"], issue["problem"], issue["question"][:60])
    db.commit()
    for row in rows:
        db.refresh(row)
    if rows and rows[0].node_id:
        try:
            sync_content_vault(db, course.id, rows[0].node_id, "quiz")
        except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
            pass
    return rows
