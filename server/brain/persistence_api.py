"""Workspace-scoped read APIs for the company dashboard."""
from fastapi import APIRouter, HTTPException

from brain.workspace_store import get_workspace

router = APIRouter()


def _authorized(workspace: str, code: str) -> str:
    from app import check_workspace
    if not workspace:
        raise HTTPException(401, 'Workspace and invite code are required')
    trusted, error = check_workspace(workspace, code)
    if error or not trusted:
        raise HTTPException(403, error or 'Wrong invite code')
    return trusted


@router.get('/workspace')
def workspace_info(workspace: str = '', code: str = ''):
    return get_workspace(_authorized(workspace, code))


@router.get('/meetings')
def meetings(workspace: str = '', code: str = ''):
    from brain.meeting_store import list_meetings
    return {'meetings': list_meetings(_authorized(workspace, code))}


@router.get('/tickets')
def tickets(workspace: str = '', code: str = ''):
    from brain.meeting_store import list_tickets
    return {'tickets': list_tickets(_authorized(workspace, code))}
