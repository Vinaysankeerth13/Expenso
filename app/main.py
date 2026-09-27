import logging
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Expenso")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


class TemplateContextMiddleware(BaseHTTPMiddleware):
    """Injects current year/month into every request for nav links."""
    async def dispatch(self, request, call_next):
        now = datetime.now()
        request.state.now_year = now.year
        request.state.now_month = now.month
        return await call_next(request)

app.add_middleware(TemplateContextMiddleware)


# Register routes
from app.routes.dashboard import router as dashboard_router
from app.routes.api import router as api_router

app.include_router(dashboard_router)
app.include_router(api_router, prefix="/htmx")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
def readiness():
    from app.db_connection import get_connection
    from fastapi.responses import JSONResponse

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT bool_and(to_regclass(name) IS NOT NULL)
                    FROM unnest(ARRAY['transactions','raw_emails','merchant_categories',
                        'extraction_reviews','transaction_notifications','pipeline_runs',
                        'ingestion_checkpoints','email_processing_outcomes','classification_edits']) AS names(name)""")
                ready = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception:
        ready = False
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)
