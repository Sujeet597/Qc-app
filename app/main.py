from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, dashboard, inspections, masters, purchase_orders, users
from app.checklist import CHECKLIST
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.seed import seed


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)  # use Alembic migrations once the schema settles
    if settings.seed_demo_data:
        with SessionLocal() as db:
            seed(db)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(auth.router)
app.include_router(purchase_orders.router)
app.include_router(inspections.router)
app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(masters.router)


app.mount("/app", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="app")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/")


@app.get("/api/checklist", tags=["checklist"])
def checklist():
    return [c.__dict__ for c in CHECKLIST]


@app.get("/health")
def health():
    return {"status": "ok", "ai_provider": settings.ai_provider}
