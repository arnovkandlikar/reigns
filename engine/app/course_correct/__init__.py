"""Course Correct package (Role D owns the modules; this __init__ is Role B's — keep it empty).

The engine (app/plugins.py) imports `app.course_correct.api` and calls, per PRD §12.5:
    diagnose(session: SessionContext) -> DriftProfile
    build_bubble(level: int, profile: DriftProfile, session: SessionContext) -> BubbleContent
Optional hook (FR-D5):
    on_fix_outcome(session: SessionContext, correction_id: str, fixed: bool) -> None
Until api.py exists the engine uses a simple fallback bubble, so nothing is blocked.
"""
