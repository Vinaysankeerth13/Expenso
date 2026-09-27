"""HTMX partial endpoints — returns HTML fragments, never full pages."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard_service import (
    get_transactions_page_data,
    get_category_options,
    update_category,
    undo_category,
)
from app import db_dashboard, db_transactions
from app.services.formatting import display_merchant
from app.services.nlq_service import ask as nlq_ask

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["display_merchant"] = display_merchant

_RETRY_NOTICES = {
    "inserted": "Recorded as a transaction and added to your totals.",
    "duplicate": "Matched a transaction that was already recorded; totals are unchanged.",
    "rejected": "Recognized as a non-transaction notice and removed from review.",
    "review": "Still needs review with the current parser.",
}


@router.get("/filtered-transactions")
async def filtered_transactions(
    request: Request, q: str = "", category: str = "", page: int = 1, review: bool = False
):
    """Returns a filtered transaction rows partial for the live search UI."""
    data = get_transactions_page_data(
        query=q or None,
        category=category or None,
        page=page,
        needs_review=review,
    )
    return templates.TemplateResponse(
        "partials/transaction_rows.html",
        {"request": request, **data, "statsOob": True},
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
async def update_transaction_category(
    request: Request,
    ref: int = Form(...),
    category: str = Form(...),
    learn_merchant: bool = Form(False),
):
    """Updates the category of a transaction and returns the re-rendered row for inline swap."""
    try:
        result = update_category(ref, category, learn_merchant=learn_merchant)
    except ValueError:
        return HTMLResponse("Invalid category edit", status_code=422)

    if not result.get("success"):
        return HTMLResponse(
            '<div class="text-xs text-red-500 px-4 py-2">Update failed</div>',
            status_code=422,
        )

    return _edit_response(request, result, changed=result["batchCount"] > 0)


@router.post("/undo-category")
def undo_transaction_category(
    request: Request, edit_id: int = Form(...), from_notice: bool = Form(False)
):
    try:
        result = undo_category(edit_id)
    except ValueError:
        return HTMLResponse(
            "This edit has changed and cannot be undone. Reload to see the current category.",
            status_code=409,
        )
    changed = from_notice or result["batchReverted"] > 0
    if from_notice:
        return _edit_response(request, result, changed, "partials/edit_notice_undone.html")
    return _edit_response(request, result, changed)


def _edit_response(request, txn, changed, template="partials/category_edit_response.html"):
    """Row (or notice) plus live review counts; a batch change also refreshes the list."""
    response = templates.TemplateResponse(
        template,
        {
            "request": request,
            "txn": txn,
            "categoryOptions": get_category_options(),
            "needsReviewCount": db_dashboard.get_low_confidence_count(),
            "reviewCountOob": True,
        },
    )
    if changed:
        response.headers["HX-Trigger-After-Settle"] = "transactions-changed"
    return response


@router.post("/retry-review")
def retry_review(request: Request, raw_email_id: str = Form(...)):
    """Reparse one quarantined message with the current parser."""
    from app.db_extraction import get_reviews
    from app.extract_transactions import extract_and_insert_transactions

    emails = db_transactions.get_raw_emails_by_ids([raw_email_id])
    if not emails:
        return HTMLResponse("Message not found", status_code=404)
    try:
        stats = extract_and_insert_transactions(emails)
        outcome = next(o for o in _RETRY_NOTICES if stats.get(o))
        notice = _RETRY_NOTICES[outcome]
    except Exception:
        log.exception("Retry failed for review message %s", raw_email_id)
        outcome, notice = "review", "Retry failed; the message is unchanged. Please try again."

    reviews = get_reviews(limit=1, raw_email_id=raw_email_id)
    return templates.TemplateResponse(
        "partials/review_retry_response.html",
        {
            "request": request,
            "raw_email_id": raw_email_id,
            "item": reviews["reviews"][0] if reviews["reviews"] else None,
            "notice": notice,
            "show_transactions_link": outcome == "inserted",
            "count": reviews["count"],
            "countOob": True,
        },
    )
