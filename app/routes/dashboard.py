"""
Dashboard page routes. Returns full HTML pages rendered via Jinja2.
Only imports from the service layer — never from DB modules.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime

from app.services.dashboard_service import (
    get_year_page_data,
    get_month_page_data,
    get_transactions_page_data,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, data: dict) -> dict:
    """Build template context with nav globals + page data."""
    return {
        "request": request,
        "now_year": request.state.now_year,
        "now_month": request.state.now_month,
        **data,
    }


@router.get("/")
async def index():
    current_year = datetime.now().year
    return RedirectResponse(url=f"/year/{current_year}", status_code=302)


@router.get("/year/{year}")
async def year_page(request: Request, year: int):
    data = get_year_page_data(year)
    return templates.TemplateResponse("year.html", _ctx(request, data))


@router.get("/month/{year}/{month}")
async def month_page(request: Request, year: int, month: int):
    if month < 1 or month > 12:
        return RedirectResponse(url=f"/year/{year}", status_code=302)
    data = get_month_page_data(year, month)
    return templates.TemplateResponse("month.html", _ctx(request, data))


@router.get("/transactions")
async def transactions_page(
    request: Request,
    q: str = "",
    category: str = "",
    page: int = 1,
):
    data = get_transactions_page_data(
        query=q or None,
        category=category or None,
        page=page,
    )
    return templates.TemplateResponse("transactions.html", _ctx(request, data))
