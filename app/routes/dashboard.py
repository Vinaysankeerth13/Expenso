"""Dashboard page routes — renders full HTML pages via Jinja2."""

from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard_service import (
    get_navigation_data,
    get_year_page_data,
    get_month_page_data,
    get_transactions_page_data,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_YEAR_MIN = 2000
_YEAR_MAX = 2100


@router.get("/pipeline")
def pipeline_status(request: Request):
    from app.pipeline_health import get_pipeline_health
    from fastapi.responses import HTMLResponse

    try:
        data = get_pipeline_health()
    except Exception:
        return HTMLResponse("Pipeline status is unavailable. Please retry when the database is reachable.", status_code=503)
    return templates.TemplateResponse("pipeline.html", _ctx(request, data))


@router.get("/review")
def extraction_review(request: Request, page: int = Query(1, ge=1)):
    """Show quarantined notifications without including them in financial totals."""
    from app.db_extraction import get_reviews

    data = get_reviews(limit=50, offset=(page - 1) * 50)
    return templates.TemplateResponse("review.html", _ctx(request, {
        **data, "page": page, "has_next": page * 50 < data['count'],
    }))


def _ctx(request: Request, data: dict) -> dict:
    """Merges page data with the request-scoped nav globals for template rendering."""
    return {
        "request": request,
        "now_year": request.state.now_year,
        "now_month": request.state.now_month,
        **data,
    }


@router.get("/")
async def index():
    """Redirects the root URL to the current month, the landing page."""
    now = datetime.now()
    return RedirectResponse(url=f"/month/{now.year}/{now.month}", status_code=302)


@router.get("/year/{year}")
async def year_page(request: Request, year: int):
    """Renders the annual spending overview for the given year."""
    if not (_YEAR_MIN <= year <= _YEAR_MAX):
        return RedirectResponse(url=f"/year/{datetime.now().year}", status_code=302)
    data = get_year_page_data(year)
    return templates.TemplateResponse("year.html", _ctx(request, data))


@router.get("/month/{year}/{month}")
async def month_page(request: Request, year: int, month: int):
    """Renders the monthly spending detail view for the given year and month."""
    if not (_YEAR_MIN <= year <= _YEAR_MAX) or not (1 <= month <= 12):
        return RedirectResponse(url=f"/year/{datetime.now().year}", status_code=302)
    data = get_month_page_data(year, month)
    return templates.TemplateResponse("month.html", _ctx(request, data))


@router.get("/ask")
async def ask_page(request: Request):
    """Renders the natural language query page."""
    return templates.TemplateResponse("ask.html", _ctx(request, get_navigation_data()))


@router.get("/transactions")
async def transactions_page(
    request: Request, q: str = "", category: str = "", page: int = 1, review: bool = False
):
    """Renders the searchable transactions browser with optional filters."""
    data = get_transactions_page_data(
        query=q or None,
        category=category or None,
        page=page,
        needs_review=review,
    )
    return templates.TemplateResponse("transactions.html", _ctx(request, data))
