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
    calls = {"n": 0}

    def cancel_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    run = service.run(should_cancel=cancel_after_two)
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
