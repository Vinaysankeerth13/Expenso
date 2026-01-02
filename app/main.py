from fastapi import FastAPI
app = FastAPI(title="Expenso (dev)")

@app.get("/")
def root():
    """Health check endpoint returning project status."""
    return {"ok": True, "project": "Expenso"}
