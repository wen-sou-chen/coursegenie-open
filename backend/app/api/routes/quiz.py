"""习题生成/编辑/删除路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import STATUS_APPROVED, Course, OutlineNode, QuizItem
from app.schemas.ops import QuizUpdateRequest
from app.schemas.quiz import QuizGenerateRequest
from app.services.kb import sync_content_vault
from app.services.quiz import generate_quizzes
from app.services.quiz_rules import quiz_level, quiz_minutes, quiz_rubric

router = APIRouter(prefix="/quiz", tags=["quiz"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(payload: QuizGenerateRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成习题")
    node = db.get(OutlineNode, payload.node_id) if payload.node_id else None
    items = generate_quizzes(db, course, node, payload.count, payload.difficulty)
    return [_quiz_out(q) for q in items]


@router.post("/{quiz_id}/vault")
def retry_vault(quiz_id: str, db: Session = Depends(get_db)):
    """把该题所在知识点的笔记增量同步到 Vault（失败只提示，不影响习题）。"""
    quiz = db.get(QuizItem, quiz_id)
    if quiz is None:
        raise HTTPException(404, "习题不存在")
    try:
        result = sync_content_vault(db, quiz.course_id, quiz.node_id, "quiz")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"笔记写入失败：{exc}") from exc
    return {"id": quiz.id, "vault": result}


@router.patch("/{quiz_id}")
def update(quiz_id: str, payload: QuizUpdateRequest, db: Session = Depends(get_db)):
    """就地编辑习题（题干/选项/答案/解析/难度）。"""
    quiz = db.get(QuizItem, quiz_id)
    if quiz is None:
        raise HTTPException(404, "习题不存在")
    for key in ("qtype", "difficulty", "question", "options", "answer", "explanation"):
        value = getattr(payload, key)
        if value is not None:
            setattr(quiz, key, value)
    # 任务 55：分层 / 预计用时 / 评分要点落在 quiz_items.meta（只覆盖显式传了的字段）
    meta_updates = {key: getattr(payload, key) for key in ("level", "minutes", "rubric")}
    meta_updates = {k: v for k, v in meta_updates.items() if v is not None}
    if meta_updates:
        quiz.meta = {**(quiz.meta or {}), **meta_updates}
    db.commit()
    db.refresh(quiz)
    return _quiz_out(quiz)


@router.delete("/{quiz_id}")
def delete(quiz_id: str, db: Session = Depends(get_db)):
    quiz = db.get(QuizItem, quiz_id)
    if quiz is None:
        raise HTTPException(404, "习题不存在")
    db.delete(quiz)
    db.commit()
    return {"ok": True, "id": quiz_id}


def _quiz_out(q: QuizItem) -> dict:
    return {
        "id": q.id,
        "qtype": q.qtype,
        "difficulty": q.difficulty,
        "question": q.question,
        "options": q.options,
        "answer": q.answer,
        "explanation": q.explanation,
        # 任务 55：分层 / 预计用时 / 开放题评分要点（读）
        "level": quiz_level(q),
        "minutes": quiz_minutes(q),
        "rubric": quiz_rubric(q),
    }
