"""FastAPI app: the ranked deal board plus a small JSON API."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import store
from ..config import get_settings
from ..db import get_sessionmaker, init_db
from ..models import Deal, Listing, Product, ScanRun, Target
from ..money import fmt, pct
from ..pipeline.scoring import RISK_WEIGHTS

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = fmt
templates.env.filters["pct"] = pct

STATUSES = ("new", "watching", "bought", "dismissed")
SORTS = {
    "score": Deal.score.desc(),
    "profit": Deal.profit_cents.desc(),
    "roi": Deal.roi.desc(),
    "discount": Deal.discount_pct.desc(),
    "confidence": Deal.match_confidence.desc(),
    "newest": Deal.created_at.desc(),
}


def _blank_to_none(value: Any) -> Any:
    """Treat an empty form field as "no filter" instead of a 422.

    A number input the user never filled in still submits as ``field=``, so
    every optional numeric filter has to tolerate the empty string.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


# Query() must live inside the Annotated: passing Query(...) as the *default*
# makes FastAPI rebuild the field from the plain type and silently drop the
# BeforeValidator, which is exactly the bug this guards against.
BlankableFloat = Annotated[float | None, BeforeValidator(_blank_to_none), Query()]
BlankableInt = Annotated[int | None, BeforeValidator(_blank_to_none), Query()]

DEFAULT_BOARD_LIMIT = 100
DEFAULT_API_LIMIT = 50
MAX_LIMIT = 500


def _clamp_limit(limit: int | None, default: int) -> int:
    return min(max(limit or default, 1), MAX_LIMIT)


_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {"running": False, "last_error": None}


