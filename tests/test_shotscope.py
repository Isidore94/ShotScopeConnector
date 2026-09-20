"""Synthetic/offline tests. These do not prove Apple Watch or live login support."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from shotscope_connector.connector import (
    AuthenticationError, DashboardClient, SchemaError, Store, SyncError,
    TokenParser, boolean, csv_data, normalize, process_lock, publish, round_id, sync,
)


@pytest.fixture
def sample():
    return ({"roundID": 123, "startedDate": "2026-09-19T14:00:00-07:00", "courseName": "Synthetic Course",
             "tees": "White", "totalShots": 4, "putts": 2, "state": "Active"},
            {"holes": [{"holeNum": 1, "par": 4, "score": 4, "fairwayInRegulation": False,
                        "greenInRegulation": True, "pin": {"lat": 49.001, "lng": -119.001},
                        "shots": [{"startLat": 49.0, "startLng": -119.0, "endLat": 49.0008,
                                   "endLng": -119.0008, "club": {"name": "8 Iron", "tag": "8i"},
                                   "lie": "Rough", "distance": 140.0, "remaining": 10.0,
                                   "lostBall": False, "waterHazard": False, "positional": "false",
                                   "dateTime": "2026-09-19T21:05:00Z"}]}]})


def test_unknown_units_and_gps_preservation(sample):
    result = normalize(*sample)
    shot = result["shots"][0]
    assert shot["start_lat"] == 49.0 and shot["end_lon"] == -119.0008
    assert shot["distance_source"] == 140
    assert shot["distance_m"] is None and shot["remaining_m"] is None
    assert shot["gps_displacement_m"] > 0
    assert shot["club_normalized"] == "8i"
    assert result["round"]["shots_with_complete_gps"] == 1
    assert result["holes"][0]["penalties"] is None


def test_explicit_units_and_zero(sample):
    sample[1]["holes"][0]["shots"][0]["remaining"] = 0
    result = normalize(*sample, "yd")
    assert result["shots"][0]["distance_m"] == pytest.approx(128.016)
    assert result["shots"][0]["remaining_m"] == 0
    assert normalize(*sample, "m")["shots"][0]["distance_m"] == 140


@pytest.mark.parametrize("source,expected", [(False, False), ("false", False), ("0", False),
                                           (True, True), ("true", True), (None, None), ("unknown", None)])
def test_booleans_do_not_use_truthiness(source, expected):
    assert boolean(source) is expected


def test_no_penalty_putt_or_sg_invention(sample):
    sample[1]["holes"][0]["shots"][0].update(lostBall=True, waterHazard=True, lie="Green")
    result = normalize(*sample, "m")
    assert result["shots"][0]["lost_ball"] is True
    assert result["shots"][0]["penalty_strokes"] is None
    assert result["shots"][0]["strokes_gained"] is None
    assert result["holes"][0]["putts"] is None
    assert result["round"]["putts_source"] == 2


def test_missing_and_out_of_range_gps(sample):
    sample[1]["holes"][0]["shots"][0]["startLat"] = 1234
    result = normalize(*sample)
    assert result["shots"][0]["start_lat"] is None
    assert result["shots"][0]["gps_displacement_m"] is None
    assert result["round"]["shots_with_complete_gps"] == 0
    assert any("GPS" in warning["warning"] for warning in result["warnings"])


def test_unsupported_schema_is_error_and_duplicates_rejected(sample):
    with pytest.raises(SchemaError):
        normalize(sample[0], {"newShape": []})
    sample[1]["holes"].append(deepcopy(sample[1]["holes"][0]))
    with pytest.raises(SchemaError):
        normalize(*sample)


def test_missing_pin_array_order_not_guessed(sample):
    sample[1]["holes"][0]["pin"] = [-119.001, 49.001]
    result = normalize(*sample)
    assert result["holes"][0]["pin_lat"] is None
    assert result["holes"][0]["pin_lon"] is None


def test_identical_physical_shots_not_collapsed(sample):
    shots = sample[1]["holes"][0]["shots"]
    shots.append(deepcopy(shots[0]))
    result = normalize(*sample)
    assert len(result["shots"]) == 2
    assert [row["sequence"] for row in result["shots"]] == [1, 2]


def test_exact_dedupe_edit_replaces_rows_and_preserves_archive(tmp_path, sample):
    store = Store(tmp_path / "data")
    assert store.ingest(*sample, "unknown") is True
    assert store.ingest(*sample, "unknown") is False
    sample[1]["holes"][0]["shots"].append(deepcopy(sample[1]["holes"][0]["shots"][0]))
    assert store.ingest(*sample, "unknown") is True
    assert len(store.all()) == 1
    assert store.status()["shots_stored"] == 2
    assert len(list((tmp_path / "data/raw/123").glob("*.json"))) == 2
    # Changing only the verified unit setting must recompute normalization.
    assert store.ingest(*sample, "m") is True
    assert json.loads(store.all()[0]["normalized_json"])["shots"][0]["distance_m"] == 140


def test_bad_schema_archived_without_overwriting_good_round(tmp_path, sample):
    store = Store(tmp_path)
    store.ingest(*sample, "m")
    old = store.all()[0]["revision"]
    with pytest.raises(SchemaError):
        store.ingest(sample[0], {"wrong_shape": 1}, "m")
    assert store.all()[0]["revision"] == old
    assert len(list((tmp_path / "raw/123").glob("*.json"))) == 2


def test_square_tables_untouched_and_account_isolation(tmp_path, sample):
    with sqlite3.connect(tmp_path / "square.sqlite3") as con:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO sessions VALUES ('square-sentinel')")
    store = Store(tmp_path)
    store.bind_account("sample@example.invalid")
    store.bind_account("SAMPLE@example.invalid")
    with pytest.raises(SyncError):
        store.bind_account("someone-else@example.invalid")
    store.ingest(*sample, "unknown")
    assert store.path.name == "shotscope.sqlite3"
    with sqlite3.connect(tmp_path / "square.sqlite3") as con:
        assert con.execute("SELECT * FROM sessions").fetchall() == [("square-sentinel",)]
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None


def test_pending_publication_retries_without_reimport(tmp_path, sample):
    store = Store(tmp_path / "local")
    store.ingest(*sample, "m")
    output = tmp_path / "missing-drive"
    assert publish(store, output) is False
    assert not output.exists()  # Do not manufacture a missing mounted Drive root.
    assert store.status()["publication_pending"] is True
    output.mkdir()
    assert publish(store, output) is True
    assert store.status()["publication_pending"] is False
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["shot_count"] == 1
    assert manifest["cloud_sync_status"] == "unverified"
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected
    assert len(store.all()) == 1


def test_csv_formula_guard_does_not_change_raw_json():
    data = csv_data([{"club": "=evil()", "distance": -1.5}], ["club", "distance"]).decode("utf-8-sig")
    assert "'=evil()" in data
    assert "-1.5" in data


class FakeClient:
    def __init__(self, details):
        self.details, self.called = details, []

    def detail(self, identity):
        self.called.append(identity)
        result = self.details[identity]
        if isinstance(result, Exception):
            raise result
        return result


def test_sync_fetches_new_and_recent_rounds_and_captures_edits(tmp_path, sample):
    store = Store(tmp_path / "local")
    store.ingest(*sample, "m")
    changed = deepcopy(sample[1])
    changed["holes"][0]["shots"][0]["distance"] = 145
    older = dict(sample[0], roundID=122, startedDate="2026-09-01")
    client = FakeClient({"123": changed, "122": sample[1]})
    result = sync(store, client, {"rounds": [older, sample[0]]}, None, "m", recent=1)
    assert set(client.called) == {"122", "123"}
    assert result["rounds_changed"] == 2
    assert result["drive_status"] == "pending"
    assert store.status()["shots_stored"] == 2


def test_sync_schema_failure_keeps_good_data_and_reports_partial(tmp_path, sample):
    store = Store(tmp_path)
    store.ingest(*sample, "m")
    old = store.all()[0]["revision"]
    client = FakeClient({"123": {"unexpected": []}})
    result = sync(store, client, {"rounds": [sample[0]]}, None, "m")
    assert result["ok"] is False
    assert store.all()[0]["revision"] == old
    assert store.status()["last_successful_sync"] is None


def test_no_rounds_is_not_auth_failure(tmp_path):
    store = Store(tmp_path)
    result = sync(store, FakeClient({}), {"rounds": []}, None)
    assert result["ok"] is True and result["rounds_listed"] == 0
    assert result["rounds_checked"] == 0


def test_explicit_round_must_be_in_account_list(tmp_path, sample):
    with pytest.raises(SyncError):
        sync(Store(tmp_path), FakeClient({}), {"rounds": [sample[0]]}, None, selected_id="999")


def test_absent_rounds_are_flagged_not_deleted(tmp_path, sample):
    store = Store(tmp_path)
    store.ingest(*sample, "m")
    sync(store, FakeClient({}), {"rounds": []}, None)
    assert store.status()["not_in_latest_listing"] == ["123"]
    assert store.status()["rounds_stored"] == 1


def test_token_parser_attribute_order_and_escaping():
    parser = TokenParser()
    parser.feed('<input value="a&amp;b" type="hidden" name="__RequestVerificationToken">')
    assert parser.token == "a&b"


def test_path_traversal_id_rejected():
    with pytest.raises(SchemaError):
        round_id("../123")


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {"Content-Type": "application/json"}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, _):
        yield self.body


class Session:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)
    def close(self):
        pass


def test_normal_login_round_list_and_no_cookie_disk_storage():
    session = Session([
        Response(b'<input name="__RequestVerificationToken" value="test">', headers={"Content-Type": "text/html"}),
        Response(b"", 302, {"Location": "/"}),
        Response(b"home", headers={"Content-Type": "text/html"}),
        Response(b'{"rounds": []}'),
    ])
    client = DashboardClient(session, sleep=lambda _: None)
    assert client.login("user@example.invalid", "synthetic-password") == {"rounds": []}
    assert session.calls[1][2]["data"]["__RequestVerificationToken"] == "test"
    assert session.calls[-1][1].endswith("/api/Rounds/slim")
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


def test_external_redirect_blocked_before_followup():
    session = Session([Response(b"", 307, {"Location": "https://attacker.invalid/"})])
    with pytest.raises(AuthenticationError):
        DashboardClient(session)._request("POST", "/Account/Login", {"Password": "synthetic"})
    assert len(session.calls) == 1


def test_html_not_mistaken_for_empty_rounds():
    session = Session([Response(b"Sign in", headers={"Content-Type": "text/html"})])
    with pytest.raises(AuthenticationError):
        DashboardClient(session).rounds()


def test_duplicate_round_ids_stop_import():
    session = Session([Response(b'{"rounds": [{"roundID": 1}, {"roundID": 1}]}')])
    with pytest.raises(SchemaError):
        DashboardClient(session).rounds()


def test_rate_limit_stops_without_loop():
    session = Session([Response(b"", 429)])
    with pytest.raises(SyncError, match="rate-limited"):
        DashboardClient(session).rounds()
    assert len(session.calls) == 1


def test_overlapping_jobs_rejected(tmp_path):
    with process_lock(tmp_path / "sync.lock"):
        with pytest.raises(SyncError, match="Another Shot Scope job"):
            with process_lock(tmp_path / "sync.lock"):
                pass
