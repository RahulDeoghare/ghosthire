"""API tests.

Built against a database made from the real snapshots, so these assert what a
reviewer would actually see rather than what a fixture was arranged to show.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ghosthire import api, db, pipeline


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    conn = db.connect(path)
    db.init(conn)
    pipeline.rebuild(conn, quiet=True)
    conn.close()
    monkeypatch.setattr(api, "db", lambda: db.connect(path))
    return TestClient(api.app)


def test_leaderboard_ranks_only_assessed_listings(client):
    rows = client.get("/api/leaderboard").json()

    assert rows, "the archive should produce at least one assessed listing"
    for row in rows:
        assert row["career_page_checked"] == 1
        assert row["ghost_score"] is not None
    assert [r["ghost_score"] for r in rows] == sorted(
        (r["ghost_score"] for r in rows), reverse=True)


def test_an_unassessed_listing_is_never_given_a_score(client):
    """The rule the whole project turns on. These listings exist, are
    returned, and carry no number that could be read as a verdict."""
    rows = client.get("/api/listings", params={"assessed": False}).json()

    assert rows
    for row in rows:
        assert row["ghost_score"] is None
        assert row["band"] is None
        assert row["assessed"] is False


def test_unassessed_listings_never_appear_on_the_leaderboard(client):
    board_ids = {r["id"] for r in client.get("/api/leaderboard").json()}
    unassessed = {r["id"] for r in
                  client.get("/api/listings", params={"assessed": False}).json()}
    assert board_ids & unassessed == set()


def test_every_listing_carries_the_collector_that_produced_it(client):
    """§0.4: a number on screen that cannot be traced to a c_* ID and a
    snapshot on disk should not be on screen."""
    for row in client.get("/api/listings", params={"limit": 500}).json():
        assert row["collector_id"], f"listing {row['id']} has no provenance"
        assert row["collector_id"].startswith("c_")


def test_detail_shows_both_sides_of_the_comparison(client):
    listing = client.get("/api/leaderboard").json()[0]
    detail = client.get(f"/api/listings/{listing['id']}").json()

    assert detail["board_url"]
    # What the match was made against, so a reader can check the verdict
    # instead of trusting it.
    assert detail["career_page_roles"]
    assert all(r["career_url"] for r in detail["career_page_roles"])
    assert set(detail["signal_weights"]) == set(detail["signals"])


def test_a_matched_listing_links_to_the_role_it_matched(client):
    matched = [r for r in client.get("/api/listings", params={"limit": 500}).json()
               if r["assessed"] and r["ghost_score"] == 0]
    assert matched, "the true-negative case must be representable"

    detail = client.get(f"/api/listings/{matched[0]['id']}").json()
    assert detail["matched_career_listing"] is not None
    assert detail["matched_career_listing"]["career_url"]


def test_a_non_match_still_gives_the_reader_somewhere_to_check(client):
    """The claim is "this role is not on their careers page", and checking it
    means opening that page. Without the URL the evidence view offered a
    verdict and no way to test it."""
    unmatched = [r for r in client.get("/api/leaderboard").json()
                 if r["ghost_score"] and r["ghost_score"] > 0]
    assert unmatched, "the archive should hold at least one non-match"

    detail = client.get(f"/api/listings/{unmatched[0]['id']}").json()
    assert detail["board_url"], "the board side must be linkable"
    assert detail["career_page_url"], "the careers-page side must be linkable too"
    assert detail["career_page_url"].startswith("http")


def test_the_app_runs_with_a_relocated_database(tmp_path, monkeypatch):
    """A read-only deployment cannot write beside the repo, so the database
    path has to move via the environment — which `.env.example` documented
    long before it worked."""
    monkeypatch.setenv("GHOSTHIRE_DB", str(tmp_path / "elsewhere.db"))
    import importlib
    from ghosthire import db as db_module
    importlib.reload(db_module)

    assert db_module.DB_PATH == tmp_path / "elsewhere.db"
    conn = db_module.connect()
    db_module.init(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, \
        "foreign_keys is not optional, wherever the file lives"
    conn.close()
    monkeypatch.delenv("GHOSTHIRE_DB")
    importlib.reload(db_module)


def test_connect_survives_a_read_only_filesystem(tmp_path):
    """WAL needs to create a sidecar file. On a read-only deployment that
    fails, and it is not worth failing the request over — WAL buys concurrency
    with a writer, and a read-only replica has no writer.

    Exercised against a directory the OS actually refuses to write, rather
    than a patched sqlite3, so it tests the real failure.
    """
    import os
    import stat
    from ghosthire import db as db_module

    build = tmp_path / "build"
    build.mkdir()
    seeded = db_module.connect(build / "built.db")
    db_module.init(seeded)
    seeded.close()

    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    path = db_module.read_only_copy(build / "built.db", ro_dir / "served.db")
    os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)   # r-x: no new files

    try:
        conn = db_module.connect(path)              # must not raise
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_listings").fetchone()[0] == 0
        conn.close()
    finally:
        os.chmod(ro_dir, stat.S_IRWXU)


def test_the_trigger_is_honestly_unavailable_without_the_cli(client, monkeypatch):
    """On a Python-only host the Bright Data CLI does not exist. Saying so
    beats a 502 that looks like the collector broke."""
    from ghosthire import bdata
    monkeypatch.setattr(bdata.shutil, "which", lambda _: None)

    r = client.post("/api/scrape/trigger",
                    json={"key": "board_company", "company": "razorpay"})
    assert r.status_code == 501
    assert "bdata" in r.json()["detail"]


def test_missing_listing_is_404(client):
    assert client.get("/api/listings/999999").status_code == 404


def test_collectors_panel_exposes_the_ids(client):
    rows = client.get("/api/collectors").json()
    created = [r for r in rows if r["created"]]

    assert created
    for row in created:
        assert row["collector_id"].startswith("c_")
    # Collectors that were never created are shown as such rather than hidden,
    # so the panel cannot imply more coverage than exists.
    assert any(not r["created"] for r in rows)


def test_collectors_sharing_one_id_do_not_share_each_others_counts(client):
    """The board firehose and the per-company board search run on the same
    c_* ID. Counting stored listings by ID alone showed each of them the
    other's rows, so both reported the combined total."""
    rows = {r["key"]: r for r in client.get("/api/collectors").json()}
    firehose, per_company = rows["board_internshala_jobs"], rows["board_company"]

    assert firehose["collector_id"] == per_company["collector_id"]
    assert firehose["listings_stored"] != per_company["listings_stored"]
    assert per_company["listings_stored"] > 0


