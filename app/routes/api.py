"""
HTMX partial endpoints. Returns HTML fragments, not JSON.
Only imports from the service layer.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard_service import (
    get_transactions_page_data,
    get_category_options,
    update_category,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/filtered-transactions")
async def filtered_transactions(
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
    return templates.TemplateResponse(
        "partials/transaction_rows.html",
        {"request": request, **data},
    )


@router.post("/update-category")
async def update_transaction_category(
    request: Request,
    ref: int = Form(...),
    category: str = Form(...),
):
    result = update_category(ref, category)

    if not result.get("success"):
        return HTMLResponse(
            '<div class="text-xs text-red-500 px-4 py-2">Update failed</div>',
            status_code=422,
        )

    return templates.TemplateResponse(
        "partials/transaction_row_single.html",
        {
            "request": request,
            "txn": result,
            "categoryOptions": get_category_options(),
        },
    )
