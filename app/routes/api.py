"""HTMX partial endpoints — returns HTML fragments, never full pages."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard_service import (
    get_transactions_page_data,
    get_category_options,
    update_category,
)
from app.services.nlq_service import ask as nlq_ask

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/filtered-transactions")
async def filtered_transactions(request: Request, q: str = "", category: str = "", page: int = 1):
    """Returns a filtered transaction rows partial for the live search UI."""
    data = get_transactions_page_data(
        query=q or None,
        category=category or None,
        page=page,
    )
    return templates.TemplateResponse(
        "partials/transaction_rows.html",
        {"request": request, **data},
    )


@router.post("/ask")
async def ask_question(request: Request, question: str = Form(...)):
    """Accepts a natural language question, runs it through the NLQ service, and returns a response card."""
    result = nlq_ask(question)
    return templates.TemplateResponse(
        "partials/ask_response.html",
        {"request": request, **result},
    )


@router.post("/update-category")
async def update_transaction_category(request: Request, ref: int = Form(...), category: str = Form(...)):
    """Updates the category of a transaction and returns the re-rendered row for inline swap."""
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
