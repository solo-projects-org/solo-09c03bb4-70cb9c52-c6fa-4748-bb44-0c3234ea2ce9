# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite，供清障队在网页上查看并安排清障。

## 功能与实现

| 需求 | 实现 |
| --- | --- |
| 首次在线打开后，断网刷新也能填写 | Service Worker 安装时缓存应用外壳（页面/JS/CSS/样例照片），断网时导航请求回退到缓存 |
| 待传记录不丢 | 记录（含照片 Blob）存入浏览器 IndexedDB，刷新、关机重启均在 |
| 回网点补传，坐标+原照入 SQLite | `POST /api/records`（multipart），照片落盘、元数据入库 |
| 收到确认才移出待传 | 浏览器仅在收到服务器 `{"ok": true}` 响应后才删除 IndexedDB 中的该笔 |
| 确认丢失，重传只入库一次 | 每笔记录保存时生成 UUID 作幂等键；`records.uuid` 唯一约束，重复提交返回 `duplicate: true` 直接确认，不重复写行、不重复存照片 |
| 入库后才供清障队查看 | `/records` 页面只渲染 SQLite 中已确认的记录 |
| 随附样例照片 | `app/static/sample-photo.png`（演示时无相机可点"使用样例照片"，离线也能取到） |
| 分批导入：领批 | `/records` 点"领取批次"。有未结批次就原样返回；否则把已入库且未签收的点冻结成新批。`BEGIN IMMEDIATE` 写事务 + `status='open'` 的部分唯一索引，两个窗口同时领批也只会拿到同一批 |
| 批内点位固定 | 点位在领批时写入 `batch_items`；之后补传的新点只入库、不进当前批。按批号可随时重下，内容不变 |
| 带批号 WGS84 GeoJSON | `GET /api/batches/<批号>.geojson`，`FeatureCollection`，坐标为 `[经度, 纬度]`（CRS84/WGS84），每个点 `properties.record_id` 为原入库编号，并带 `batch_no` |
| 回执 CSV | 首行表头需含 `record_id`，只列导入成功的编号；只有表头表示全批未导入。BOM、CRLF、列顺序、附加列均可；行数参差/非正整数/缺列整份退回 |
| 回执有误整份退回 | 含批外编号（不属于本批的 `record_id`）、格式错误均返回 400，不写入任何签收状态、不结批 |
| 签收与结批同一事务 | 一次 `BEGIN IMMEDIATE` 事务内把签收的点置 `signed`、未签收置 `carried`，并写 `batches.status='closed'`；中途失败整体回滚，未签收点留到下批 |
| 已结批重交 | 编号集合与原回执相同 → 原样返回当时保存的结果 JSON；不同 → 409 拒绝 |
| 无待发点不建空批 | 无可领点位时返回 `batch: null`，不创建批次；巡护员继续补传后再领批即可 |

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。

SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。

## 演示：断网采集 → 回网补传

1. **首次在线打开** `http://localhost:8000`，页面右下角无提示即已就绪（Service Worker 已缓存全部外壳资源）。
2. **模拟断网**（任选其一）：
   - 浏览器 DevTools → Network → 勾选 **Offline**；或
   - `docker compose stop`（更真实地模拟"林道上没信号"）。
3. **刷新页面** —— 页面照常打开。填写经纬度（或点"获取当前定位"），选择照片
   （演示可点"使用随附样例照片"），点 **保存到待传记录**。可多采几笔，再次刷新待传列表仍在。
4. **恢复网络**：取消 Offline，或 `docker compose start`。
5. 点 **📤 补传全部待传记录** —— 每笔收到服务器确认后才从待传列表移除。
6. 打开 **/records**（清障队查看页）即可看到已入库的坐标与原照。

**验证幂等**：补传途中若在服务器写入后断网（确认丢失），该笔仍留在待传；
再次补传时服务器按 UUID 判重，返回 `duplicate: true`，浏览器正常移出待传，
SQLite 中始终只有一行。

## 演示：清障队分批导入

1. 回到采集页，用随附样例照片再补传几笔（每笔幂等键不同），`/records` 中可见。
2. 打开 <http://localhost:8000/records>，点 **📦 领取批次** —— 当前已入库且未签收的点位
   冻结成新批（如 `B-0001`），页面列出每个点的**原入库编号**（`record_id`）与坐标。
3. 点 **⬇️ 下载本批 WGS84 GeoJSON**：文件带批号与每点 `record_id`，可直接导入 GIS。
4. 在领批**之后**再补传一笔新点 —— 本批点位数不变，新点留在库里等下一批。
5. 准备回执 CSV（可用"下载回执模板"），只填本次导入成功的 `record_id`，例如：

   ```csv
   record_id
   1
   2
   ```

   只有表头（`record_id` 一行）表示全批都没导入。
6. 点 **📥 上传回执**：整批结批，回执中的点标为已签收，未出现的点自动留待下一批。
   若回执含批外编号或格式有误，整份退回，本批仍为进行中，签收状态不变。
7. 再点 **📦 领取批次**：得到下一批，其中包含上一批未签收的点位 + 领批前新补传的点。
8. 对已结批再次上传相同编号集合的回执，会原样返回当时的结果；集合不同则被拒绝。
9. 批次历史表中可随时按批号重新下载已结批的 GeoJSON。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data python3 app/app.py        # 或：gunicorn --chdir app -b 0.0.0.0:8000 app:app
```

## 目录结构

```
├── docker-compose.yml      # 一键启动，卷持久化 /data
├── Dockerfile              # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py              # Flask 路由 + SQLite（uuid 幂等、批次/回执事务）
│   ├── templates/
│   │   ├── index.html      # 采集页（巡护员）
│   │   └── records.html    # 清障页（领批 / 下载 / 回执签收）
│   └── static/
│       ├── app.js          # IndexedDB 待传队列 + 补传逻辑
│       ├── batches.js      # 清障页领批、按批号重下、回执上传
│       ├── sw.js           # Service Worker（离线外壳缓存）
│       ├── style.css
│       └── sample-photo.png# 随附样例照片
└── tools/
    └── make_sample_photo.py# 样例照片生成脚本（纯标准库）
```

## API

- `GET  /api/records` — 已入库记录 JSON 列表
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `GET  /photos/<file>` — 已入库原照

### 批次与回执

- `POST /api/batches/claim` — 领批。有未结批次时 `{"created": false, "batch": {...}}`
  返回原批；否则冻结当前已入库且未签收的点位建新批 `{"created": true, ...}`；
  没有可领点位时 `{"batch": null}`（不建空批）。
- `GET  /api/batches` — 批次列表（含点数、签收数、结转数）
- `GET  /api/batches/<批号>` — 批次详情（含 `items[]`，每项有 `record_id`、坐标、状态）
- `GET  /api/batches/<批号>.geojson` — 下载该批 WGS84 GeoJSON（`application/geo+json`，
  附 `Content-Disposition`）。已结批仍可下载，内容与领批时一致。
- `POST /api/batches/<批号>/receipt` — 上传回执 CSV（multipart 字段 `file`，或原始请求体）。
  - 接受：签收回执中的 `record_id`，结批，未签收点留待下批；返回
    `signed_ids` / `carried_ids` / `closed_at`
  - 批外编号或格式错误：`400 {"ok": false, "error": "..."}`，签收状态不变、不结批
  - 已结批且编号集合相同：原样重放当时的结果（200）；集合不同：`409` 拒绝
