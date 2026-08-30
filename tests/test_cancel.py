"""Cancelling a running job, and cleaning up after one that was killed."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb import store
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.models import Product, ScanRun, Target
from pokemon_arb.pipeline.scan import ScanService
from pokemon_arb.sources.demo import DemoEbayClient, demo_products
from pokemon_arb.web.app import create_app


@pytest.fixture
def service():
    svc = ScanService(ebay_client=DemoEbayClient(seed=5))
    svc.sync_products(demo_products())
    svc.build_targets(per_set=5)
    return svc


# --- scan cancellation -----------------------------------------------------
def test_cancel_stops_the_scan_early(service):
    run = service.run(should_cancel=lambda: True)
    assert run.status == "cancelled"
    assert run.targets_scanned == 0


def test_cancel_keeps_work_already_done(service):
    """Stopping mid-scan must not throw away targets already processed."""
    original = service._scan_target
    completed = {"n": 0}

    def counting(*args, **kwargs):
        original(*args, **kwargs)
        completed["n"] += 1

    service._scan_target = counting
    run = service.run(should_cancel=lambda: completed["n"] >= 2)

    assert run.status == "cancelled"
    assert run.targets_scanned == 2
    assert run.listings_seen > 0
    with get_sessionmaker()() as session:
        assert session.query(Target).filter(Target.last_scanned_at.isnot(None)).count() == 2


def test_uncancelled_scan_is_unaffected(service):
    assert service.run(should_cancel=lambda: False).status == "ok"
    assert service.run().status == "ok"


def test_cancelled_scan_is_not_left_running(service):
    service.run(should_cancel=lambda: True)
    with get_sessionmaker()() as session:
        assert not session.scalars(select(ScanRun).where(ScanRun.status == "running")).all()


# --- sync cancellation -----------------------------------------------------
def test_cancel_stops_a_sync_and_keeps_what_it_committed():
    svc = ScanService(ebay_client=DemoEbayClient())
    assert svc.sync_products(demo_products(), should_cancel=lambda: True) == 0

    svc.sync_products(demo_products())
    with get_sessionmaker()() as session:
        assert session.query(Product).count() == len(demo_products())


def test_keyboard_interrupt_marks_the_run_cancelled(service, monkeypatch):
    """Ctrl-C must not leave a row stuck at 'running' either."""

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_scan_target", boom)
    with pytest.raises(KeyboardInterrupt):
        service.run()
    with get_sessionmaker()() as session:
        latest = store.latest_scan(session)
        assert latest.status == "cancelled"
        assert latest.finished_at is not None


# --- reaping rows orphaned by a dead process -------------------------------
def test_reaper_closes_runs_left_by_a_killed_process():
    with get_sessionmaker()() as session:
        session.add(ScanRun(status="running"))
        session.commit()

        assert store.reap_interrupted_runs(session) == 1
        session.commit()

        run = store.latest_scan(session)
        assert run.status == "interrupted"
        assert run.finished_at is not None
        assert "restarted" in run.error


def test_reaper_leaves_finished_runs_alone(service):
    service.run()
    with get_sessionmaker()() as session:
        assert store.reap_interrupted_runs(session) == 0
        assert store.latest_scan(session).status == "ok"


def test_startup_reaps_orphaned_runs():
    """A restart mid-scan otherwise shows an in-progress scan forever."""
    with get_sessionmaker()() as session:
        session.add(ScanRun(status="running"))
        session.commit()

    with TestClient(create_app()) as client:
        body = client.get("/scans").text
    assert "interrupted" in body
    with get_sessionmaker()() as session:
        assert not session.scalars(select(ScanRun).where(ScanRun.status == "running")).all()


# --- the endpoint ----------------------------------------------------------
def test_cancel_endpoint_redirects():
    with TestClient(create_app()) as client:
        response = client.post("/jobs/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/scans"


def test_cancel_endpoint_stops_a_job_in_flight():
    """End to end: a job spinning in a thread stops once /jobs/cancel is hit."""
    from pokemon_arb.web import app as app_module

    started = threading.Event()
    stopped = threading.Event()

    def long_job() -> str:
        started.set()
        while not app_module._job_cancel.is_set():
            if not started.wait(timeout=0.01):
                break
        stopped.set()
        return "cancelled"

    worker = threading.Thread(target=app_module._run_job, args=("scan", long_job), daemon=True)
    worker.start()
    assert started.wait(timeout=5)

    with TestClient(create_app()) as client:
        client.post("/jobs/cancel", follow_redirects=False)

    assert stopped.wait(timeout=5), "job did not observe the cancel flag"
    worker.join(timeout=5)
    assert app_module._job_state["running"] is None


# --- cancelling during a price-guide download ------------------------------
# The download is the longest part of a sync. It used to buffer the whole
# response before yielding a row, so a cancel could not take effect until it
# had finished -- the Cancel button did nothing for the entire download.


def _guide_csv(rows: int) -> str:
    header = (
        "id,console-name,product-name,loose-price,cib-price,new-price,"
        "graded-price,box-only-price,manual-only-price,sales-volume,release-date\n"
    )
    body = "".join(
        f"{i},Pokemon Base Set,Card #{i},10.00,20.00,30.00,40.00,50.00,60.00,5,1999-01-09\n"
        for i in range(rows)
    )
    return header + body


def test_price_guide_download_is_streamed_not_buffered():
    """The first row must arrive before the response body is exhausted.

    Uses a chunked body and records how much was consumed at first yield --
    the buffering version read all of it before yielding anything.
    """
    import httpx
    import respx

    from pokemon_arb.sources.pricecharting import PriceChartingClient

    chunks = [line.encode() for line in _guide_csv(500).splitlines(keepends=True)]
    consumed = {"n": 0}

    def body():
        for chunk in chunks:
            consumed["n"] += 1
            yield chunk

    with respx.mock(base_url="https://www.pricecharting.com") as mock:
        mock.get("/price-guide/download-custom").mock(
            return_value=httpx.Response(200, content=body())
        )
        with PriceChartingClient("token") as client:
            stream = client.iter_price_guide()
            first = next(stream)
            at_first_yield = consumed["n"]
            stream.close()

    assert first.name == "Card #0"
    assert at_first_yield < len(chunks), (
        f"whole body consumed ({at_first_yield}/{len(chunks)} chunks) before the first row"
    )


def test_price_guide_download_stops_when_cancelled():
    import httpx
    import respx

    from pokemon_arb.sources.pricecharting import PriceChartingClient

    seen = []
    with respx.mock(base_url="https://www.pricecharting.com") as mock:
        mock.get("/price-guide/download-custom").mock(
            return_value=httpx.Response(200, text=_guide_csv(1000))
        )
        with PriceChartingClient("token") as client:
            for product in client.iter_price_guide(should_cancel=lambda: len(seen) >= 10):
                seen.append(product)
    assert len(seen) == 10, "download kept going after the cancel"


def test_sync_from_price_guide_stops_when_cancelled():
    import httpx
    import respx

    from pokemon_arb.sources.pricecharting import PriceChartingClient

    with respx.mock(base_url="https://www.pricecharting.com") as mock:
        mock.get("/price-guide/download-custom").mock(
            return_value=httpx.Response(200, text=_guide_csv(1000))
        )
        service = ScanService(pc_client=PriceChartingClient("token"))
        synced = service.sync_from_price_guide(should_cancel=lambda: True)
    assert synced == 0

    from pokemon_arb.models import Product

    with get_sessionmaker()() as session:
        assert session.query(Product).count() == 0


def test_sync_checks_cancel_on_every_row_not_every_hundredth():
    """Rows that are skipped never advanced the old counter."""
    from pokemon_arb.sources.pricecharting import PCProduct

    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return False

    # Products with no ungraded price are skipped by sync_products.
    skipped = [
        PCProduct(
            external_id=f"p{i}", name=f"Card #{i}", set_name="Set", prices={"ungraded_cents": None}
        )
        for i in range(50)
    ]
    ScanService(ebay_client=DemoEbayClient()).sync_products(skipped, should_cancel=should_cancel)
    assert calls["n"] == 50
