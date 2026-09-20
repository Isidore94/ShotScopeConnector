"""Standalone Shot Scope dashboard importer and local Drive-folder publisher.

Unofficial protocol reference: marcusgoll/openround-public, shotscope.py.
See README.md and THIRD_PARTY_NOTICES.txt. No live
Apple Watch/account compatibility is implied by the offline tests.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv
import requests

BASE = "https://dashboard.shotscope.com"
VERSION = "shotscope-0.2.0"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
KEYRING_SERVICE = "ShotScopeConnector"


class SyncError(Exception):
    """Messages are fixed, safe diagnostics: never include credentials or bodies."""


class AuthenticationError(SyncError):
    pass


class SchemaError(SyncError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def round_id(value: Any) -> str:
    result = str(value)
    if not re.fullmatch(r"[0-9]{1,24}", result):
        raise SchemaError("Invalid or unsupported round identifier.")
    return result


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def process_lock(path: Path):
    """Kernel lock releases after crashes; Windows and Unix test support."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncError("Another Shot Scope job is running; this run was skipped.") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class TokenParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.token: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "input" and attributes.get("name") == "__RequestVerificationToken":
            self.token = attributes.get("value")


class DashboardClient:
    """Normal dashboard form login; cookie session in memory only."""
    def __init__(self, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        # Avoid implicit .netrc authentication/proxy credentials overriding requests.
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "ShotScopeConnector/0.2 (personal export)"})
        self.sleep = sleep

    def close(self):
        self.session.close()

    def _request(self, method: str, path: str, data=None) -> tuple[bytes, str]:
        url = urljoin(BASE, path)
        for _ in range(6):
            if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "dashboard.shotscope.com":
                raise AuthenticationError("Unexpected sign-in redirect; no credentials were sent to another host.")
            try:
                with self.session.request(method, url, data=data, timeout=(10, 30),
                                          allow_redirects=False, stream=True) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise SyncError("Dashboard returned a redirect without a destination.")
                        url = urljoin(url, location)
                        if response.status_code in (301, 302, 303):
                            method, data = "GET", None
                        continue
                    if response.status_code in (401, 403):
                        raise AuthenticationError("Shot Scope login was rejected or requires interactive attention.")
                    if response.status_code == 429:
                        # Stop instead of repeatedly retrying an authentication/rate block.
                        raise SyncError("Shot Scope rate-limited the request. Wait before running another sync.")
                    if response.status_code >= 400:
                        raise SyncError(f"Shot Scope returned HTTP {response.status_code}; no response body was logged.")
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > MAX_RESPONSE_BYTES:
                            raise SyncError("Dashboard response exceeds the safety size limit.")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("Content-Type", "")
            except requests.RequestException:
                raise SyncError("Network/TLS request failed; existing data was retained.") from None
        raise AuthenticationError("Too many login redirects; manual account attention is required.")

    def _json(self, path: str) -> dict:
        body, content_type = self._request("GET", path)
        if "json" not in content_type.lower():
            raise AuthenticationError("Dashboard returned a page instead of JSON; sign-in/session may have changed.")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise SchemaError("Dashboard returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise SchemaError("Expected a JSON object from the dashboard.")
        return value

    def login(self, email: str, password: str) -> dict:
        body, _ = self._request("GET", "/Account/Login")
        parser = TokenParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        if not parser.token:
            raise AuthenticationError("Login form has changed or needs interactive sign-in; no bypass was attempted.")
        self._request("POST", "/Account/Login", {
            "__RequestVerificationToken": parser.token, "Email": email, "Password": password,
        })
        listing = self.rounds()
        return listing

    def rounds(self) -> dict:
        listing = self._json("/api/Rounds/slim")
        items = listing.get("rounds")
        if not isinstance(items, list):
            raise SchemaError("Round-list schema changed: expected a rounds array.")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise SchemaError("Round-list entry is not an object.")
            identity = round_id(item.get("roundID"))
            if identity in seen:
                raise SchemaError("Round-list contains repeated identifiers; no rounds were overwritten.")
            seen.add(identity)
        return listing

    def detail(self, identity: str) -> dict:
        self.sleep(0.5)
        return self._json(f"/api/v2/rounds/{round_id(identity)}")


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, "0", "false", "False"):
        return False
    if value in (1, "1", "true", "True"):
        return True
    return None


