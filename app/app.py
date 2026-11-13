"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障页：已入库记录 + 分批领批 / GeoJSON 下载 / 回执签收结批
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /api/batches 领批、按批号重下、上传导入回执（签收与结批同一事务）
- /photos/...  已入库的原照
"""
import csv
import io
import json
import os
import re
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone

from flask import (Flask, Response, g, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL,          -- photos 目录下的文件名
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);

-- 批次：同一时间最多只允许一个 open 批次（部分唯一索引兜底并发领批）
CREATE TABLE IF NOT EXISTS batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no       TEXT UNIQUE,          -- 对外批号，如 B-0001
    status         TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    receipt_ids    TEXT,                 -- 已接受回执的 record_id 集合（排序 JSON）
    receipt_result TEXT                  -- 接受回执时的结果 JSON（原样重放）
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_batches_one_open
    ON batches(status) WHERE status = 'open';

-- 批内点位：领批时冻结，之后新补传的点不会加进来
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    record_id  INTEGER NOT NULL REFERENCES records(id),
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | signed | carried
    signed_at  TEXT,
    PRIMARY KEY (batch_id, record_id)
);
-- 一笔记录同一时间只能挂在一个未结批次里（未签收的点位留到下批时才会腾出）
CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_items_pending
    ON batch_items(record_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_batch_items_batch ON batch_items(batch_id);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        # isolation_level=None：事务由下面显式 BEGIN IMMEDIATE / COMMIT 控制，
        # 领批与结批都用写锁串行化，避免两个窗口同时建出两个未结批次。
        g.db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def no_store_html(resp):
    # HTML 页面不做 HTTP 缓存，保证在线时拿到最新数据；
    # 离线可用性由 Service Worker 的 Cache Storage 负责。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service Worker 必须挂在根作用域下：从 / 路径提供，控制整个站点
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/records")
def records_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    batches = db.execute(
        "SELECT * FROM batches ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows, batches=batches)


# ---------------- 批次辅助 ----------------

def batch_to_dict(db, batch):
    """批次的对外信息（不含点位）。"""
    counts = db.execute(
        "SELECT status, COUNT(*) AS n FROM batch_items "
        "WHERE batch_id = ? GROUP BY status", (batch["id"],)
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in counts}
    item_count = sum(by_status.values())
    return {
        "batch_no": batch["batch_no"],
        "status": batch["status"],
        "created_at": batch["created_at"],
        "closed_at": batch["closed_at"],
        "item_count": item_count,
        "signed_count": by_status.get("signed", 0),
        "carried_count": by_status.get("carried", 0),
        "pending_count": by_status.get("pending", 0),
        "geojson_url": "/api/batches/%s.geojson" % batch["batch_no"],
        "receipt_url": "/api/batches/%s/receipt" % batch["batch_no"],
    }


def batch_items(db, batch_id):
    rows = db.execute(
        "SELECT bi.record_id, bi.status, bi.signed_at, "
        "       r.lat, r.lng, r.note, r.photo, r.created_at, r.uploaded_at "
        "FROM batch_items bi JOIN records r ON r.id = bi.record_id "
        "WHERE bi.batch_id = ? ORDER BY bi.record_id", (batch_id,)
    ).fetchall()
    return [
        {
            "record_id": r["record_id"],
            "status": r["status"],
            "signed_at": r["signed_at"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ]


def find_batch(db, batch_no):
    return db.execute(
        "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
    ).fetchone()


# ---------------- 记录 API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or now_iso()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 保存原照（文件名用 uuid，天然去重）----
    ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    fname = client_uuid + ext
    photo.save(os.path.join(PHOTO_DIR, fname))

    # ---- 入库；唯一约束兜底并发/重试竞争 ----
    try:
        cur = db.execute(
            "INSERT INTO records (uuid, lat, lng, note, photo, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_uuid, lat, lng, note, fname, created_at, now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # 另一请求已用同一 uuid 入库：照片文件名相同，不能删除（已被胜者引用）。
        _rollback_quietly(db)
        row = db.execute(
            "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
        ).fetchone()
        return jsonify({"ok": True, "duplicate": True,
                        "id": row["id"] if row else None})

    return jsonify({"ok": True, "duplicate": False, "id": cur.lastrowid})


@app.route("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name)


# ---------------- 领批 ----------------

def _rollback_quietly(db):
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _open_batch_response(db, batch):
    """已有未结批次时统一返回原批（含部分唯一索引兜底的并发路径）。"""
    return jsonify({
        "ok": True,
        "created": False,
        "reused_open_batch": True,
        "batch": batch_to_dict(db, batch),
    })


def _claim_impl():
    """领批：有未结批次就原样返回；否则把已入库且未签收的点位冻结成新批。

    整个过程在一个 BEGIN IMMEDIATE 写事务内完成，两个窗口同时领批时
    后到者会等锁释放后读到同一个 open 批次，不会建出第二个。
    """
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        open_batch = db.execute(
            "SELECT * FROM batches WHERE status = 'open'"
        ).fetchone()
        if open_batch is not None:
            resp = _open_batch_response(db, open_batch)
            db.execute("COMMIT")
            return resp

        eligible = db.execute(
            "SELECT id FROM records r WHERE NOT EXISTS ("
            "  SELECT 1 FROM batch_items bi "
            "  WHERE bi.record_id = r.id AND bi.status IN ('signed', 'pending')"
            ") ORDER BY r.id"
        ).fetchall()

        if not eligible:
            # 没有待发点：不建空批，巡护员可以继续补传新记录。
            db.execute("COMMIT")
            return jsonify({
                "ok": True,
                "created": False,
                "reused_open_batch": False,
                "batch": None,
                "message": "没有待发点位，未创建空批次；可继续补传新记录。",
            })

        created_at = now_iso()
        cur = db.execute(
            "INSERT INTO batches (batch_no, status, created_at) "
            "VALUES (NULL, 'open', ?)", (created_at,)
        )
        batch_id = cur.lastrowid
        batch_no = "B-%04d" % batch_id
        db.execute(
            "UPDATE batches SET batch_no = ? WHERE id = ?", (batch_no, batch_id)
        )
        db.executemany(
            "INSERT INTO batch_items (batch_id, record_id, status) "
            "VALUES (?, ?, 'pending')",
            [(batch_id, r["id"]) for r in eligible],
        )
        batch = db.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        resp = jsonify({
            "ok": True,
            "created": True,
            "reused_open_batch": False,
            "batch": batch_to_dict(db, batch),
        })
        db.execute("COMMIT")
        return resp
    except sqlite3.IntegrityError:
        # 唯一索引兜底（理论上 IMMEDIATE 已串行化）：已有 open 批次就返回它
        _rollback_quietly(db)
        batch = db.execute(
            "SELECT * FROM batches WHERE status = 'open'"
        ).fetchone()
        if batch is not None:
            return _open_batch_response(db, batch)
        raise
    except Exception:
        _rollback_quietly(db)
        raise


@app.route("/api/batches/claim", methods=["POST", "GET"])
def api_claim():
    return _claim_impl()


@app.route("/api/batches", methods=["GET"])
def api_batches():
    db = get_db()
    rows = db.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    return jsonify({
        "ok": True,
        "batches": [batch_to_dict(db, b) for b in rows],
    })


@app.route("/api/batches/<batch_no>", methods=["GET"])
def api_batch_info(batch_no):
    db = get_db()
    batch = find_batch(db, batch_no)
    if batch is None:
        return jsonify({"ok": False, "error": "批次不存在"}), 404
    info = batch_to_dict(db, batch)
    info["items"] = batch_items(db, batch["id"])
    if batch["receipt_ids"]:
        info["receipt_ids"] = json.loads(batch["receipt_ids"])
    return jsonify({"ok": True, "batch": info})


# ---------------- 批号下载 GeoJSON（WGS84） ----------------

@app.route("/api/batches/<batch_no>.geojson", methods=["GET"])
def api_batch_geojson(batch_no):
    return _batch_geojson(batch_no)


@app.route("/api/batches/<batch_no>/geojson", methods=["GET"])
def api_batch_geojson_alias(batch_no):
    return _batch_geojson(batch_no)


def _batch_geojson(batch_no):
    db = get_db()
    batch = find_batch(db, batch_no)
    if batch is None:
        return jsonify({"ok": False, "error": "批次不存在"}), 404
    items = batch_items(db, batch["id"])
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                # GeoJSON 坐标顺序为 [经度, 纬度]，WGS84
                "coordinates": [it["lng"], it["lat"]],
            },
            "properties": {
                "record_id": it["record_id"],   # 原入库编号
                "batch_no": batch_no,
                "note": it["note"],
                "created_at": it["created_at"],
                "uploaded_at": it["uploaded_at"],
                "photo_url": it["photo_url"],
                "signed": it["status"] == "signed",
            },
        }
        for it in items
    ]
    payload = {
        "type": "FeatureCollection",
        "name": "倒木点位批次 %s" % batch_no,
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "properties": {
            "batch_no": batch_no,
            "status": batch["status"],
            "created_at": batch["created_at"],
            "closed_at": batch["closed_at"],
            "item_count": len(features),
            "datum": "WGS84",
        },
        "features": features,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resp = Response(body, mimetype="application/geo+json")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="batch-%s.geojson"' % batch_no
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 导入回执：签收 + 结批（同一事务） ----------------

class ReceiptError(ValueError):
    pass


def parse_receipt_csv(raw):
    """解析回执 CSV，返回导入成功的 record_id 集合。

    只接受 UTF-8 CSV；首行表头必须含 record_id 列。只有表头表示全批未导入。
    任何格式问题都抛 ReceiptError —— 整份退回，不产生部分签收。
    """
    try:
        text = raw.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError):
        raise ReceiptError("回执必须是 UTF-8 编码的 CSV 文件")
    if not text.strip():
        raise ReceiptError("回执为空：至少需要表头 record_id")

    try:
        rows = [row for row in csv.reader(io.StringIO(text))
                if any((cell or "").strip() for cell in row)]
    except csv.Error as e:
        raise ReceiptError("CSV 无法解析：%s" % e)

    if not rows:
        raise ReceiptError("回执为空：至少需要表头 record_id")

    header = [h.strip().lower() for h in rows[0]]
    if "record_id" not in header:
        raise ReceiptError("回执表头必须包含 record_id 列")
    col = header.index("record_id")

    ids = set()
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise ReceiptError(
                "回执第 %d 行列数与表头不一致（应 %d 列，实际 %d 列）"
                % (lineno, len(header), len(row))
            )
        cell = (row[col] or "").strip()
        if not cell:
            raise ReceiptError("回执第 %d 行 record_id 为空" % lineno)
        if not re.fullmatch(r"[0-9]+", cell):
            raise ReceiptError(
                "回执第 %d 行 record_id 不是正整数：%s" % (lineno, cell)
            )
        value = int(cell)
        if value <= 0:
            raise ReceiptError(
                "回执第 %d 行 record_id 不是正整数：%s" % (lineno, cell)
            )
        ids.add(value)
    return ids


def _receipt_upload():
    """从 multipart 文件字段或原始请求体取出 CSV 字节。"""
    upload = None
    for field in ("file", "csv", "receipt", "upload"):
        upload = request.files.get(field)
        if upload is not None:
            break
    if upload is None and request.files:
        upload = next(iter(request.files.values()))
    if upload is not None:
        return upload.read()
    return request.get_data() or b""


@app.route("/api/batches/<batch_no>/receipt", methods=["POST"])
def api_batch_receipt(batch_no):
    db = get_db()
    raw = _receipt_upload()
    try:
        submitted = parse_receipt_csv(raw)
    except ReceiptError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    db.execute("BEGIN IMMEDIATE")
    try:
        batch = find_batch(db, batch_no)
        if batch is None:
            _rollback_quietly(db)
            return jsonify({"ok": False, "error": "批次不存在"}), 404

        if batch["status"] == "closed":
            # 已结批重交：编号集合相同 -> 原样返回当时的结果；不同 -> 拒绝。
            stored_ids = set(json.loads(batch["receipt_ids"] or "[]"))
            if submitted == stored_ids:
                result = batch["receipt_result"]
                db.execute("COMMIT")
                return Response(result, mimetype="application/json")
            _rollback_quietly(db)
            missing = sorted(stored_ids - submitted)
            extra = sorted(submitted - stored_ids)
            return jsonify({
                "ok": False,
                "error": "批次 %s 已结批，重交的记录编号集合与原回执不一致"
                         % batch_no,
                "batch_no": batch_no,
                "accepted_receipt_ids": sorted(stored_ids),
                "missing_ids": missing,
                "unexpected_ids": extra,
            }), 409

        # 未结批：校验编号全部属于本批（批外编号整份退回）
        batch_ids = {r["record_id"] for r in db.execute(
            "SELECT record_id FROM batch_items WHERE batch_id = ?",
            (batch["id"],),
        ).fetchall()}
        outside = sorted(submitted - batch_ids)
        if outside:
            _rollback_quietly(db)
            return jsonify({
                "ok": False,
                "error": "回执含批外编号，整份退回：%s"
                         % ", ".join(str(i) for i in outside),
                "batch_no": batch_no,
                "outside_ids": outside,
            }), 400

        signed_ids = sorted(submitted)
        carried_ids = sorted(batch_ids - submitted)
        closed_at = now_iso()

        # 签收与结批在同一事务里写入：签收的点记 signed，未签收的留到下批。
        if signed_ids:
            db.executemany(
                "UPDATE batch_items SET status = 'signed', signed_at = ? "
                "WHERE batch_id = ? AND record_id = ?",
                [(closed_at, batch["id"], rid) for rid in signed_ids],
            )
        if carried_ids:
            db.executemany(
                "UPDATE batch_items SET status = 'carried', signed_at = NULL "
                "WHERE batch_id = ? AND record_id = ?",
                [(batch["id"], rid) for rid in carried_ids],
            )

        result = {
            "ok": True,
            "batch_no": batch_no,
            "status": "closed",
            "signed_count": len(signed_ids),
            "carried_count": len(carried_ids),
            "signed_ids": signed_ids,
            "carried_ids": carried_ids,
            "closed_at": closed_at,
            "message": "本批已结批：已签收 %d 笔，未签收 %d 笔留待下一批。"
                       % (len(signed_ids), len(carried_ids)),
        }
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        db.execute(
            "UPDATE batches SET status = 'closed', closed_at = ?, "
            "receipt_ids = ?, receipt_result = ? WHERE id = ?",
            (closed_at, json.dumps(signed_ids), result_json, batch["id"]),
        )
        db.execute("COMMIT")
        return Response(result_json, mimetype="application/json")
    except Exception:
        _rollback_quietly(db)
        raise


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
