from fastapi import FastAPI
app = FastAPI(title="Expenso (dev)")

@app.get("/")
def root():
    return {"ok": True, "project": "Expenso"}