def coordinates(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    latitude, longitude = number(lat), number(lon)
    if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None
    return latitude, longitude


def gps_distance(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    a1, a2 = math.radians(lat1), math.radians(lat2)
    delta_a, delta_b = a2 - a1, math.radians(lon2 - lon1)
    hav = math.sin(delta_a / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(delta_b / 2) ** 2
    return round(2 * 6371000 * math.asin(math.sqrt(min(1.0, max(0.0, hav)))), 3)


def normalized_club(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[\s_-]", "", value.lower())
    if key in ("driver", "d", "dr"):
        return "Driver"
    if key in ("putter", "pt", "p"):
        return "Putter"
    match = re.fullmatch(r"([2-9])(?:i|iron)", key)
    if match:
        return match.group(1) + "i"
    aliases = {"pw": "PW", "pitchingwedge": "PW", "sw": "SW", "sandwedge": "SW",
               "aw": "AW", "gw": "GW", "gapwedge": "GW", "lw": "LW", "lobwedge": "LW"}
    return aliases.get(key, value)  # Unknown names remain explicitly uncollapsed.


def normalize(slim: dict, detail: dict, distance_unit="unknown") -> dict:
    """Preserve source values; avoid SG/penalty/putt inference and unit guessing."""
    identity = round_id(slim.get("roundID"))
    raw_holes = detail.get("holes")
    if not isinstance(raw_holes, list):
        raise SchemaError("Round detail has no holes array; Apple Watch schema needs verification.")
    if distance_unit not in ("unknown", "m", "yd"):
        raise ValueError("Unsupported distance unit")
    warnings, holes, shots, seen = [], [], [], set()
    def warn(hole, seq, message):
        warnings.append({"round_id": identity, "hole": hole, "sequence": seq, "warning": message})
    if distance_unit == "unknown":
        warn(None, None, "Provider distance units unverified; distance_m and remaining_m left empty.")
    factor = {"m": 1.0, "yd": 0.9144}.get(distance_unit)
    for raw_hole in raw_holes:
        if not isinstance(raw_hole, dict):
            raise SchemaError("Hole entry is not an object.")
        hole = number(raw_hole.get("holeNum"))
        if hole is None or not hole.is_integer() or not 1 <= hole <= 36 or hole in seen:
            raise SchemaError("Missing, duplicate or unsupported hole number.")
        hole = int(hole)
        seen.add(hole)
        raw_shots = raw_hole.get("shots")
        if raw_shots is None:
            raw_shots = []
            warn(hole, None, "No shot array supplied for this hole.")
        if not isinstance(raw_shots, list):
            raise SchemaError("Shot collection is not an array.")
        pin = raw_hole.get("pin")
        # Only named-coordinate form is interpreted; array order is not assumed.
        pin_lat, pin_lon = coordinates(pin.get("lat", pin.get("latitude")),
                                       pin.get("lng", pin.get("longitude"))) if isinstance(pin, dict) else (None, None)
        if pin_lat is None:
            warn(hole, None, "Pin coordinates missing/unsupported; no precise proximity inferred.")
        holes.append({"round_id": identity, "hole": hole, "par": number(raw_hole.get("par")),
                      "score": number(raw_hole.get("score")),
                      "fairway_in_regulation": boolean(raw_hole.get("fairwayInRegulation")),
                      "green_in_regulation": boolean(raw_hole.get("greenInRegulation")),
                      "pin_lat": pin_lat, "pin_lon": pin_lon,
                      "recorded_shot_count": len(raw_shots), "putts": None, "penalties": None})
        for seq, shot in enumerate(raw_shots, 1):
            if not isinstance(shot, dict):
                raise SchemaError("Shot entry is not an object.")
            lat, lon = coordinates(shot.get("startLat"), shot.get("startLng"))
            end_lat, end_lon = coordinates(shot.get("endLat"), shot.get("endLng"))
            if lat is None or end_lat is None:
                warn(hole, seq, "Incomplete/invalid start or end GPS coordinates.")
            if (lat, lon) == (0.0, 0.0) or (end_lat, end_lon) == (0.0, 0.0):
                warn(hole, seq, "Zero/zero GPS pair may be a placeholder; retained, not silently removed.")
            club = shot.get("club")
            if isinstance(club, dict):
                club = club.get("name") or club.get("tag")
            club = club if isinstance(club, str) else None
            if club is None:
                warn(hole, seq, "Club label missing or unsupported; no guess made.")
            distance, remaining = number(shot.get("distance")), number(shot.get("remaining"))
            shots.append({"round_id": identity, "hole": hole, "sequence": seq,
                          "club_original": club, "club_normalized": normalized_club(club),
                          "start_lat": lat, "start_lon": lon, "end_lat": end_lat, "end_lon": end_lon,
                          "distance_source": distance, "remaining_source": remaining,
                          "source_distance_unit": distance_unit,
                          "distance_m": distance * factor if distance is not None and factor else None,
                          "remaining_m": remaining * factor if remaining is not None and factor else None,
                          "gps_displacement_m": gps_distance(lat, lon, end_lat, end_lon),
                          "distance_type": "on_course_total_not_carry", "lie": shot.get("lie"),
                          "timestamp_source": shot.get("dateTime"),
                          "lost_ball": boolean(shot.get("lostBall", shot.get("lost"))),
                          "water_hazard": boolean(shot.get("waterHazard")),
                          "positional": boolean(shot.get("positional")),
                          "penalty_strokes": None, "strokes_gained": None,
                          "source_event_id": shot.get("sourceEventId", shot.get("source_event_id"))})
    score = number(slim.get("totalShots"))
    hole_scores = [hole["score"] for hole in holes]
    if score is not None and hole_scores and all(value is not None for value in hole_scores):
        if sum(hole_scores) != score:
            warn(None, None, "Provider hole scores do not sum to the provider round score.")
    if not shots:
        warn(None, None, "No individual shots returned; not proof of a functioning GPS integration.")
    return {"schema_version": VERSION, "round": {
        "round_id": identity, "started_at_source": slim.get("startedDate"),
        "course": slim.get("courseName"), "tees": slim.get("tees"),
        "score": score, "putts_source": number(slim.get("putts")),
        "source_state": slim.get("state"), "holes_recorded": len(holes),
        "shots_recorded": len(shots),
        "shots_with_complete_gps": sum(s["start_lat"] is not None and s["end_lat"] is not None for s in shots),
        "provider_distance_unit": distance_unit,
    }, "holes": holes, "shots": shots, "warnings": warnings}


class Store:
    """Independent SQLite storage; never opens a Square connector database."""
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "shotscope.sqlite3"
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS ss_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ss_rounds (
                    round_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
                    source_hash TEXT NOT NULL, source_path TEXT NOT NULL,
                    normalized_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ss_holes (
                    round_id TEXT NOT NULL, hole INTEGER NOT NULL, data_json TEXT NOT NULL,
                    PRIMARY KEY(round_id, hole));
                CREATE TABLE IF NOT EXISTS ss_shots (
                    round_id TEXT NOT NULL, hole INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    data_json TEXT NOT NULL, PRIMARY KEY(round_id, hole, sequence));
            """)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, key, default=None):
        with self.connection() as con:
            row = con.execute("SELECT value_json FROM ss_meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value, con=None):
        if con is None:
            with self.connection() as local:
                self.set(key, value, local)
        else:
            con.execute("INSERT OR REPLACE INTO ss_meta VALUES (?,?)", (key, json.dumps(value)))

    def bind_account(self, email):
        account = digest(email.strip().casefold().encode())
        existing = self.get("account_fingerprint")
        if existing and existing != account:
            raise SyncError("This local Shot Scope archive belongs to a different account; use separate local storage.")
        self.set("account_fingerprint", account)

    def known(self):
        with self.connection() as con:
            return {row[0] for row in con.execute("SELECT round_id FROM ss_rounds")}

    def archive(self, slim, detail):
        identity = round_id(slim.get("roundID"))
        raw = json_bytes({"slim": slim, "detail": detail})
        sha = digest(raw)
        path = self.root / "raw" / identity / f"{sha}.json"
        if not path.exists():
            atomic_write(path, raw)
        return path, sha

    def ingest(self, slim, detail, distance_unit):
        # Archive before parsing, so an unsupported schema remains inspectable locally.
        path, source_hash = self.archive(slim, detail)
        normalized = normalize(slim, detail, distance_unit)
        revision = digest(json_bytes({"source": source_hash, "parser": VERSION, "units": distance_unit}))
        identity = normalized["round"]["round_id"]
        normalized["round"]["revision"] = revision
        normalized["round"]["source_hash"] = source_hash
        with self.connection() as con:
            previous = con.execute("SELECT revision FROM ss_rounds WHERE round_id=?", (identity,)).fetchone()
            if previous and previous[0] == revision:
                return False
            con.execute("INSERT OR REPLACE INTO ss_rounds VALUES (?,?,?,?,?,?)", (
                identity, revision, source_hash, str(path), json.dumps(normalized), now()))
            con.execute("DELETE FROM ss_holes WHERE round_id=?", (identity,))
            con.execute("DELETE FROM ss_shots WHERE round_id=?", (identity,))
            con.executemany("INSERT INTO ss_holes VALUES (?,?,?)", [
                (identity, hole["hole"], json.dumps(hole)) for hole in normalized["holes"]])
            con.executemany("INSERT INTO ss_shots VALUES (?,?,?,?)", [
                (identity, shot["hole"], shot["sequence"], json.dumps(shot)) for shot in normalized["shots"]])
            self.set("publication_pending", True, con)
        return True

    def all(self):
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM ss_rounds ORDER BY round_id")]

    def status(self):
        rows = self.all()
        return {"schema_version": VERSION, "rounds_stored": len(rows),
                "shots_stored": sum(len(json.loads(row["normalized_json"])["shots"]) for row in rows),
                "last_run": self.get("last_run"), "last_successful_sync": self.get("last_successful_sync"),
                "last_error": self.get("last_error"), "failed_rounds": self.get("failed_rounds", []),
                "not_in_latest_listing": self.get("not_in_latest_listing", []),
                "publication_pending": self.get("publication_pending", False),
                "last_local_publication": self.get("last_local_publication"),
                "publication_error": self.get("publication_error"),
                "cloud_sync_status": "unverified"}


def csv_data(rows: list[dict], fallback_fields: list[str]) -> bytes:
    fields = list(dict.fromkeys(fallback_fields + [key for row in rows for key in row]))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            # Prevent formula execution from provider-entered course/club labels.
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return output.getvalue().encode("utf-8-sig")


def publish(store: Store, output: Path | None) -> bool:
    """Manifest hashes detect partially cloud-synced generations. No cloud claims."""
    if not store.get("publication_pending", False):
        return True
    if output is None or not output.is_dir():
        store.set("publication_error", "Shot Scope output folder unavailable; local records retained.")
        return False
    a, b = output.resolve(), store.root.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise SyncError("Shot Scope output folder must not overlap local application storage.")
    if (output / "all_shots.csv").exists() or (output / "sessions.csv").exists():
        raise SyncError("Destination resembles Square outputs; select a separate ShotScope folder.")
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SyncError("Cannot verify destination manifest ownership; existing files retained.") from None
        if not isinstance(existing, dict) or not str(existing.get("schema_version", "")).startswith("shotscope-"):
            raise SyncError("Destination manifest belongs to another app; existing files retained.")
    rows = store.all()
    normalized = [json.loads(row["normalized_json"]) for row in rows]
    files = {
        "rounds.csv": csv_data([n["round"] for n in normalized], ["round_id"]),
        "holes.csv": csv_data([h for n in normalized for h in n["holes"]], ["round_id", "hole"]),
        "shots.csv": csv_data([s for n in normalized for s in n["shots"]], ["round_id", "hole", "sequence"]),
        "quality_warnings.csv": csv_data([w for n in normalized for w in n["warnings"]], ["round_id", "hole", "sequence", "warning"]),
        "README.md": ("# Shot Scope archive\n\nUnofficial dashboard import. Read manifest.json and verify file hashes before combining files.\n"
                      "Missing values are unknown, not zero. Provider distance units are only normalized after local verification.\n"
                      "gps_displacement_m is separately calculated from endpoints, not carry. GPS accuracy is not independently verified.\n"
                      "No strokes-gained benchmarks, hole putt counts or penalty counts are invented. Source timestamps retain their original representation.\n"
                      "Round edits replace current rows; per-shot sequence is revision-local, not a permanent physical-shot ID.\n"
                      "Raw files contain reserialized provider JSON, not byte-exact HTTP responses or auth data. Text CSV cells may be escaped to prevent formulas.\n"
                      "Local write success does not prove cloud upload. Square exports are in a different folder.\n").encode(),
    }
    for row, item in zip(rows, normalized):
        identity = row["round_id"]
        files[f"rounds/{identity}.json"] = json_bytes(item)
        files[f"raw/{identity}/{row['source_hash']}.json"] = Path(row["source_path"]).read_bytes()
    exported = now()
    manifest = {"schema_version": VERSION, "export_id": uuid.uuid4().hex, "exported_at": exported,
                "round_count": len(rows), "shot_count": sum(len(n["shots"]) for n in normalized),
                "last_successful_sync": store.get("last_successful_sync"),
                "last_run": store.get("last_run"), "last_error": store.get("last_error"),
                "sync_scope": store.get("sync_scope"), "failed_rounds": store.get("failed_rounds", []),
                "not_in_latest_listing": store.get("not_in_latest_listing", []),
                "cloud_sync_status": "unverified", "files": {name: digest(data) for name, data in files.items()}}
    try:
        for name, data in files.items():
            atomic_write(output / name, data)
        atomic_write(output / "manifest.json", json_bytes(manifest))
    except OSError:
        store.set("publication_error", "Local Drive publication failed; retry with the publish command.")
        return False
    store.set("publication_pending", False)
    store.set("publication_error", None)
    store.set("last_local_publication", exported)
    return True


def sync(store: Store, client: DashboardClient, listing: dict, output: Path | None,
         distance_unit="unknown", recent=20, full=False, selected_id=None):
    items = listing.get("rounds")
    if not isinstance(items, list):
        raise SchemaError("Round-list schema changed.")
    known = store.known()
    ordered = sorted(items, key=lambda item: str(item.get("startedDate") or ""), reverse=True)
    available = {round_id(item.get("roundID")) for item in ordered}
    if selected_id is not None and round_id(selected_id) not in available:
        raise SyncError("Requested round is not in this account's returned round list.")
    store.set("last_run", now())
    store.set("sync_scope", {"full": full, "recent_window": recent, "round_id": selected_id})
    store.set("not_in_latest_listing", sorted(known - available))
    # Nothing is deleted merely because a list omits it; it might be filtered/paged.
    selected = [item for index, item in enumerate(ordered)
                if (selected_id is not None and round_id(item["roundID"]) == str(selected_id))
                or (selected_id is None and (full or index < recent or round_id(item["roundID"]) not in known))]
    failures, changed, checked = [], 0, 0
    for slim in selected:
        identity = round_id(slim["roundID"])
        try:
            detail = client.detail(identity)
            changed += store.ingest(slim, detail, distance_unit)
            checked += 1
        except SchemaError:
            failures.append({"round_id": identity, "error": "unsupported_schema; inspect locally archived JSON"})
        except SyncError as exc:
            failures.append({"round_id": identity, "error": str(exc)})
            break  # Stop on auth/network/rate errors rather than hammering remaining rounds.
    store.set("failed_rounds", failures)
    store.set("last_error", "One or more rounds could not be refreshed." if failures else None)
    if not failures:
        store.set("last_successful_sync", now())
    store.set("publication_pending", True)  # Publish current sync status even with no new shots.
    published = publish(store, output)
    return {"ok": not failures, "rounds_listed": len(items), "rounds_checked": checked,
            "rounds_changed": changed, "failed_rounds": failures,
            "drive_status": "written_to_sync_folder" if published else "pending",
            "cloud_sync_status": "unverified"}


def vault():
    if os.name != "nt":
        raise SyncError("Credential storage is implemented for Windows only; no plaintext fallback is allowed.")
    try:
        # Explicit Windows backend; never permit keyrings.alt plaintext fallback.
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    except Exception:
        raise SyncError("Windows Credential Manager unavailable; install this project with pip install -e .") from None


def default_data_dir() -> Path:
    """Dedicated per-user storage, independent of Square's LOCAL_DATA_DIR."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ShotScopeConnector"
    return Path.home() / ".local" / "share" / "ShotScopeConnector"


def initialize_config(path: Path) -> None:
    """Create this project's own configuration; never overwrite existing secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Local-only configuration. Never commit this file.\n"
        f'SHOTSCOPE_DATA_DIR="{default_data_dir().as_posix()}"\n'
        "SHOTSCOPE_EMAIL=\n"
        "SHOTSCOPE_OUTPUT_DIR=\n"
        "SHOTSCOPE_DISTANCE_UNIT=unknown\n"
    )
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(content)
    except FileExistsError:
        raise SyncError("Config already exists; edit it locally. No settings were overwritten.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Absolute .env path recommended for scheduled jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create independent configuration without overwriting any existing file")
    sub.add_parser("login", help="Verify login, then save password in Windows Credential Manager")
    sub.add_parser("logout", help="Remove stored Shot Scope password; keep golf data")
    probe = sub.add_parser("probe", help="Fetch one round and show field completeness; does not import/export it")
    probe.add_argument("--round-id")
    run = sub.add_parser("sync", help="Import new/recent rounds and publish; suitable for Task Scheduler")
    run.add_argument("--all", action="store_true", help="Re-fetch every returned round, including old edits")
    run.add_argument("--round-id")
    run.add_argument("--recent", type=int, default=20)
    sub.add_parser("publish", help="Retry Drive publication without contacting Shot Scope")
    sub.add_parser("status", help="Show only operational status, not credentials or raw GPS")
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            initialize_config(Path(args.env).expanduser())
            print("Created local .env. Set SHOTSCOPE_EMAIL and SHOTSCOPE_OUTPUT_DIR, then run login.")
            return 0
        except SyncError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
    load_dotenv(args.env, override=False)
    local = os.environ.get("SHOTSCOPE_DATA_DIR", "").strip() or str(default_data_dir())
    email = os.environ.get("SHOTSCOPE_EMAIL", "").strip()
    units = os.environ.get("SHOTSCOPE_DISTANCE_UNIT", "unknown").strip()
    destination = os.environ.get("SHOTSCOPE_OUTPUT_DIR", "").strip()
    output = Path(destination).expanduser() if destination else None
    if units not in ("unknown", "m", "yd"):
        parser.error("SHOTSCOPE_DISTANCE_UNIT must be unknown, m or yd.")
    if args.command == "sync" and not 1 <= args.recent <= 1000:
        parser.error("--recent must be between 1 and 1000.")
    root = Path(local).expanduser()
    if output:
        a, b = output.resolve(), root.resolve()
        if a == b or a in b.parents or b in a.parents:
            parser.error("SHOTSCOPE_DATA_DIR and SHOTSCOPE_OUTPUT_DIR must not overlap.")
    client = None
    try:
        with process_lock(root / "sync.lock"):
            store = Store(root)
            if args.command == "status":
                print(json.dumps(store.status(), indent=2))
                return 0
            if args.command == "publish":
                store.set("publication_pending", True)
                result = publish(store, output)
                print(json.dumps({"ok": result, "drive_status": "written_to_sync_folder" if result else "pending"}))
                return 0 if result else 2
            if not email:
                raise SyncError("Set SHOTSCOPE_EMAIL in your local .env; never put your password there.")
            keyring = vault()
            if args.command == "logout":
                keyring.delete_password(KEYRING_SERVICE, email)
                print("Shot Scope saved password removed. Archived golf data was retained.")
                return 0
            # Retry old Drive jobs even if today's login/network attempt fails later.
            if args.command == "sync":
                publish(store, output)
                store.set("last_run", now())
            password = getpass.getpass("Shot Scope password (hidden): ") if args.command == "login" else keyring.get_password(KEYRING_SERVICE, email)
            if not password:
                raise AuthenticationError("No saved password. Run the login command interactively on the mini PC first.")
            client = DashboardClient()
            listing = client.login(email, password)
            store.bind_account(email)  # Only bind storage after a successful account login.
            store.set("last_error", None)
            if args.command == "login":
                keyring.set_password(KEYRING_SERVICE, email, password)
                print(json.dumps({"ok": True, "rounds_listed": len(listing["rounds"]), "credentials": "saved_in_windows_credential_manager"}))
                return 0
            del password
            if args.command == "probe":
                ordered = sorted(listing["rounds"], key=lambda item: str(item.get("startedDate") or ""), reverse=True)
                if args.round_id:
                    ordered = [item for item in ordered if round_id(item["roundID"]) == round_id(args.round_id)]
                if not ordered:
                    print(json.dumps({"ok": False, "status": "no_matching_round", "message": "Upload a recorded round first; no CSV export is required."}))
                    return 2
                slim = ordered[0]
                detail = client.detail(round_id(slim["roundID"]))
                path, _ = store.archive(slim, detail)
                parsed = normalize(slim, detail, units)
                evidence = bool(parsed["shots"] and parsed["round"]["shots_with_complete_gps"])
                print(json.dumps({"ok": evidence, "round_id": parsed["round"]["round_id"],
                                  "holes": len(parsed["holes"]), "shots": len(parsed["shots"]),
                                  "shots_with_complete_gps": parsed["round"]["shots_with_complete_gps"],
                                  "shots_with_club": sum(s["club_original"] is not None for s in parsed["shots"]),
                                  "holes_with_pin": sum(h["pin_lat"] is not None for h in parsed["holes"]),
                                  "provider_distance_unit": units, "warning_count": len(parsed["warnings"]),
                                  "raw_saved_locally": True,
                                  "message": "Compare the local raw data with the app before configuring units and scheduling."}, indent=2))
                return 0 if evidence else 2
            result = sync(store, client, listing, output, units, args.recent, args.all, args.round_id)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] and result["drive_status"] != "pending" else 2
    except SyncError as exc:
        # All SyncError messages originate from fixed, secret-free diagnostics above.
        if 'store' in locals():
            store.set("last_error", str(exc))
            if args.command == "sync":
                store.set("publication_pending", True)
                try:
                    publish(store, output)
                except Exception:
                    pass
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except Exception as exc:
        # Third-party exception messages can include secrets, URLs or account names.
        diagnostic = f"Unexpected {type(exc).__name__}; inspect configuration locally. No credentials were logged."
        if 'store' in locals():
            try:
                store.set("last_error", diagnostic)
            except Exception:
                pass
        print(json.dumps({"ok": False, "error": diagnostic}))
        return 2
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
