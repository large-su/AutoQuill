"""Evolution read/assignment endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core import evolution, release_windows


router = APIRouter()

class _Assign(BaseModel):
    aid: str
    node_id: str | None = None


@router.get('/api/evolution')
@router.get('/api/evolution/feedback')
def api_evolution(horizon: int = 30, view: str = 'scheme', metric: str = 'likes',
                  since: str = '', until: str = '', aid: str = '', node_id: str = '',
                  as_of: str = '', version: str = '', attribution_basis: str = 'evidence'):
    if metric not in ('reads', 'likes', 'comments', 'collects', 'favors'):
        raise HTTPException(400, '未知反馈指标')
    try:
        options = dict(as_of=as_of or None, version=version)
        if attribution_basis != 'evidence':
            options['attribution_basis'] = attribution_basis
        return evolution.contract(horizon, metric, since, until, aid, node_id, **options)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get('/api/evolution/refreshes')
def api_refreshes():
    return evolution.refresh_status()


@router.post('/api/evolution/releases/refresh')
def api_release_times_refresh():
    return release_windows.sync_releases()


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


