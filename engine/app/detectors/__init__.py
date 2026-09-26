"""Detectors package (Role C owns the modules; this __init__ is Role B's — keep it empty).

Convention the engine relies on (see app/plugins.py). Each detector module:
  - lives at app/detectors/<name>.py where <name> is one of:
      reference_auditor, claim_verifier, consistency_probe, code_api_checker,
      pushback (Role B), source_faithfulness (Role D)
  - exposes a module-level instance called `detector` with:
        name: str                      # same as the module name
        async def check(self, claim: Claim, session: SessionContext) -> DetectorResult
  - imports its models from app.models (Claim, SessionContext, DetectorResult, Evidence).
You never need to register a detector anywhere: the engine finds it by module name.
"""
