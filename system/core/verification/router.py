"""FastAPI router for Verification Engine endpoints."""
from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, HTTPException

from system.core.verification.schemas import VerificationReport

router = APIRouter(prefix="/verification", tags=["verification"])


def _get_engine() -> Any:
    from system.core.verification.engine import VerificationEngine
    return VerificationEngine()


@router.post("/{project_id}/verify", response_model=VerificationReport, status_code=201)
async def verify_project(project_id: str, project_path: str = ".", auto_heal: bool = True):
    """Run full verification suite on a project."""
    engine = _get_engine()
    return await engine.verify(project_id, project_path, auto_heal=auto_heal)


@router.get("/report/{report_id}", response_model=VerificationReport)
async def get_report(report_id: str):
    """Retrieve a specific verification report."""
    engine = _get_engine()
    report = await engine.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/project/{project_id}/reports", response_model=List[VerificationReport])
async def list_reports(project_id: str):
    """List all verification reports for a project."""
    engine = _get_engine()
    return await engine.get_project_reports(project_id)