def test_run_counts_are_not_shared_between_collectors_with_one_id(client):
    """The same defect as listings_stored, in a different column: two keys
    share c_mt1senswibym6o5va, and keying runs by ID alone gave each of them
    the other's runs as well."""
    rows = {r["key"]: r for r in client.get("/api/collectors").json()}
    firehose, per_company = rows["board_internshala_jobs"], rows["board_company"]

    assert firehose["collector_id"] == per_company["collector_id"]
    combined = firehose["runs"] + per_company["runs"]
    assert firehose["runs"] < combined and per_company["runs"] < combined


def test_a_retired_collector_keeps_its_failures_on_the_record(client):
    """The first career collector was built against a landing page, failed on
    two of three targets and was replaced. It is gone from the config, so
    listing only configured collectors silently deleted those failures from
    the reliability record."""
    rows = client.get("/api/collectors").json()
    retired = [r for r in rows if r.get("retired")]

    assert retired, "a collector that ran but is no longer configured must still show"
    assert any(r["runs_failed"] > 0 for r in retired)
    assert all(r["collector_id"].startswith("c_") for r in retired)


def test_an_uncreated_collector_stores_nothing(client):
    for row in client.get("/api/collectors").json():
        if not row["created"]:
            assert row["listings_stored"] == 0


def test_meta_reports_both_populations(client):
    meta = client.get("/api/meta").json()
    assert meta["assessed"] >= 1
    assert meta["not_assessed"] >= 1
    assert meta["signals"]["not_on_career_page"] == 45


def test_trigger_rejects_an_unknown_collector(client):
    assert client.post("/api/scrape/trigger", json={"key": "nope"}).status_code == 404


def test_trigger_refuses_a_collector_with_no_id(client):
    """board_internshala_intern has never been created. The endpoint must say
    so rather than shelling out with the ID None."""
    response = client.post("/api/scrape/trigger",
                           json={"key": "board_internshala_intern"})
    assert response.status_code == 409


def test_trigger_rejects_an_unknown_company(client):
    response = client.post("/api/scrape/trigger",
                           json={"key": "board_company", "company": "nosuchco"})
    assert response.status_code == 404
