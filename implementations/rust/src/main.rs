use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    env,
    io::Read,
    sync::{Arc, Condvar, Mutex},
    thread,
    time::{Duration, Instant},
};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use uuid::Uuid;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, currency TEXT NOT NULL, balance_minor INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, from_account TEXT NOT NULL, to_account TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL, posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_id TEXT NOT NULL REFERENCES transfers(id), account_id TEXT NOT NULL REFERENCES accounts(id), amount_minor INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS entries_account ON entries(account_id, id);
"#;

fn open_db(path: &str) -> rusqlite::Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(30))?;
    db.pragma_update(None, "foreign_keys", "ON")?;
    Ok(db)
}

fn balance(db: &Connection, account: &str) -> rusqlite::Result<i64> {
    db.query_row("SELECT balance_minor FROM accounts WHERE id=?1", [account], |r| r.get(0))
}

fn initialize(path: &str) -> rusqlite::Result<()> {
    let db = open_db(path)?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.execute_batch(SCHEMA)?;
    let _ = db.execute("ALTER TABLE accounts ADD COLUMN balance_minor INTEGER NOT NULL DEFAULT 0", []);
    db.execute("UPDATE accounts SET balance_minor=COALESCE((SELECT SUM(amount_minor) FROM entries WHERE entries.account_id=accounts.id),0)", [])?;
    let seeds = [
        ("demo-alice", "USD"), ("demo-bob", "USD"),
        ("ledgerlab-funder", "USD"), ("demo-clearing", "USD"),
    ];
    for (id, currency) in seeds { db.execute("INSERT OR IGNORE INTO accounts(id,currency) VALUES(?1,?2)", params![id,currency])?; }
    let openings = [
        ("ledgerlab-opening-funder-v1", "demo-clearing", "ledgerlab-funder", 1_000_000_000_000_000_i64),
        ("ledgerlab-opening-alice-v1", "demo-clearing", "demo-alice", 250_000_i64),
        ("ledgerlab-opening-bob-v1", "demo-clearing", "demo-bob", 250_000_i64),
    ];
    for (key, source, target, amount) in openings {
        let exists: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM transfers WHERE idempotency_key=?1)", [key], |r| r.get(0))?;
        if !exists {
            let id = Uuid::new_v4().to_string();
            db.execute("INSERT INTO transfers(id,idempotency_key,request_hash,from_account,to_account,amount_minor,currency) VALUES(?1,?2,'synthetic-opening-v1',?3,?4,?5,'USD')", params![id,key,source,target,amount])?;
            db.execute("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?1,?2,?3)", params![id,source,-amount])?;
            db.execute("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?1,?2,?3)", params![id,target,amount])?;
            db.execute("UPDATE accounts SET balance_minor=balance_minor-?1 WHERE id=?2", params![amount,source])?;
            db.execute("UPDATE accounts SET balance_minor=balance_minor+?1 WHERE id=?2", params![amount,target])?;
        }
    }
    Ok(())
}

fn json_response(status: u16, value: Value) -> Response<std::io::Cursor<Vec<u8>>> {
    let body = serde_json::to_vec(&value).unwrap_or_else(|_| b"{}".to_vec());
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json; charset=utf-8"[..]).unwrap();
    Response::from_data(body).with_status_code(StatusCode(status)).with_header(header)
}

fn fail(status: u16, code: &str, message: impl ToString) -> Response<std::io::Cursor<Vec<u8>>> {
    json_response(status, json!({"error":{"code":code,"message":message.to_string()}}))
}

