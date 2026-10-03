from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import URLError
from urllib.request import urlopen

from .bench import DEFAULT_SLO

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LEDGER_DB", ROOT / "data" / "ledger.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
EVALUATIONS_PATH = ROOT / "data" / "evaluations.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, currency TEXT NOT NULL, balance_minor INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, from_account TEXT NOT NULL, to_account TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL, posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_id TEXT NOT NULL REFERENCES transfers(id), account_id TEXT NOT NULL REFERENCES accounts(id), amount_minor INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS entries_account ON entries(account_id, id);
"""
LOCK = threading.RLock()
EVALUATION_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()
EVALUATION_JOBS = {}
try:
    stored_evaluations = json.loads(EVALUATIONS_PATH.read_text())
    EVALUATIONS = stored_evaluations if isinstance(stored_evaluations, list) else stored_evaluations.get("evaluations", [])
except (OSError, json.JSONDecodeError):
    EVALUATIONS = []


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


@contextmanager
def connection():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


with connection() as db:
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
    if "balance_minor" not in columns:
        db.execute("ALTER TABLE accounts ADD COLUMN balance_minor INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE accounts SET balance_minor=COALESCE((SELECT SUM(amount_minor) FROM entries WHERE entries.account_id=accounts.id),0)")
    # Balanced synthetic opening entries make the sandbox usable immediately.
    db.execute("INSERT OR IGNORE INTO accounts(id,currency) VALUES('demo-alice','USD')")
    db.execute("INSERT OR IGNORE INTO accounts(id,currency) VALUES('demo-bob','USD')")
    db.execute("INSERT OR IGNORE INTO accounts(id,currency) VALUES('ledgerlab-funder','USD')")
    db.execute("INSERT OR IGNORE INTO accounts(id,currency) VALUES('demo-clearing','USD')")
    openings = [
        ("ledgerlab-opening-funder-v1", "demo-clearing", "ledgerlab-funder", 1_000_000_000_000_000),
        ("ledgerlab-opening-alice-v1", "demo-clearing", "demo-alice", 250_000),
        ("ledgerlab-opening-bob-v1", "demo-clearing", "demo-bob", 250_000),
    ]
    for key, source, target, amount in openings:
        if not db.execute("SELECT 1 FROM transfers WHERE idempotency_key=?", (key,)).fetchone():
            transfer_id = str(uuid.uuid4())
            db.execute("INSERT INTO transfers(id,idempotency_key,request_hash,from_account,to_account,amount_minor,currency) VALUES(?,?,?,?,?,?,?)", (transfer_id, key, "synthetic-opening-v1", source, target, amount, "USD"))
            db.executemany("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?,?,?)", [(transfer_id, source, -amount), (transfer_id, target, amount)])
            db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, source))
            db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?", (amount, target))


def balance(db, account_id):
    row = db.execute("SELECT balance_minor AS balance FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["balance"] if row else 0


class Handler(BaseHTTPRequestHandler):
    server_version = "LedgerLab/0.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, data):
        payload = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 65536:
            raise ValueError("request body too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/" or path == "/index.html":
            target = ROOT / "web" / "index.html"
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/evaluations/engines":
            self.send_json(200, {"engines": engine_catalog()})
            return
        if path == "/evaluations":
            self.send_json(200, {"evaluations": list(EVALUATIONS)})
            return
        if path.startswith("/evaluations/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = EVALUATION_JOBS.get(job_id)
            if job is None:
                self.send_json(404, {"error": {"code": "job_not_found", "message": "Evaluation job not found"}})
            else:
                self.send_json(200, dict(job))
            return
        try:
            with connection() as db:
                if path == "/health":
                    self.send_json(200, {"status": "ok", "implementation": "python", "protocol_version": 1})
                elif path == "/metrics":
                    row = db.execute("SELECT COUNT(*) AS entries, COALESCE(SUM(amount_minor),0) AS net_minor FROM entries").fetchone()
                    bad = db.execute("SELECT COUNT(*) AS count FROM (SELECT transfer_id FROM entries GROUP BY transfer_id HAVING COUNT(*) != 2 OR SUM(amount_minor) != 0)").fetchone()["count"]
                    balance_mismatches = db.execute("SELECT COUNT(*) FROM accounts a WHERE a.balance_minor != COALESCE((SELECT SUM(amount_minor) FROM entries e WHERE e.account_id=a.id),0)").fetchone()[0]
                    transfers = db.execute("SELECT COUNT(*) AS count FROM transfers").fetchone()["count"]
                    self.send_json(200, {"entry_count": row["entries"], "net_minor": row["net_minor"], "transfer_count": transfers, "invalid_transfer_groups": bad, "balance_mismatch_accounts": balance_mismatches})
                elif path == "/accounts":
                    rows = db.execute("SELECT id, currency, created_at FROM accounts ORDER BY id").fetchall()
                    self.send_json(200, {"accounts": [dict(r) | {"balance_minor": balance(db, r["id"])} for r in rows]})
                elif path.startswith("/accounts/"):
                    account_id = path.rsplit("/", 1)[-1]
                    row = db.execute("SELECT id, currency, created_at FROM accounts WHERE id=?", (account_id,)).fetchone()
                    if not row:
                        self.send_json(404, {"error": {"code": "account_not_found", "message": "Account not found"}})
                    else:
                        self.send_json(200, dict(row) | {"balance_minor": balance(db, account_id)})
                elif path == "/ledger":
                    query = parse_qs(parsed.query)
                    account_id = query.get("account_id", [None])[0]
                    limit = max(1, min(int(query.get("limit", ["100"])[0]), 1000))
                    rows = db.execute("SELECT id, transfer_id, account_id, amount_minor, created_at FROM entries WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit)).fetchall() if account_id else db.execute("SELECT id, transfer_id, account_id, amount_minor, created_at FROM entries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                    self.send_json(200, {"entries": [dict(r) for r in rows]})
                else:
                    self.send_json(404, {"error": {"code": "not_found", "message": "Route not found"}})
        except (ValueError, sqlite3.Error) as exc:
            self.send_json(400, {"error": {"code": "invalid_request", "message": str(exc)}})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self.body()
            if path.startswith("/evaluations/"):
                parts = path.strip("/").split("/")
                engine = parts[1] if len(parts) > 1 else ""
                action = parts[2] if len(parts) > 2 else "run"
                available = {item["id"] for item in engine_catalog() if item["available"]}
                if engine not in available:
                    self.send_json(501, {"error": {"code": "engine_unavailable", "message": f"{engine} engine is not implemented yet"}})
                    return
                try:
                    slo = parse_slo(body.get("slo"))
                    profile = body.get("profile", "session")
                    if profile not in ("session", "raw"):
                        raise ValueError("profile must be 'session' or 'raw'")
                    if action == "sweep":
                        levels = body.get("levels", [1, 2, 4, 8, 16, 32, 64])
                        seconds = body.get("seconds", 5)
                        if not isinstance(levels, list) or not 1 <= len(levels) <= 10 or any(type(level) is not int or not 1 <= level <= 1024 for level in levels):
                            raise ValueError("levels must be a list of 1-10 integers between 1 and 1024")
                        if type(seconds) not in (int, float) or not 1 <= seconds <= 30:
                            raise ValueError("seconds must be between 1 and 30")
                        config = {"kind": "sweep", "levels": sorted(set(levels)), "seconds": float(seconds), "profile": profile, "slo": slo}
                    elif action == "capacity":
                        start = body.get("start", 64)
                        max_users = body.get("max_users", 2048 if profile == "session" else 1024)
                        seconds = body.get("seconds", 5)
                        if type(start) is not int or not 1 <= start <= 65536:
                            raise ValueError("start must be an integer between 1 and 65536")
                        if type(max_users) is not int or not start <= max_users <= 262144:
                            raise ValueError("max_users must be an integer between start and 262144")
                        if type(seconds) not in (int, float) or not 1 <= seconds <= 30:
                            raise ValueError("seconds must be between 1 and 30")
                        confirm_seconds = body.get("confirm_seconds", max(30.0, 5 * float(seconds)))
                        if type(confirm_seconds) not in (int, float) or not 0 <= confirm_seconds <= 600:
                            raise ValueError("confirm_seconds must be between 0 and 600")
                        config = {"kind": "capacity", "profile": profile, "seconds": float(seconds), "start": start, "max_users": max_users, "confirm_seconds": float(confirm_seconds), "slo": slo}
                    elif action == "run":
                        users, seconds = body.get("users", 8), body.get("seconds", 5)
                        if type(users) is not int or not 1 <= users <= 256 or type(seconds) is not int or not 1 <= seconds <= 30:
                            raise ValueError("users must be 1-256 and seconds 1-30")
                        config = {"kind": "run", "users": users, "seconds": seconds, "profile": profile, "slo": slo}
                    else:
                        self.send_json(404, {"error": {"code": "not_found", "message": "Unknown evaluation action"}})
                        return
                except ValueError as exc:
                    self.send_json(400, {"error": {"code": "invalid_request", "message": str(exc)}})
                    return
                if not EVALUATION_LOCK.acquire(blocking=False):
                    self.send_json(409, {"error": {"code": "evaluation_running", "message": "Another evaluation is running"}})
                    return
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    EVALUATION_JOBS[job_id] = {"job_id": job_id, "engine": engine, "kind": config["kind"], "status": "running", "phase": "Queued", "progress": 1, "started_at": datetime.now(timezone.utc).isoformat(), "result": None, "error": None}
                threading.Thread(target=run_evaluation_job, args=(job_id, engine, config), daemon=True).start()
                self.send_json(202, {"job_id": job_id, "status": "running"})
                return
            if path == "/bench/memory":
                users, seconds = body.get("users", 8), body.get("seconds", 5)
                if type(users) is not int or not 1 <= users <= 128 or type(seconds) not in (int, float) or not 0 < seconds <= 30:
                    self.send_json(400, {"error": {"code": "invalid_request", "message": "users must be 1–128 and seconds must be in (0, 30]"}})
                    return
                self.send_json(200, memory_core_benchmark(users, float(seconds)))
                return
            if path == "/accounts":
                account_id = body.get("id")
                currency = body.get("currency", "USD")
                if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 80 or not isinstance(currency, str) or len(currency) != 3:
                    self.send_json(400, {"error": {"code": "invalid_request", "message": "id and three-character currency are required"}})
                    return
                with connection() as db:
                    db.execute("INSERT INTO accounts(id,currency) VALUES(?,?)", (account_id, currency.upper()))
                self.send_json(201, {"id": account_id, "currency": currency.upper(), "balance_minor": 0})
                return
            if path != "/transfers":
                self.send_json(404, {"error": {"code": "not_found", "message": "Route not found"}})
                return
            key = body.get("idempotency_key") or self.headers.get("Idempotency-Key")
            source, target, amount, currency = body.get("from_account"), body.get("to_account"), body.get("amount_minor"), body.get("currency", "USD")
            if not isinstance(key, str) or not key or len(key) > 200 or not isinstance(source, str) or not isinstance(target, str) or source == target or type(amount) is not int or amount <= 0 or amount > 2**63 - 1 or not isinstance(currency, str) or len(currency) != 3:
                self.send_json(400, {"error": {"code": "invalid_request", "message": "A key, distinct accounts, positive integer amount_minor, and three-character currency are required"}})
                return
            currency = currency.upper()
            fingerprint = json.dumps([source, target, amount, currency], separators=(",", ":"))
            with LOCK, connection() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT id, request_hash, from_account, to_account, amount_minor, currency FROM transfers WHERE idempotency_key=?", (key,)).fetchone()
                if prior:
                    if prior["request_hash"] != fingerprint:
                        self.send_json(409, {"error": {"code": "idempotency_conflict", "message": "Key was used for a different transfer"}})
                        return
                    self.send_json(200, {"id": prior["id"], "status": "posted", "idempotent_replay": True, "from_balance_minor": balance(db, source), "to_balance_minor": balance(db, target)})
                    return
                accounts = db.execute("SELECT id,currency FROM accounts WHERE id IN (?,?)", (source, target)).fetchall()
                if len(accounts) != 2:
                    self.send_json(404, {"error": {"code": "account_not_found", "message": "Source or destination account not found"}})
                    return
                if any(a["currency"] != currency for a in accounts):
                    self.send_json(409, {"error": {"code": "currency_mismatch", "message": "Transfer currency must match both accounts"}})
                    return
                if balance(db, source) < amount:
                    self.send_json(409, {"error": {"code": "insufficient_funds", "message": "Source account has insufficient funds"}})
                    return
                transfer_id = str(uuid.uuid4())
                db.execute("INSERT INTO transfers(id,idempotency_key,request_hash,from_account,to_account,amount_minor,currency) VALUES(?,?,?,?,?,?,?)", (transfer_id, key, fingerprint, source, target, amount, currency))
                db.executemany("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?,?,?)", [(transfer_id, source, -amount), (transfer_id, target, amount)])
                db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, source))
                db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?", (amount, target))
                self.send_json(201, {"id": transfer_id, "status": "posted", "idempotent_replay": False, "from_balance_minor": balance(db, source), "to_balance_minor": balance(db, target)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"code": "invalid_request", "message": str(exc)}})
        except sqlite3.IntegrityError as exc:
            self.send_json(409, {"error": {"code": "conflict", "message": str(exc)}})
        except sqlite3.Error as exc:
            self.send_json(500, {"error": {"code": "storage_error", "message": str(exc)}})


def memory_core_benchmark(users, seconds):
    """Measure balanced transfer arithmetic without HTTP or SQLite."""
    from concurrent.futures import ThreadPoolExecutor
    deadline = time.perf_counter() + seconds
    def worker(_):
        left = right = 10**12
        count = 0
        while time.perf_counter() < deadline:
            if count & 1:
                if right < 1:
                    break
                right -= 1
                left += 1
            else:
                if left < 1:
                    break
                left -= 1
                right += 1
            count += 1
        return count, left, right
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=users) as pool:
        rows = list(pool.map(worker, range(users)))
    elapsed = time.perf_counter() - started
    successes = sum(row[0] for row in rows)
    valid = all(left + right == 2 * 10**12 for _, left, right in rows)
    return {"mode": "in_memory_core", "users": users, "seconds": round(elapsed, 3), "successful_transfers": successes,
            "throughput_ops_per_second": round(successes / elapsed, 2), "state_model": "independent thread-local account pair per worker",
            "http_or_sqlite_included": False, "invariants": {"all_pairs_conserved": valid, "passed": valid}}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def update_job(job_id, **changes):
    with JOBS_LOCK:
        if job_id in EVALUATION_JOBS:
            EVALUATION_JOBS[job_id].update(changes)


def has_c_libraries():
    if not shutil.which("pkg-config"):
        return False
    try:
        return subprocess.run(["pkg-config", "--exists", "libevent", "json-c", "sqlite3"], check=False).returncode == 0
    except OSError:
        return False


def engine_catalog():
    c_ready = bool(shutil.which("cc") and has_c_libraries())
    return [
        {"id": "python", "name": "Python", "available": True, "note": "Threaded HTTP + SQLite WAL"},
        {"id": "rust", "name": "Rust", "available": bool(shutil.which("cargo")), "note": "tiny_http + rusqlite + SQLite"},
        {"id": "c", "name": "C", "available": c_ready, "note": "libevent + SQLite + JSON-C"},
        {"id": "cobol", "name": "COBOL", "available": bool(shutil.which("gcobol") and c_ready), "note": "COBOL ledger core + shared C HTTP/SQLite adapter"},
    ]


def prepare_engine(engine, temp_dir, progress):
    """Build the selected implementation and return its server command and runtime label."""
    implementations = ROOT / "implementations"
    target = Path(temp_dir)
    if engine == "python":
        return [sys.executable, "-m", "ledgerlab"], f"Python {sys.version.split()[0]}"
    if engine == "rust":
        project = implementations / "rust"
        progress("Building Rust release server", 5)
        built = subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path", str(project / "Cargo.toml")], cwd=ROOT, capture_output=True, text=True, timeout=300)
        if built.returncode:
            raise RuntimeError((built.stderr or built.stdout)[-2000:])
        return [str(project / "target" / "release" / "ledgerlab-rust")], subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip()
    if engine == "c":
        progress("Compiling C server", 5)
        libs = subprocess.run(["pkg-config", "--cflags", "--libs", "libevent", "json-c", "sqlite3"], capture_output=True, text=True, check=True).stdout.split()
        binary = target / "ledgerlab-c"
        command = ["cc", "-O2", "-std=c11", "-pthread", '-DLEDGER_ENGINE="c"', str(implementations / "c" / "server.c"), "-o", str(binary), *libs]
        built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise RuntimeError((built.stderr or built.stdout)[-2000:])
        version = subprocess.run(["cc", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
        return [str(binary)], version
    if engine == "cobol":
        progress("Compiling COBOL core and C transport", 5)
        libs = subprocess.run(["pkg-config", "--cflags", "--libs", "libevent", "json-c", "sqlite3"], capture_output=True, text=True, check=True).stdout.split()
        cobol_dir = implementations / "cobol"
        binary = target / "ledgerlab-cobol"
        command = ["gcobol", "-nomain", "-O2", "-pthread", "-DCOBOL_CORE", '-DLEDGER_ENGINE="cobol"', str(implementations / "c" / "server.c"), str(cobol_dir / "ledger_core.cob"), "-o", str(binary), *libs]
        built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise RuntimeError((built.stderr or built.stdout)[-2000:])
        version = subprocess.run(["gcobol", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
        return [str(binary)], version + " (COBOL core; C HTTP/SQLite adapter)"
    raise ValueError(f"Unknown engine: {engine}")


def parse_slo(raw):
    """Validate and normalise SLO thresholds; defaults follow common API targets."""
    raw = raw or {}
    try:
        thresholds = {
            "p95_ms": float(raw.get("p95_ms", DEFAULT_SLO["p95_ms"])),
            "p99_ms": float(raw.get("p99_ms", DEFAULT_SLO["p99_ms"])),
            "error_rate": float(raw.get("error_rate", DEFAULT_SLO["error_rate"])),
        }
    except (TypeError, ValueError):
        raise ValueError("slo thresholds must be numbers")
    if not 0 < thresholds["p95_ms"] <= 60_000 or not 0 < thresholds["p99_ms"] <= 120_000 or not 0 <= thresholds["error_rate"] <= 1:
        raise ValueError("slo thresholds are out of range")
    return thresholds


def run_evaluation_job(job_id, engine, config):
    try:
        progress = lambda phase, value: update_job(job_id, phase=phase, progress=value)
        if config["kind"] == "sweep":
            result = run_sweep(engine, config["levels"], config["seconds"], config["profile"], config["slo"], progress)
        elif config["kind"] == "capacity":
            result = run_capacity(engine, config["profile"], config["seconds"], config["start"], config["max_users"], config["slo"], config.get("confirm_seconds", 30.0), progress)
        else:
            result = run_evaluation(engine, config["users"], config["seconds"], config["profile"], config["slo"], progress)
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        EVALUATIONS.insert(0, result)
        del EVALUATIONS[20:]
        temp_path = EVALUATIONS_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(EVALUATIONS, indent=2))
        temp_path.replace(EVALUATIONS_PATH)
        update_job(job_id, status="completed", phase="Complete", progress=100, result=result)
    except Exception as exc:
        update_job(job_id, status="failed", phase="Failed", progress=100, error=str(exc))
    finally:
        EVALUATION_LOCK.release()


@contextmanager
def running_engine(engine, progress=None):
    """Build, start, health-check, and always tear down an isolated engine process."""
    def report(phase, value):
        if progress:
            progress(phase, value)

    evaluation_started = time.perf_counter()
    report("Preparing isolated database", 3)
    with tempfile.TemporaryDirectory(prefix=f"ledgerlab-{engine}-") as temp_dir:
        build_started = time.perf_counter()
        command, engine_runtime = prepare_engine(engine, temp_dir, report)
        build_seconds = round(time.perf_counter() - build_started, 3)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        db_path = str(Path(temp_dir) / "ledger.sqlite3")
        env = os.environ.copy() | {"LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": str(port), "LEDGER_DB": db_path}
        server_started = time.perf_counter()
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        base_url = f"http://127.0.0.1:{port}"
        ready = False
        try:
            report("Starting engine server", 7)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with urlopen(base_url + "/health", timeout=.3) as response:
                        ready = response.status == 200
                    if ready:
                        break
                    report("Waiting for health check", min(13, 7 + (time.monotonic() - server_started)))
                except (URLError, TimeoutError, OSError):
                    report("Waiting for health check", min(13, 7 + (time.monotonic() - server_started)))
                    time.sleep(.05)
            if not ready:
                detail = server.stderr.read().decode(errors="replace") if server.poll() is not None else "startup timed out"
                raise RuntimeError(f"Engine failed to start: {detail[-1000:]}")
            startup_seconds = time.perf_counter() - server_started
            report("Checking protocol conformance", 15)
            checks = conformance_checks(base_url, engine)
            yield {
                "base_url": base_url, "env": env, "report": report, "checks": checks,
                "engine_runtime": engine_runtime, "build_seconds": build_seconds,
                "startup_seconds": startup_seconds, "evaluation_started": evaluation_started, "server": server,
            }
        finally:
            report("Stopping engine server", 96)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def process_metrics(pid):
    """CPU seconds and peak RSS for a live process, read from /proc (Linux)."""
    cpu_seconds, peak_rss_kb = None, None
    try:
        with open(f"/proc/{pid}/stat") as handle:
            tail = handle.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_seconds = (int(tail[11]) + int(tail[12])) / ticks
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    peak_rss_kb = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    return {"cpu_seconds": round(cpu_seconds, 3) if cpu_seconds is not None else None, "peak_rss_kb": peak_rss_kb}


def run_bench(base_url, env, profile, users, seconds, slo, engine, report, phase, low, high, warmup=0.0):
    """Run one load profile against a live engine and return its parsed report.

    Also records the load generator's own CPU/RSS so a report can show whether
    the server or the client was the bottleneck.
    """
    command = [
        sys.executable, "-m", "ledgerlab.bench", "--base-url", base_url, "--profile", profile,
        "--users", str(users), "--seconds", str(seconds), "--implementation", engine,
        "--slo-p95-ms", str(slo["p95_ms"]), "--slo-p99-ms", str(slo["p99_ms"]), "--slo-error-rate", str(slo["error_rate"]),
        "--warmup-seconds", str(warmup),
    ]
    runner = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    benchmark_started = time.monotonic()
    metrics = {"cpu_seconds": None, "peak_rss_kb": None}
    while runner.poll() is None:
        elapsed = time.monotonic() - benchmark_started
        report(phase, min(high, low + int((high - low) * elapsed / (seconds + warmup + 1))))
        sample = process_metrics(runner.pid)
        if sample["cpu_seconds"] is not None or sample["peak_rss_kb"] is not None:
            metrics = sample
        if elapsed > seconds + warmup + 90:
            runner.kill()
            raise TimeoutError("Benchmark process timed out")
        time.sleep(.25)
    # The runner is a zombie until reaped; /proc still holds its final counters.
    sample = process_metrics(runner.pid)
    if sample["cpu_seconds"] is not None or sample["peak_rss_kb"] is not None:
        metrics = sample
    stdout, stderr = runner.communicate()
    if runner.returncode != 0:
        raise RuntimeError(stderr[-1500:] or stdout[-1500:] or "benchmark failed")
    result = json.loads(stdout)
    result.setdefault("resources", {})["generator"] = metrics
    return result


def validate_ledger(result):
    invariant = result.get("invariant_check", {})
    if not all(invariant.get(key) for key in ("benchmark_accounts_conserved", "all_transfers_have_two_balanced_entries", "account_balances_match_journal")):
        raise RuntimeError(f"SQLite ledger invariant check failed: {invariant}")


def run_memory_core(base_url, users, seconds, progress=None, phase="Running in-memory transfer core", value=86):
    from .bench import request
    if progress:
        progress(phase, value)
    status, memory_core = request(base_url + "/bench/memory", "POST", {"users": users, "seconds": seconds})
    if status != 200 or not memory_core.get("invariants", {}).get("passed"):
        raise RuntimeError(f"in-memory core benchmark failed: HTTP {status} {memory_core}")
    return memory_core


def run_evaluation(engine, users, seconds, profile, slo, progress=None, warmup=2.0):
    """Single managed run: build, load once, check invariants, tear down."""
    with running_engine(engine, progress) as ctx:
        report = ctx["report"]
        report("Running realistic workload", 25)
        result = run_bench(ctx["base_url"], ctx["env"], profile, users, seconds, slo, engine, report, "Running realistic workload", 25, 80, warmup=warmup)
        validate_ledger(result)
        report("Running in-memory transfer core", 86)
        result["memory_core"] = run_memory_core(ctx["base_url"], min(128, users), seconds)
        report("Checking ledger invariants", 92)
        result.setdefault("resources", {})["engine"] = process_metrics(ctx["server"].pid)
        result.update({
            "kind": "run", "engine": engine, "engine_runtime": ctx["engine_runtime"],
            "build_seconds": ctx["build_seconds"], "startup_seconds": round(ctx["startup_seconds"], 3),
            "evaluation_setup_seconds": round(time.perf_counter() - ctx["evaluation_started"], 3),
            "conformance": {"passed": True, "checks": ctx["checks"]},
            "lifecycle": "started -> benchmarked -> stopped", "engine_pid": ctx["server"].pid,
        })
        return result


def run_sweep(engine, levels, seconds, profile, slo, progress=None):
    """Concurrency sweep that finds the highest level still meeting the SLO."""
    with running_engine(engine, progress) as ctx:
        report = ctx["report"]
        curve, capacity, first_failing = [], None, None
        gen = {"cpu_seconds": 0.0, "peak_rss_kb": 0}
        span = max(1, len(levels))
        for index, level in enumerate(levels):
            phase = f"Sweep level {level} clients ({index + 1}/{span})"
            low = 25 + int(60 * index / span)
            high = 25 + int(60 * (index + 1) / span)
            result = run_bench(ctx["base_url"], ctx["env"], profile, level, seconds, slo, engine, report, phase, low, high)
            validate_ledger(result)
            g = result.get("resources", {}).get("generator", {})
            gen["cpu_seconds"] += g.get("cpu_seconds") or 0.0
            gen["peak_rss_kb"] = max(gen["peak_rss_kb"], g.get("peak_rss_kb") or 0)
            curve.append(curve_entry(result, level))
            if first_failing is None:
                if result["slo"]["passed"]:
                    capacity = level
                else:
                    first_failing = level
        memory_core = run_memory_core(ctx["base_url"], min(128, max(levels)), seconds, report, "Running in-memory transfer core", 89)
        return {
            "kind": "sweep", "engine": engine, "profile": profile,
            "workload": {"profile": profile, "seconds_per_level": seconds, "levels": levels},
            "slo": slo, "curve": curve, "capacity_users": capacity, "first_failing_users": first_failing,
            "memory_core": memory_core, "engine_runtime": ctx["engine_runtime"],
            "build_seconds": ctx["build_seconds"], "startup_seconds": round(ctx["startup_seconds"], 3),
            "evaluation_setup_seconds": round(time.perf_counter() - ctx["evaluation_started"], 3),
            "conformance": {"passed": True, "checks": ctx["checks"]},
            "resources": {"engine": process_metrics(ctx["server"].pid), "generator": {"cpu_seconds": round(gen["cpu_seconds"], 2), "peak_rss_kb": gen["peak_rss_kb"]}},
            "lifecycle": "started -> swept -> stopped", "engine_pid": ctx["server"].pid,
            "note": "Capacity is the highest concurrency whose run met every SLO threshold; it is specific to this host and workload.",
        }



def curve_entry(result, level):
    return {
        "users": level, "throughput_rps": result["results"]["throughput_rps"],
        "writes_per_second": result["results"]["writes_per_second"],
        "latency_ms": result["results"]["latency_ms"], "error_rate": result["results"]["error_rate"],
        "successes": result["results"]["successes"], "errors": result["results"]["errors"], "slo": result["slo"],
    }


def run_capacity(engine, profile, seconds, start, max_users, slo, confirm_seconds=30.0, progress=None):
    """Adaptive capacity: warm up, double load until an SLO fails, refine the knee,
    then confirm the reported level with a longer soak run (stepping down on failure)."""
    with running_engine(engine, progress) as ctx:
        report = ctx["report"]
        planned = []
        level = start
        while level <= max_users:
            planned.append(level)
            level *= 2
        total_steps = len(planned) + 3
        tested, curve, capacity, first_failing = set(), [], None, None
        gen = {"cpu_seconds": 0.0, "peak_rss_kb": 0}

        def measure(level, index, note, secs=None, warmup=0.0, bounds=None):
            secs = seconds if secs is None else secs
            phase = f"{note} · {level} users ({index}/{total_steps})"
            if bounds:
                low, high = bounds
            else:
                low = 25 + int(58 * (index - 1) / total_steps)
                high = 25 + int(58 * index / total_steps)
            result = None
            try:
                result = run_bench(ctx["base_url"], ctx["env"], profile, level, secs, slo, engine, report, phase, low, high, warmup=warmup)
                validate_ledger(result)
                passed, entry = result["slo"]["passed"], curve_entry(result, level)
            except Exception as exc:
                # A level that cannot complete is recorded as a failure so the
                # curve and capacity are preserved instead of losing the sweep.
                passed, entry = False, {
                    "users": level, "throughput_rps": 0.0, "writes_per_second": 0.0,
                    "latency_ms": {"p50": None, "p95": None, "p99": None}, "error_rate": 1.0,
                    "successes": 0, "errors": 0, "failed_reason": str(exc)[:300],
                    "slo": {"thresholds": slo, "observed": {"p95_ms": None, "p99_ms": None, "error_rate": 1.0}, "passed": False, "failures": ["run_error"]},
                }
            if result is not None:
                g = result.get("resources", {}).get("generator", {})
                gen["cpu_seconds"] += g.get("cpu_seconds") or 0.0
                gen["peak_rss_kb"] = max(gen["peak_rss_kb"], g.get("peak_rss_kb") or 0)
            return passed, entry

        def record(level, index, note):
            passed, entry = measure(level, index, note)
            tested.add(level)
            curve.append(entry)
            return passed

        report("Warming up engine", 18)
        try:
            run_bench(ctx["base_url"], ctx["env"], profile, min(start, 256), seconds, slo, engine, report, "Warming up engine", 18, 22, warmup=0.0)
        except Exception:
            pass

        passed_levels = []
        for index, level in enumerate(planned, start=1):
            if record(level, index, "Doubling load"):
                capacity = level
                passed_levels.append(level)
            else:
                first_failing = level
                break
        if first_failing is not None and passed_levels:
            low, high = passed_levels[-1], first_failing
            for step in range(2):
                middle = (low + high) // 2
                if middle in tested or high - low <= 1:
                    break
                if record(middle, len(planned) + 1 + step, "Refining knee"):
                    low = middle
                    capacity = middle
                else:
                    high = middle
                    first_failing = middle

        # Confirmation/soak: hold the reported level for a longer window and step
        # down if it cannot sustain it — a short pass alone is not enough.
        confirmation = None
        if capacity is not None and confirm_seconds > seconds:
            level = capacity
            for attempt in range(2):
                passed, entry = measure(level, total_steps + attempt, f"Confirming {int(confirm_seconds)}s soak", secs=confirm_seconds, bounds=(80 + attempt * 7, 87 + attempt * 7))
                entry["confirmation_seconds"] = confirm_seconds
                entry["confirmed"] = passed
                confirmation = entry
                if passed:
                    capacity = level
                    break
                lower = max((item for item in passed_levels if item < level), default=None)
                if lower is None:
                    capacity = None
                    first_failing = first_failing or level
                    break
                level = lower

        curve.sort(key=lambda entry: entry["users"])
        memory_core = run_memory_core(ctx["base_url"], min(128, max(planned) if planned else start), seconds, report, "Running in-memory transfer core", 89)
        return {
            "kind": "capacity", "engine": engine, "profile": profile,
            "workload": {"profile": profile, "seconds_per_level": seconds, "start": start, "max_users": max_users, "confirm_seconds": confirm_seconds},
            "slo": slo, "curve": curve, "capacity_users": capacity, "first_failing_users": first_failing,
            "confirmation": confirmation,
            "memory_core": memory_core, "engine_runtime": ctx["engine_runtime"],
            "build_seconds": ctx["build_seconds"], "startup_seconds": round(ctx["startup_seconds"], 3),
            "evaluation_setup_seconds": round(time.perf_counter() - ctx["evaluation_started"], 3),
            "conformance": {"passed": True, "checks": ctx["checks"]},
            "resources": {"engine": process_metrics(ctx["server"].pid), "generator": {"cpu_seconds": round(gen["cpu_seconds"], 2), "peak_rss_kb": gen["peak_rss_kb"]}},
            "lifecycle": "started -> warmed -> capacity -> confirmed -> stopped", "engine_pid": ctx["server"].pid,
            "note": "Adaptive capacity: warm up, double concurrency until an SLO fails, refine the knee, then confirm with a longer soak run; specific to this host and workload.",
        }


def conformance_checks(base_url, engine):
    """Exercise shared ledger semantics before recording any performance result."""
    from .bench import request

    checks = {}
    status, health = request(base_url + "/health")
    if status != 200 or health.get("implementation") != engine:
        raise RuntimeError(f"health identity check failed: {health}")
    checks["health_identity"] = True
    status, accounts = request(base_url + "/accounts")
    if status != 200 or not {"demo-alice", "demo-bob", "ledgerlab-funder"}.issubset({a.get("id") for a in accounts.get("accounts", [])}):
        raise RuntimeError("account listing/seed check failed")
    status, account = request(base_url + "/accounts/demo-alice")
    if status != 200 or account.get("currency") != "USD":
        raise RuntimeError("single-account lookup check failed")
    status, journal = request(base_url + "/ledger?account_id=demo-alice&limit=5")
    if status != 200 or not any(entry.get("account_id") == "demo-alice" for entry in journal.get("entries", [])):
        raise RuntimeError("account ledger lookup check failed")
    checks["account_and_ledger_reads"] = True

    key = f"conformance-{uuid.uuid4().hex}"
    transfer = {"idempotency_key": key, "from_account": "demo-alice", "to_account": "demo-bob", "amount_minor": 7, "currency": "USD"}
    status, first = request(base_url + "/transfers", "POST", transfer)
    if status != 201:
        raise RuntimeError(f"initial posting failed: HTTP {status} {first}")
    status, replay = request(base_url + "/transfers", "POST", transfer)
    if status != 200 or replay.get("id") != first.get("id") or replay.get("idempotent_replay") is not True:
        raise RuntimeError(f"idempotent replay check failed: HTTP {status} {replay}")
    checks["idempotent_replay"] = True

    conflict = dict(transfer, amount_minor=8)
    status, _ = request(base_url + "/transfers", "POST", conflict)
    if status != 409:
        raise RuntimeError(f"idempotency conflict should be HTTP 409, got {status}")
    checks["idempotency_conflict"] = True

    status, account = request(base_url + "/accounts/demo-alice")
    if status != 200:
        raise RuntimeError("seed account lookup failed")
    insufficient = {"idempotency_key": f"conformance-{uuid.uuid4().hex}", "from_account": "demo-alice", "to_account": "demo-bob", "amount_minor": account["balance_minor"] + 1, "currency": "USD"}
    status, body = request(base_url + "/transfers", "POST", insufficient)
    if status != 409 or body.get("error", {}).get("code") != "insufficient_funds":
        raise RuntimeError(f"insufficient-funds check failed: HTTP {status} {body}")
    checks["insufficient_funds"] = True

    currency_account = f"conformance-jpy-{uuid.uuid4().hex[:12]}"
    status, _ = request(base_url + "/accounts", "POST", {"id": currency_account, "currency": "JPY"})
    if status != 201:
        raise RuntimeError("could not create currency-mismatch fixture")
    mismatch = {"idempotency_key": f"conformance-{uuid.uuid4().hex}", "from_account": "demo-alice", "to_account": currency_account, "amount_minor": 1, "currency": "USD"}
    status, body = request(base_url + "/transfers", "POST", mismatch)
    if status != 409 or body.get("error", {}).get("code") != "currency_mismatch":
        raise RuntimeError(f"currency-mismatch check failed: HTTP {status} {body}")
    checks["currency_mismatch"] = True
    return checks


def main():
    host = os.environ.get("LEDGER_HOST", "127.0.0.1")
    port = int(os.environ.get("LEDGER_PORT", "8080"))
    print(f"LedgerLab Python API listening on http://{host}:{port} (database: {DB_PATH})", flush=True)
    Server((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
