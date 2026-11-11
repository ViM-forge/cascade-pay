from fastapi import FastAPI

app = FastAPI(title="Mock provider (health only)")


@app.get("/health/live")
@app.get("/health/ready")
async def health():
    return {"status": "ok"}
