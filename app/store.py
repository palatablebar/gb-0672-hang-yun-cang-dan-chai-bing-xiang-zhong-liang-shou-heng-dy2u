import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS booking_confirmations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        seal TEXT NOT NULL,
        receipt_text TEXT NOT NULL,
        release_mode TEXT NOT NULL,
        booking TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '现行',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(run_id, seal))""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def confirm_booking(path, run_id, booking, receipt_text):
    """落库订舱确认单。

    成功条件：至少存在一个可进入确认单的已接受箱；否则确认失败，旧版保留。
    幂等：同批次同裁决印章（回执+箱裁决决定）只保留一条，重跑不新增第二套。
    """
    summary=booking['summary']
    if summary['accepted'] <= 0:
        # 确认失败：不写入新版本，旧版确认单原样保留
        raise BusinessError('CONFIRMATION_FAILED',
            f"无重量对平且来源唯一的箱可进入确认单（待补证{summary['pending']}/退回{summary['returned']}），确认失败并保留旧版确认单",409)
    seal=booking['seal']
    with connect(path) as con:
        if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
            raise BusinessError('NOT_FOUND','批次不存在',404)
        existing=con.execute('SELECT id FROM booking_confirmations WHERE run_id=? AND seal=?',(run_id,seal)).fetchone()
        if existing:
            row=con.execute('SELECT * FROM booking_confirmations WHERE id=?',(existing['id'],)).fetchone()
            return _to_dict(row, reused=True)
        # 新版生效：旧版标记为历史，不删除（归属不清/覆盖时原明细仍可追溯）
        con.execute("UPDATE booking_confirmations SET status='历史' WHERE run_id=? AND status='现行'",(run_id,))
        cur=con.execute("""INSERT INTO booking_confirmations
            (run_id,seal,receipt_text,release_mode,booking,status) VALUES(?,?,?,?,?,'现行')""",
            (run_id,seal,receipt_text,booking['release_mode'],json.dumps(booking,ensure_ascii=False)))
        row=con.execute('SELECT * FROM booking_confirmations WHERE id=?',(cur.lastrowid,)).fetchone()
    return _to_dict(row, reused=False)

def latest_booking(path, run_id):
    with connect(path) as con:
        row=con.execute("""SELECT * FROM booking_confirmations WHERE run_id=?
            ORDER BY CASE status WHEN '现行' THEN 0 ELSE 1 END, id DESC LIMIT 1""",(run_id,)).fetchone()
    return _to_dict(row) if row else None

def booking_versions(path, run_id):
    with connect(path) as con:
        rows=con.execute('SELECT id,seal,release_mode,status,created_at FROM booking_confirmations WHERE run_id=? ORDER BY id DESC',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def _to_dict(row, reused=None):
    data=dict(row)
    data['booking']=json.loads(data['booking'])
    if reused is not None: data['reused']=reused
    return data
