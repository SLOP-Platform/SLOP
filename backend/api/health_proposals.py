"""backend/api/health_proposals.py

LLM test-proposal sub-router (split out of backend/api/health.py for the #1302
linecount drain). Carries the propose-tests / proposed-tests endpoints.

This APIRouter is mounted into the parent health router via
`router.include_router(...)` in health.py, so every route keeps its original
`/api/v1/health/...` (and deprecated `/api/health/...`) path and inherits the
same control-plane guard wired by `_mount` in backend/api/main.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.rate_limit import limiter
from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.path_guard import PathNotAllowed, safe_component
from backend.platform.ollama_runtime import normalize_llm_agent_config
from backend.core.url_guard_httpx import pinned_async_client

log = get_logger(__name__)
router = APIRouter()


# ── LLM test proposal system ──────────────────────────────────────────────


class TestProposalRequest(BaseModel):
    fix_description: str = Field(..., description="What was fixed and why")
    diff_summary: str = Field("", description="Optional: key lines changed")
    bug_category: str = Field("", description="e.g. method_mismatch, field_not_wired")


@router.post("/propose-tests")
async def propose_tests(req: TestProposalRequest) -> dict[str, Any]:
    """Ask the LLM to propose new test cases based on a recent bug fix.

    The LLM analyzes the fix description and generates Python test code
    following the project's existing test patterns. Tests are written to
    tests/proposed/ — never to tests/ directly. A human must approve via
    POST /health/proposed-tests/{id}/approve.

    Safety: proposed tests are syntax-checked and dry-run collected
    (pytest --collect-only) before being shown to the user.
    """
    import time as _time
    import hashlib
    from pathlib import Path

    PROPOSED_DIR = Path("tests") / "proposed"
    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)

    prompt = f"""You are a Python test engineer reviewing a bug fix in a FastAPI + Vue 3 homelab app called SLOP.

Bug fix description:
{req.fix_description}

Diff summary:
{req.diff_summary or "Not provided"}

Bug category: {req.bug_category or "unknown"}

Generate a focused pytest test class (2-5 test methods) that would have caught this bug BEFORE it was fixed.
Follow these patterns from the existing test suite:
- Use @pytest.fixture with scope="module" for db_path
- Use fastapi.testclient.TestClient for API calls
- Each test has a clear docstring explaining what it catches
- Test names start with test_
- Import from backend.* as needed
- No mocks unless absolutely necessary — test the real behavior

Return ONLY valid Python code. No markdown fences. No explanation outside the code.
Start with: import pytest
"""

    # Try cloud LLM cascade
    try:
        from backend.core.cloud_llm import escalate_to_cloud

        _esc = await escalate_to_cloud(prompt, app_key="", purpose="ai_test_generation")
        proposed_code = _esc.response if _esc and _esc.ok else None
    except Exception:
        proposed_code = None

    # Fallback to local LLM — branch on provider per 0eb5431 pattern
    if not proposed_code:
        try:
            import json as _json
            from backend.core.state import StateDB

            with StateDB() as db:
                _agent_cfg_raw = db.get_setting("llm_agent_config")
            _agent_cfg = normalize_llm_agent_config((
                _json.loads(_agent_cfg_raw)
                if isinstance(_agent_cfg_raw, str)
                else (_agent_cfg_raw or {})
            ))
            _provider = _agent_cfg.get("provider", "ollama")
            if _provider == "llamacpp":
                ollama_url = _agent_cfg.get("llamacpp_url", "http://localhost:8081")
            else:
                ollama_url = _agent_cfg.get("ollama_url", "http://localhost:11434")
            model = _agent_cfg.get("ollama_model") or _agent_cfg.get("model", "phi4-mini")
            async with pinned_async_client(timeout=60) as client:
                resp = await client.post(
                    f"{ollama_url}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False},
                )
                proposed_code = resp.json().get("response", "")
        except Exception as e:
            return {"ok": False, "error": safe_detail(e, "No LLM available.", log=log)}

    if not proposed_code or len(proposed_code) < 100:
        return {"ok": False, "error": "LLM returned empty or too-short response"}

    # Safety validation: must parse as Python
    import ast as _ast

    try:
        _ast.parse(proposed_code)
    except SyntaxError as e:
        return {
            "ok": False,
            "error": safe_detail(e, "LLM-generated code has a syntax error.", log=log),
        }

    # Write to proposed/ with timestamp ID
    proposal_id = hashlib.sha1(
        f"{_time.time()}{req.fix_description}".encode(),
        usedforsecurity=False,  # short non-security ID for the proposal filename
    ).hexdigest()[:8]
    filename = f"test_proposed_{proposal_id}.py"
    proposal_path = PROPOSED_DIR / filename

    header = f'''"""PROPOSED TEST — awaiting human review.

