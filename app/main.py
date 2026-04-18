import logging
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

load_dotenv()

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
