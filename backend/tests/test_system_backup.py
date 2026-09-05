from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.system_backup import build_export, restore_import


def test_backup_restore_preserves_relational_and_panel_data(db, user, transcript):
    suite = models.EvalSuite(
        user_id=user.id,
        name="Audit suite",
        prompts_json=["question"],
        models_json=[],
    )
    db.add(suite)
    db.flush()
    run = models.EvalRun(
        suite_id=suite.id,
        user_id=user.id,
        results_json=[{"answer": "ok"}],
        summary_json={"count": 1},
    )
    db.add(run)
    transcript.mode = "deliberation"
    historical = datetime(2020, 1, 2, 3, 4, 5)
    transcript.created_at = historical
    transcript.updated_at = historical
    user.new_chat_use_default_persona = True
    panel_run = models.DeliberationRun(
        user_id=user.id,
        session_id=transcript.id,
        turn_id=transcript.turns[0].id,
        status="converged",
        prompt="panel question",
        synthesis="panel answer",
        converged=True,
        rounds_used=2,
    )
    db.add(panel_run)
    db.flush()
    panel_step = models.DeliberationStep(
        run_id=panel_run.id,
        lane_id=transcript.lanes[0].id,
        message_id=transcript.lanes[0].messages[0].id,
        round_index=0,
        phase="draft",
        model="model-0",
        raw_text="draft answer",
    )
    snapshot = models.AnswerSnapshot(
        user_id=user.id,
        session_id=transcript.id,
        prompt="panel question",
        model="model-0",
        content="pinned answer",
    )
    db.add_all([panel_step, snapshot])
    db.commit()
    run_id = run.id
    suite_id = suite.id
    panel_run_id = panel_run.id
    panel_step_id = panel_step.id
    snapshot_id = snapshot.id
    transcript_id = transcript.id
    user_id = user.id

    backup = build_export(db, user)
    marker = models.Snippet(user_id=user.id, title="not in backup", content="temporary")
    db.add(marker)
    db.commit()
    marker_id = marker.id
    db.close()

    restore_db = SessionLocal()
    try:
        restore_user = restore_db.get(models.User, user_id)
        restored = restore_import(restore_db, restore_user, backup)

        assert restored["eval_suites"] >= 1
        assert restored["eval_runs"] >= 1
        restored_run = restore_db.scalar(
            select(models.EvalRun).where(models.EvalRun.id == run_id)
        )
        assert restored_run is not None
        assert restored_run.suite_id == suite_id
        assert (
            restore_db.scalar(select(models.Snippet).where(models.Snippet.id == marker_id))
            is None
        )
        restored_panel = restore_db.get(models.DeliberationRun, panel_run_id)
        assert restored_panel is not None
        assert restored_panel.synthesis == "panel answer"
        assert restore_db.get(models.DeliberationStep, panel_step_id) is not None
        assert restore_db.get(models.AnswerSnapshot, snapshot_id) is not None
        assert restore_db.get(models.Session, transcript_id).mode == "deliberation"
        assert restore_db.get(models.Session, transcript_id).created_at == historical
        assert restore_db.get(models.Session, transcript_id).updated_at == historical
        assert restore_db.get(models.User, user_id).new_chat_use_default_persona is True
    finally:
        restore_db.close()