/// Serve one request on the worker's persistent connection.
fn handle(mut request: Request, db: &mut Connection) {
    let method = request.method().clone();
    let raw_url = request.url().to_string();
    let path = raw_url.split('?').next().unwrap_or("/");
    let response = if method == Method::Get && path == "/health" {
        json_response(200, json!({"status":"ok","implementation":"rust","protocol_version":1}))
    } else if method == Method::Get && path == "/metrics" {
        let entries: i64 = db.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0)).unwrap_or(0);
        let net: i64 = db.query_row("SELECT COALESCE(SUM(amount_minor),0) FROM entries", [], |r| r.get(0)).unwrap_or(0);
        let transfers: i64 = db.query_row("SELECT COUNT(*) FROM transfers", [], |r| r.get(0)).unwrap_or(0);
        let invalid: i64 = db.query_row("SELECT COUNT(*) FROM (SELECT transfer_id FROM entries GROUP BY transfer_id HAVING COUNT(*) != 2 OR SUM(amount_minor) != 0)", [], |r| r.get(0)).unwrap_or(0);
        let balance_mismatches: i64 = db.query_row("SELECT COUNT(*) FROM accounts a WHERE a.balance_minor != COALESCE((SELECT SUM(amount_minor) FROM entries e WHERE e.account_id=a.id),0)", [], |r| r.get(0)).unwrap_or(-1);
        json_response(200, json!({"entry_count":entries,"net_minor":net,"transfer_count":transfers,"invalid_transfer_groups":invalid,"balance_mismatch_accounts":balance_mismatches}))
    } else if method == Method::Get && path == "/ledger" {
        let account=raw_url.split("account_id=").nth(1).map(|s|s.split('&').next().unwrap_or("")).filter(|s|!s.is_empty());
        let limit=raw_url.split("limit=").nth(1).and_then(|s|s.split('&').next()?.parse::<u32>().ok()).unwrap_or(100).clamp(1,1000);
        let mut entries=Vec::new();
        if let Some(account_id)=account {
            if let Ok(mut stmt)=db.prepare("SELECT id,transfer_id,account_id,amount_minor,created_at FROM entries WHERE account_id=?1 ORDER BY id DESC LIMIT ?2") {
                if let Ok(rows)=stmt.query_map(params![account_id,limit],|r|Ok(json!({"id":r.get::<_,i64>(0)?,"transfer_id":r.get::<_,String>(1)?,"account_id":r.get::<_,String>(2)?,"amount_minor":r.get::<_,i64>(3)?,"created_at":r.get::<_,String>(4)?}))) { entries.extend(rows.flatten()); }
            }
        } else if let Ok(mut stmt)=db.prepare("SELECT id,transfer_id,account_id,amount_minor,created_at FROM entries ORDER BY id DESC LIMIT ?1") {
            if let Ok(rows)=stmt.query_map([limit],|r|Ok(json!({"id":r.get::<_,i64>(0)?,"transfer_id":r.get::<_,String>(1)?,"account_id":r.get::<_,String>(2)?,"amount_minor":r.get::<_,i64>(3)?,"created_at":r.get::<_,String>(4)?}))) { entries.extend(rows.flatten()); }
        }
        json_response(200,json!({"entries":entries}))
    } else if method == Method::Get && path == "/accounts" {
        let mut accounts=Vec::new();
        if let Ok(mut stmt)=db.prepare("SELECT id,currency,created_at FROM accounts ORDER BY id") {
            let rows=stmt.query_map([], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)));
            if let Ok(rows)=rows { for row in rows.flatten() { let (id,currency,created)=row; let bal=balance(db,&id).unwrap_or(0); accounts.push(json!({"id":id,"currency":currency,"created_at":created,"balance_minor":bal})); } }
        }
        json_response(200,json!({"accounts":accounts}))
    } else if method == Method::Get && path.starts_with("/accounts/") {
        let id=&path[10..];
        match db.query_row("SELECT currency,created_at FROM accounts WHERE id=?1",[id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional() {
            Ok(Some((currency,created))) => json_response(200,json!({"id":id,"currency":currency,"created_at":created,"balance_minor":balance(db,id).unwrap_or(0)})),
            Ok(None) => fail(404,"account_not_found","Account not found"), Err(e)=>fail(500,"storage_error",e)
        }
    } else if method == Method::Post && path == "/bench/memory" {
        let mut body=String::new();
        if request.as_reader().take(65537).read_to_string(&mut body).is_err() || body.len()>65536 { fail(400,"invalid_request","Invalid request body") }
        else { match serde_json::from_str::<Value>(&body) {
            Ok(v) => { let users=v["users"].as_u64().unwrap_or(8) as usize; let seconds=v["seconds"].as_u64().unwrap_or(5);
                if !(1..=128).contains(&users)||!(1..=30).contains(&seconds) { fail(400,"invalid_request","users must be 1–128 and seconds 1–30") }
                else { memory_core_benchmark(users,seconds) } }, Err(e)=>fail(400,"invalid_request",e)
        }}
    } else if method == Method::Post && path == "/accounts" {
        let mut body=String::new();
        if request.as_reader().take(65537).read_to_string(&mut body).is_err() || body.len()>65536 { fail(400,"invalid_request","Invalid request body") }
        else { match serde_json::from_str::<Value>(&body) {
            Ok(v) => { let id=v["id"].as_str().unwrap_or(""); let currency=v["currency"].as_str().unwrap_or("USD").to_uppercase();
                if id.trim().is_empty()||id.len()>80||currency.len()!=3 { fail(400,"invalid_request","id and three-character currency are required") }
                else { match db.execute("INSERT INTO accounts(id,currency) VALUES(?1,?2)",params![id,currency]) {
                    Ok(_) => json_response(201,json!({"id":id,"currency":currency,"balance_minor":0})), Err(e)=>fail(409,"conflict",e)
                }} }, Err(e)=>fail(400,"invalid_request",e)
        }}
    } else if method == Method::Post && path == "/transfers" {
        let mut body=String::new();
        if request.as_reader().take(65537).read_to_string(&mut body).is_err()||body.len()>65536 { fail(400,"invalid_request","Invalid request body") }
        else { transfer(&body,&request,db) }
    } else { fail(404,"not_found","Route not found") };
    let _=request.respond(response);
}

fn memory_core_benchmark(users: usize, seconds: u64) -> Response<std::io::Cursor<Vec<u8>>> {
    let started=Instant::now(); let deadline=started+Duration::from_secs(seconds);
    let handles:Vec<_>=(0..users).map(|_|thread::spawn(move||{
        let (mut left,mut right)=(1_000_000_000_000_i64,1_000_000_000_000_i64); let mut count=0_u64;
        while Instant::now()<deadline {
            if count&1==0 { if left<1 { break; } left-=1; right+=1; } else { if right<1 { break; } right-=1; left+=1; }
            count+=1;
        }
        (count,left+right==2_000_000_000_000_i64)
    })).collect();
    let mut successes=0_u64; let mut valid=true;
    for handle in handles { match handle.join() { Ok((n,ok))=>{successes+=n;valid&=ok},Err(_)=>valid=false } }
    let elapsed=started.elapsed().as_secs_f64();
    json_response(200,json!({"mode":"in_memory_core","users":users,"seconds":elapsed,"successful_transfers":successes,
        "throughput_ops_per_second":successes as f64/elapsed,"state_model":"independent thread-local account pair per worker",
        "http_or_sqlite_included":false,"invariants":{"all_pairs_conserved":valid,"passed":valid}}))
}

fn transfer(body: &str, request: &Request, db: &mut Connection) -> Response<std::io::Cursor<Vec<u8>>> {
    let value: Value=match serde_json::from_str(body){Ok(v)=>v,Err(e)=>return fail(400,"invalid_request",e)};
    let source=value["from_account"].as_str().unwrap_or(""); let target=value["to_account"].as_str().unwrap_or("");
    let amount=match value["amount_minor"].as_i64(){Some(n) if n>0=>n,_=>return fail(400,"invalid_request","amount_minor must be a positive integer")};
    let currency=value["currency"].as_str().unwrap_or("USD").to_uppercase();
    let key=value["idempotency_key"].as_str().or_else(||request.headers().iter().find(|h|h.field.equiv("Idempotency-Key")).map(|h|h.value.as_str())).unwrap_or("");
    if key.is_empty()||key.len()>200||source.is_empty()||target.is_empty()||source==target||currency.len()!=3{return fail(400,"invalid_request","A key, distinct accounts, and three-character currency are required")}
    let hash=serde_json::to_string(&json!([source,target,amount,currency])).unwrap();
    let tx=match db.transaction_with_behavior(TransactionBehavior::Immediate){Ok(tx)=>tx,Err(e)=>return fail(500,"storage_error",e)};
    let prior:Option<(String,String)>=tx.query_row("SELECT id,request_hash FROM transfers WHERE idempotency_key=?1",[key],|r|Ok((r.get(0)?,r.get(1)?))).optional().unwrap_or(None);
    if let Some((id,old_hash))=prior { if old_hash!=hash{return fail(409,"idempotency_conflict","Key was used for a different transfer")};let from=balance(&tx,source).unwrap_or(0);let to=balance(&tx,target).unwrap_or(0);drop(tx);return json_response(200,json!({"id":id,"status":"posted","idempotent_replay":true,"from_balance_minor":from,"to_balance_minor":to})); }
    let accounts:Vec<(String,String)>=tx.prepare("SELECT id,currency FROM accounts WHERE id IN (?1,?2)").and_then(|mut s|s.query_map(params![source,target],|r|Ok((r.get(0)?,r.get(1)?))).map(|it|it.flatten().collect())).unwrap_or_default();
    if accounts.len()!=2{return fail(404,"account_not_found","Source or destination account not found")}
    if accounts.iter().any(|(_,c)|c!=&currency){return fail(409,"currency_mismatch","Transfer currency must match both accounts")}
    let from=balance(&tx,source).unwrap_or(0);if from<amount{return fail(409,"insufficient_funds","Source account has insufficient funds")}
    let id=Uuid::new_v4().to_string();
    if let Err(e)=tx.execute("INSERT INTO transfers(id,idempotency_key,request_hash,from_account,to_account,amount_minor,currency) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![id,key,hash,source,target,amount,currency]){return fail(409,"conflict",e)}
    if let Err(e)=tx.execute("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?1,?2,?3)",params![id,source,-amount]).and_then(|_|tx.execute("INSERT INTO entries(transfer_id,account_id,amount_minor) VALUES(?1,?2,?3)",params![id,target,amount])){return fail(500,"storage_error",e)}
    if let Err(e)=tx.execute("UPDATE accounts SET balance_minor=balance_minor-?1 WHERE id=?2",params![amount,source]).and_then(|_|tx.execute("UPDATE accounts SET balance_minor=balance_minor+?1 WHERE id=?2",params![amount,target])){return fail(500,"storage_error",e)}
    let to=balance(&tx,target).unwrap_or(0);if let Err(e)=tx.commit(){return fail(500,"storage_error",e)}
    json_response(201,json!({"id":id,"status":"posted","idempotent_replay":false,"from_balance_minor":from-amount,"to_balance_minor":to}))
}

fn main() {
    let host=env::var("LEDGER_HOST").unwrap_or_else(|_|"127.0.0.1".into());
    let port=env::var("LEDGER_PORT").unwrap_or_else(|_|"8082".into());
    let db_path=env::var("LEDGER_DB").unwrap_or_else(|_|"ledger-rust.sqlite3".into());
    initialize(&db_path).expect("initialize SQLite ledger");
    let server=Server::http(format!("{host}:{port}")).expect("bind HTTP server");
    let workers=env::var("LEDGER_THREADS").ok().and_then(|v|v.parse().ok())
        .unwrap_or_else(||thread::available_parallelism().map(|n|n.get()).unwrap_or(4));
    eprintln!("LedgerLab Rust listening on {host}:{port} ({workers} workers)");

    let queue: Arc<(Mutex<VecDeque<Request>>, Condvar)> =
        Arc::new((Mutex::new(VecDeque::new()), Condvar::new()));

    for _ in 0..workers {
        let queue = Arc::clone(&queue);
        let mut db = open_db(&db_path).expect("worker opens its own SQLite connection");
        thread::spawn(move || {
            let (lock, cvar) = &*queue;
            loop {
                let request = {
                    let mut deque = lock.lock().unwrap();
                    loop {
                        if let Some(request) = deque.pop_front() { break request; }
                        deque = cvar.wait(deque).unwrap();
                    }
                };
                handle(request, &mut db);
            }
        });
    }

    let (lock, cvar) = &*queue;
    for request in server.incoming_requests() {
        lock.lock().unwrap().push_back(request);
        cvar.notify_one();
    }
}