def get_db() -> Session:
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _run_scan_job(max_targets: int | None, demo: bool) -> None:
    """Background scan. Only one at a time -- eBay call budget is shared."""
    if not _scan_lock.acquire(blocking=False):
        log.info("scan already running; ignoring trigger")
        return
    try:
        _scan_state.update(running=True, last_error=None)
        from ..pipeline.scan import ScanService
        from ..sources.demo import DemoEbayClient, demo_products

        settings = get_settings()
        use_demo = demo or settings.demo_mode or not settings.has_ebay_credentials
        service = ScanService(ebay_client=DemoEbayClient() if use_demo else None)
        if use_demo:
            with get_sessionmaker()() as session:
                if not session.scalar(select(func.count()).select_from(Product)):
                    service.sync_products(demo_products())
                    service.build_targets()
        service.run(max_targets=max_targets)
    except Exception as exc:
        log.exception("background scan failed")
        _scan_state["last_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _scan_state["running"] = False
        _scan_lock.release()


def _seed_demo_if_empty() -> None:
    """Give a fresh deploy something to show when no API keys are configured."""
    from ..pipeline.scan import ScanService
    from ..sources.demo import DemoEbayClient, demo_products

    with get_sessionmaker()() as session:
        if session.scalar(select(func.count()).select_from(Product)):
            return
    log.info("seeding demo catalog")
    service = ScanService(ebay_client=DemoEbayClient())
    service.sync_products(demo_products())
    service.build_targets()


def _start_scheduler(interval_minutes: int) -> None:
    """Optional in-process scan loop.

    A separate Railway cron service running ``pokearb scan`` is the better
    setup -- it survives web restarts and does not compete with request
    handling -- but this keeps a single-service deploy workable.
    """

    def loop() -> None:
        import time

        # Let the web process finish booting and pass its first healthcheck.
        time.sleep(30)
        while True:
            try:
                _run_scan_job(None, False)
            except Exception:
                log.exception("scheduled scan failed")
            time.sleep(interval_minutes * 60)

    thread = threading.Thread(target=loop, name="scan-scheduler", daemon=True)
    thread.start()
    log.info("scan scheduler started (every %s minutes)", interval_minutes)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    if settings.seed_demo_on_startup or settings.demo_mode:
        try:
            _seed_demo_if_empty()
        except Exception:
            log.exception("demo seeding failed")
    if settings.scan_interval_minutes > 0:
        _start_scheduler(settings.scan_interval_minutes)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Pokemon Arb",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- health -------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with get_sessionmaker()() as session:
                session.execute(select(1))
            return JSONResponse({"status": "ok", "scan_running": _scan_state["running"]})
        except Exception as exc:
            return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)

    # --- deal board ---------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def board(
        request: Request,
        db: Session = Depends(get_db),
        status: str = Query("new"),
        sort: str = Query("score"),
        set_name: str | None = Query(None),
        min_roi: BlankableFloat = None,
        max_cost: BlankableFloat = None,
        min_confidence: BlankableFloat = None,
        hide_risky: bool = Query(False),
        limit: BlankableInt = None,
    ) -> HTMLResponse:
        limit = _clamp_limit(limit, DEFAULT_BOARD_LIMIT)
        stmt = select(Deal).join(Listing).join(Product).where(Listing.is_active.is_(True))
        if status != "all":
            stmt = stmt.where(Deal.status == status)
        if set_name:
            stmt = stmt.where(Product.set_name == set_name)
        if min_roi:
            stmt = stmt.where(Deal.roi >= min_roi)
        if max_cost:
            stmt = stmt.where(Deal.total_cost_cents <= int(max_cost * 100))
        if min_confidence:
            stmt = stmt.where(Deal.match_confidence >= min_confidence)
        if hide_risky:
            stmt = stmt.where(Deal.risk_penalty <= 0.25)
        deals = list(db.scalars(stmt.order_by(SORTS.get(sort, SORTS["score"])).limit(limit)))

        totals = {
            "count": len(deals),
            "profit": sum(d.profit_cents for d in deals),
            "cost": sum(d.total_cost_cents for d in deals),
        }
        sets = list(db.scalars(select(Product.set_name).distinct().order_by(Product.set_name)))
        counts = {
            row[0]: row[1]
            for row in db.execute(select(Deal.status, func.count()).group_by(Deal.status))
        }
        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "deals": deals,
                "totals": totals,
                "sets": sets,
                "status_counts": counts,
                "statuses": STATUSES,
                "sorts": list(SORTS),
                "filters": {
                    "status": status,
                    "sort": sort,
                    "set_name": set_name or "",
                    "min_roi": min_roi or "",
                    "max_cost": max_cost or "",
                    "min_confidence": min_confidence or "",
                    "hide_risky": hide_risky,
                    "limit": limit,
                },
                "last_scan": store.latest_scan(db),
                "scan_state": _scan_state,
                "risk_weights": RISK_WEIGHTS,
            },
        )

    @app.get("/deals/{deal_id}", response_class=HTMLResponse)
    def deal_detail(request: Request, deal_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
        deal = db.get(Deal, deal_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="deal not found")
        return templates.TemplateResponse(
            request,
            "deal.html",
            {
                "deal": deal,
                "settings": get_settings(),
                "risk_weights": RISK_WEIGHTS,
                "statuses": STATUSES,
            },
        )

    @app.post("/deals/{deal_id}/status")
    def set_status(
        deal_id: int,
        status: str = Form(...),
        notes: str = Form(""),
        redirect_to: str = Form("/"),
        db: Session = Depends(get_db),
    ) -> RedirectResponse:
        if status not in STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {STATUSES}")
        deal = db.get(Deal, deal_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="deal not found")
        deal.status = status
        if notes:
            deal.notes = notes
        db.commit()
        return RedirectResponse(redirect_to, status_code=303)

    # --- scans --------------------------------------------------------
    @app.get("/scans", response_class=HTMLResponse)
    def scans(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        runs = list(db.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(50)))
        target_count = db.scalar(select(func.count()).select_from(Target))
        product_count = db.scalar(select(func.count()).select_from(Product))
        listing_count = db.scalar(select(func.count()).select_from(Listing))
        return templates.TemplateResponse(
            request,
            "scans.html",
            {
                "runs": runs,
                "counts": {
                    "targets": target_count,
                    "products": product_count,
                    "listings": listing_count,
                },
                "scan_state": _scan_state,
                "settings": get_settings(),
            },
        )

    @app.post("/scan")
    def trigger_scan(
        background: BackgroundTasks,
        max_targets: int | None = Form(None),
        demo: bool = Form(False),
    ) -> RedirectResponse:
        background.add_task(_run_scan_job, max_targets, demo)
        return RedirectResponse("/scans", status_code=303)

    # --- json api -----------------------------------------------------
    @app.get("/api/deals")
    def api_deals(
        db: Session = Depends(get_db),
        status: str = Query("new"),
        limit: BlankableInt = None,
        min_score: BlankableFloat = None,
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit, DEFAULT_API_LIMIT)
        stmt = (
            select(Deal)
            .join(Listing)
            .where(Listing.is_active.is_(True), Deal.score >= (min_score or 0.0))
        )
        if status != "all":
            stmt = stmt.where(Deal.status == status)
        deals = db.scalars(stmt.order_by(Deal.score.desc()).limit(limit))
        return [
            {
                "id": d.id,
                "score": d.score,
                "status": d.status,
                "card": d.product.name,
                "set": d.product.set_name,
                "title": d.listing.title,
                "url": d.listing.url,
                "price_cents": d.listing.price_cents,
                "shipping_cents": d.listing.shipping_cents,
                "total_cost_cents": d.total_cost_cents,
                "market_value_cents": d.market_value_cents,
                "net_proceeds_cents": d.net_proceeds_cents,
                "profit_cents": d.profit_cents,
                "roi": d.roi,
                "discount_pct": d.discount_pct,
                "match_confidence": d.match_confidence,
                "risk_flags": d.risk_flags,
                "risk_penalty": d.risk_penalty,
            }
            for d in deals
        ]

    return app


app = create_app()