Generated by LLM based on fix: {req.fix_description[:100]}
Bug category: {req.bug_category or "unknown"}

To approve: POST /api/health/proposed-tests/{proposal_id}/approve
To discard: DELETE /api/health/proposed-tests/{proposal_id}

DO NOT run in CI until approved.
"""
'''
    proposal_path.write_text(header + proposed_code)

    # Dry-run collect to check for import errors (not execution)
    import sys

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        str(proposal_path),
        "--collect-only",
        "-q",
        "--tb=short",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(Path(".")),
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    collect_result_returncode = proc.returncode
    collect_result_stdout = stdout.decode()
    collection_ok = collect_result_returncode == 0

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "filename": filename,
        "collection_valid": collection_ok,
        "collection_output": collect_result_stdout[:500] if not collection_ok else None,
        "preview": proposed_code[:500],
        "message": (
            f"Test proposal saved to tests/proposed/{filename}. "
            f"Review with GET /api/health/proposed-tests/{proposal_id}, "
            f"then approve via POST /api/health/proposed-tests/{proposal_id}/approve"
        ),
    }


@router.get("/proposed-tests")
def list_proposed_tests() -> list[dict[str, Any]]:
    """List all pending proposed tests awaiting review."""
    from pathlib import Path

    PROPOSED_DIR = Path("tests") / "proposed"
    if not PROPOSED_DIR.exists():
        return []
    results = []
    for f in sorted(PROPOSED_DIR.glob("test_proposed_*.py")):
        src = f.read_text()
        results.append(
            {
                "id": f.stem.replace("test_proposed_", ""),
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "preview": src[src.find("import") : src.find("import") + 300]
                if "import" in src
                else src[:300],
            }
        )
    return results


@router.post("/proposed-tests/{proposal_id}/approve")
def approve_proposed_test(proposal_id: str) -> dict[str, Any]:
    """Promote a proposed test from tests/proposed/ to tests/.

    Runs a final syntax check before promotion.
    Rollback: git rm tests/test_proposed_{id}.py
    """
    from pathlib import Path
    import ast as _ast

    try:
        proposal_id = safe_component(proposal_id, field="proposal_id")
    except PathNotAllowed as e:
        raise HTTPException(400, detail=safe_detail(e, "Invalid proposal id.", log=log)) from e
    proposed = Path("tests") / "proposed" / f"test_proposed_{proposal_id}.py"
    if not proposed.exists():
        raise HTTPException(404, f"Proposal {proposal_id} not found.")

    # Final syntax check
    src = proposed.read_text()
    try:
        _ast.parse(src)
    except SyntaxError as e:
        raise HTTPException(
            422, detail=safe_detail(e, "Proposed test has a syntax error.", log=log)
        ) from e

    # Remove the warning header comment before promoting
    promoted_src = src[src.find("import pytest") :]  # strip header
    dest = Path("tests") / f"test_proposed_{proposal_id}.py"
    dest.write_text(promoted_src)
    proposed.unlink()

    return {
        "ok": True,
        "promoted_to": str(dest),
        "message": f"Test promoted to tests/. Add to git: git add {dest}",
        "rollback": f"git rm {dest}",
    }


@router.delete("/proposed-tests/{proposal_id}")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # light mutation — proposed test discard (id=467)
def discard_proposed_test(request: Request, proposal_id: str) -> dict[str, Any]:
    """Discard a proposed test without promoting it."""
    from pathlib import Path

    try:
        proposal_id = safe_component(proposal_id, field="proposal_id")
    except PathNotAllowed as e:
        raise HTTPException(400, detail=safe_detail(e, "Invalid proposal id.", log=log)) from e
    proposed = Path("tests") / "proposed" / f"test_proposed_{proposal_id}.py"
    if not proposed.exists():
        raise HTTPException(404, f"Proposal {proposal_id} not found.")
    proposed.unlink()
    return {"ok": True, "discarded": proposal_id}
