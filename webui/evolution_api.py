"""Evolution read/assignment endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core import evolution


router = APIRouter()

class _Assign(BaseModel):
    aid: str
    node_id: str | None = None


@router.get('/api/evolution')
def api_evolution(horizon: int = 30, view: str = 'scheme'):
    return evolution.contract(horizon)


@router.post('/api/evolution/assign')
def api_evolution_assign(spec: _Assign):
    try:
        evolution.assign_article(spec.aid, spec.node_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {'ok': True}


def register_evolution(app):
    """Compatibility hook for servers that use imperative registration."""
    app.include_router(router)


