# #97 后台管理优化：排查与方案

> 目标：把后台管理从「能看」补到「能管」。
> 5 项：①数据库健康与维护 ②备份下载/删除/恢复 ③新模块纳管 ④⑤缓存统一管理 + 日志运维。
> 流程：备份 → 排查 → 方案 → 实现 → 测试 → 确认（老规矩）。

---

## 0. 备份（已完成）

```
backups/task97admin-20260928-041418/
├── app.py            3623 行
├── index.html        8176 行
├── market_phase.py    405 行
├── store.py          2289 行
└── strategy_eval.py   889 行
```

md5 逐一比对全部 OK。回滚方式：`cp backups/task97admin-20260928-041418/<file> <原路径>`。

---

## 1. 现状盘点（本轮实测，不是猜的）

| 项目 | 实测值 |
|---|---|
| 服务进程 | `server/run.py` PID 450235，端口 **8899**（不是 8000） |
| 数据库 | SQLite，483,639,296 B（462 MB），WAL 模式 |
| WAL / SHM | 527,392 B / 32,768 B（此前 228MB 僵死锁已自愈） |
| 页数 / 空闲页 | page_count=118,081 × 4096 = 483.6 MB，**freelist=0**（无碎片，VACUUM 零收益） |
| 表数 / 行数 | 12 张表，daily_bars **2,724,093** 行；全表 COUNT 耗时 **0.10s** |
| 磁盘 | 可用 251 G（备份空间充足，但不可无限增长） |
| `data/backups/` | **当前为空**（0 个备份） |
| admin 面板 / 路由 | 7 面板 / 20 路由（见 §2） |

已确认的**能力缺口**：

- 概览页有 users/bars/sources/uptime，**没有 DB 体积与 WAL 视图** —— 228MB WAL 僵死锁当初就是靠 shell 才发现的。
- `/api/admin/backups` 只能列清单，**无下载 / 删除 / 恢复**（半个功能）。
- #88 快讯源、#95 情绪周期、#96 监控中心三个新模块，**后台完全不可见**。
- 缓存只有体检缓存一个入口，情绪周期缓存（`market_phase_daily.json`, 50KB）**无法查看/清理**。
- 日志只能看尾部 300 行，**不能下载、不能清空**。

---

## 2. 待改文件清单

| 文件 | 改动性质 | 涉及功能 |
|---|---|---|
| `server/maintain.py` | **新建** | ①④⑤ 全部逻辑 + ② 备份 CRUD 迁移 |
| `server/app.py` | 改：新增 ~12 个 `/api/admin/*` 路由；`/backups`、`/backup` 迁到 maintain | ①②③④⑤ |
| `web/index.html` | 改：新增 2 个面板按钮 + 2 个渲染函数 + 扩展 admBackup / admLogs | ①②③④⑤ |
| `server/market_phase.py` | 可能改：补 `cache_status()` / `clear_cache()`（缓存纳管需要） | ④ |
| `server/strategy_eval.py` | 不改（已有 `cache_status/clear_cache/prewarm`，直接复用） | ④ |

**不改 `store.py`**：本轮全部是只读探测 + 文件级维护，用独立 sqlite3 连接，不碰 store 的 thread-local 连接池 —— 这是刻意的选择，见 R2/R9。

---

## 3. 风险排查（20 项，R5/R7/R9 已实测）

### 功能① 数据库健康与维护

| # | 风险 | 后果 | 方案 |
|---|---|---|---|
| R1 | **TRUNCATE checkpoint 抛 `database table is locked`**（实测：有未提交事务时 `sqlite3.OperationalError`） | 按钮报错，用户以为系统坏了 | 独立连接 + `busy_timeout=15s`；`try/except OperationalError` → **自动降级 PASSIVE** 并如实返回 `busy` 说明，绝不静默吞 |
| R2 | WAL 大时（曾 228MB）TRUNCATE 长持写锁 | 阻塞线上写入 >1 分钟 | **默认 PASSIVE**（不阻塞、不截断），TRUNCATE 需前端显式勾选并二次确认 |
| R3 | VACUUM 需 2 倍磁盘 + 全程独占 | 服务假死几分钟 | **不做一键 VACUUM**（YAGNI，实测 freelist=0 收益为零）；只在界面显示"可回收页数=0，无需整理" |
| R4 | MySQL 后端 `COUNT(*)` 全表慢 | 接口超时 | 行数统计分两路：SQLite 真 COUNT（实测 0.10s），MySQL 读 `information_schema.TABLES.TABLE_ROWS` 估值并标注"估算" |
| R5 | daily_bars 2.7M 行 COUNT 拖慢页面 | —— | **实测 0.10s，可接受**，不做异步化（YAGNI） |
| R6 | 只算 `tick.db` 漏掉 WAL/SHM | 低估占用 500KB~228MB | 三文件都算并分别展示 |
| R7 | 磁盘被备份撑爆（每个 462MB） | 磁盘满 → 写库失败 | 备份前检查可用空间 < 文件 2 倍 → 拒绝并提示"先删旧备份" |

### 功能② 备份下载 / 删除 / 恢复

| # | 风险 | 后果 | 方案 |
|---|---|---|---|
| R8 | **文件名目录穿越**（`../../etc/passwd`） | **任意文件读/删**（高危） | 双重校验：① 正则白名单 `^tick_\d{8}_\d{6}\.db$`；② `os.path.realpath()` 必须落在 `data/backups/` 内。任一不过 → 400 |
| R9 | **恢复覆盖当前数据不可回退** | 数据永久丢失 | 恢复前**强制自动快照** `auto_before_restore_{ts}.db`；失败的恢复可再用它回退 |
| R10 | 恢复时服务进程持有连接 | 数据不一致 | 用 `sqlite3.Connection.backup()` **反向写**（src=备份 ro，dst=主库 rw），实测 50001→50000 脏数据被正确清除；**不走文件删除/重命名**（否则 inode 变化，旧连接写入幽灵文件） |
| R11 | 恢复后旧连接页缓存未失效 | 读到旧数据 | 返回值强制带 `need_restart: true`，前端红字提示"请重启服务进程" |
| R12 | 462MB 文件下载撑爆内存 | OOM | `FileResponse` 流式传输，绝不 `read()` 进内存 |
| R13 | MySQL 后端没有 .db 文件 | 接口误用 | `backend != sqlite` 一律 400，提示去宿主机跑 mysqldump |
| R14 | 删除正在下载的备份 | 竞争 | 接受（概率极低，不值得加锁） |

### 功能③ 新模块纳管

| # | 风险 | 后果 | 方案 |
|---|---|---|---|
| R15 | 模块探测时联网（快讯源） | 接口慢/超时 | **只做本地状态探测**：缓存文件 mtime/size、表行数、进程内模块标记。**一个都不联网** |
| R16 | 模块未启用时探测报错 | 整页 500 | 每项独立 `try/except`，失败返回 `{"state":"unknown"}`，页面显示"未启用" |

### 功能④⑤ 缓存统一管理 + 日志运维

| # | 风险 | 后果 | 方案 |
|---|---|---|---|
| R17 | 清空情绪周期缓存后首访重算 | 卡顿 ~2s | 前端明确提示"下次访问重算约 2 秒"（实测首算 1.66s） |
| R18 | 误删正在写的日志文件 | 日志丢失 | **只提供清空（truncate 到 0 字节，保留 inode）**，不提供删除 |
| R19 | 日志文件名穿越 | 任意文件读 | 沿用现有校验 + `realpath` 前缀校验，且只认 `.log/.json` |
| R20 | 单个缓存状态查询失败 | 整页 500 | 每项独立 `try/except`，失败显示"不可用" |
| R21 | 清空缓存/日志不可逆 | 用户手滑 | 破坏性操作前端二次 `confirm`，且文案写明影响范围 |
| **R22** | **备份/恢复会让 WAL 暴涨**（实测 461MB 库 → WAL 464MB，磁盘占用翻倍；SQLite 自动 checkpoint 只挪了 3 页就停） | 磁盘被吃 2 倍，与当初 228MB 僵死锁同源 | 恢复完成后**自动补一次 TRUNCATE checkpoint**（实测 464MB → 0）；截断失败只写进 `wal_note`，不阻断恢复 |

---

## 4. 方案设计

### 4.1 新增 `server/maintain.py`（纯函数 + 独立连接）

```python
db_status()            -> {backend, files:{db,wal,shm,total}, pages:{size,count,freelist,
                           journal_mode}, tables:[{name,rows,est}], disk:{free,total}}
db_checkpoint(mode)    -> {ok, mode, wal_before, wal_after, busy, log, checkpointed, note, degraded}
backup_list()          -> {backend, items:[{name,size,mtime}]}
backup_create()        -> {ok, name, size}
backup_delete(name)    -> {ok, name}               # R8 白名单
backup_restore(name)   -> {ok, snapshot, need_restart:true}   # R9 自动快照 + R10 反向 backup
_safe_backup_path(name)-> str | None               # R8 双重校验的唯一入口
cache_status()         -> {items:[{key,name,detail,size,mtime,clearable}]}
cache_clear(key)       -> {ok, key, note}
modules_status()       -> {items:[{key,name,state,detail}]}
log_files() / log_tail(name, lines) / log_clear(name)
```

**三条铁律**（写进文件头注释）：

1. **绝不复用 `store._conn()`** —— 它是 thread-local，维护操作必须用独立 `sqlite3` 连接，否则会污染业务事务。
2. **破坏性操作一律先留后路** —— 恢复前自动快照；日志只清空不删除。
3. **路径校验走唯一入口 `_safe_backup_path()`** —— 不允许任何调用点自己拼路径。

### 4.2 API 新增（`server/app.py`）

```
GET    /api/admin/db           ① 数据库体积 / WAL / 表行数
POST   /api/admin/db/checkpoint ① WAL checkpoint（mode=passive|truncate）
GET    /api/admin/backups       ② 清单（已有，迁 maintain）
POST   /api/admin/backup        ② 创建（已有，迁 maintain）
GET    /api/admin/backups/{name}/download  ② 流式下载
DELETE /api/admin/backups/{name}           ② 删除
POST   /api/admin/backups/{name}/restore   ② 恢复（自动快照）
GET    /api/admin/modules      ③ 新模块状态
GET    /api/admin/caches       ④ 统一缓存状态
POST   /api/admin/caches/clear ④ 按 key 清理
DELETE /api/admin/logs         ⑤ 清空指定日志
GET    /api/admin/logs/download ⑤ 下载日志
```

全部走 `_require_admin(request)`，与现有 20 个 admin 路由同级。

### 4.3 前端（`web/index.html`）

- `ADMIN_TABS` 数组 +3 项：`database`(🛠️ 数据库 ①)、`modules`(🧩 模块状态 ③)、`services`(🧹 缓存管理 ④)。
- `#admTabs` +3 个按钮；`loadAdmin` 的 `fns` +3 个映射。
- `admBackup` 表格加「下载 / 恢复 / 删除」三列操作按钮（恢复+删除二次确认）。
- `admLogs` 加「下载全文 / 清空内容」按钮（清空保留 inode，注释写了为什么不做删除）。
- 面板数 7 → 10。

> 面板做成 3 个而不是方案初稿的 2 个：模块纳管（③）与缓存清理（④）语义不同，
> 硬塞进一个面板会让「状态只读」和「危险操作」混在一屏，反而更容易误点清空。

---

## 5. 测试计划（`tests/check_admin_maintain.py`）

| 组 | 用例 | 期望 |
|---|---|---|
| A 路径安全 | `../../etc/passwd`、`tick_1.db`、`tick_20260928_041418.db`、`/etc/passwd`、`tick_20260928_041418.db.extra` | 前 4 个全部 `None`，最后一个合法放行 |
| B 备份往返 | 建备份 → 列表出现 → 行数一致 → 删掉 | 文件字节数 > 0，行数与源库一致 |
| C 恢复 | 造一条脏数据 → 恢复 → 脏数据消失 + 自动快照存在 | 脏数据清零，`auto_before_restore_*` 生成 |
| D checkpoint | 有活跃事务时 TRUNCATE → 降级 PASSIVE 不抛异常 | `degraded=true` 且 `wal_after` 有值 |
| E 缓存 | 状态可读 → clear 后 size 归零 | `clearable` 项清理成功 |
| F 日志 | tail 行数正确、清空后 size=0、穿越被拒 | 三项全过 |
| G API 冒烟 | 12 个新路由全部 200/400 符合预期，非管理员 403 | 全过 |

---

## 6. 验收标准

1. `python3 tests/check_admin_maintain.py` 全绿。
2. 浏览器后台出现 9 个面板，数据库面板能看到 462MB / WAL 527KB / 12 张表行数。
3. 备份面板能下载、删除、恢复（恢复后自动快照存在）。
4. 服务重启后 e2e 回归无新增失败。

---

## 7. 实测记录（落地后的验证数据）

| 项 | 结果 |
|---|---|
| `server/maintain.py` 自检 | **38/38 通过**（路径白名单 13 项 + 备份往返 12 项 + checkpoint 降级 6 项 + 状态接口 7 项） |
| `tests/check_admin_maintain.py` | **54/54 通过**（12 个新路由 + 7 类穿越输入 + 备份下载/恢复/删除 + 日志运维） |
| 回归 | #95 `check_market_phase` 25/25、#96 `check_alerts` 33/33、`e2e_admin` 22/22、`check_newsfeed` 15/15 |
| 前端 | 5 个面板全部渲染正常，**控制台 0 错误** |
| WAL 收回 | 恢复前 464 MB → 自动截断后 **0 B** |

### 排查阶段的两个「没想到」，都被测试逼出来了

1. **PASSIVE checkpoint 也会抛 `database table is locked`**
   初版只给 TRUNCATE 做了降级，实测发现 PASSIVE 同样会抛。改成：
   TRUNCATE/FULL 被拒 → 降级 PASSIVE → PASSIVE 再被拒 → 返 `ok=false, busy=true` + 说明，
   **接口永不 500**。

2. **空的 `sqlite3.connect()` 不会真正打开文件**
   自检里「留一个占位连接防止 WAL 被自动清理」一度失效——连接建了但没发过语句，
   SQLite 根本没打开 db，`w.close()` 就被当成最后一个连接、直接把 WAL checkpoiont
   并删掉。加一句 `PRAGMA journal_mode` 查询后正常。
   顺带确认：**最后一个连接关闭时 SQLite 会自动 checkpoint 并删除 `-wal` 文件**，
   所以「WAL 有 500KB」在没人连的时候根本看不出来——这也是当初 228MB 僵死只能靠
   shell 发现的原因之一。

3. **checkpoint 只被活跃读事务阻塞，写事务不阻塞它**（测试据此重写）：
   占住 RESERVED 写锁时 TRUNCATE 照常成功；要测冲突得用 `BEGIN` + `SELECT` 开读事务。

### 前端截图

见 `shots_admin97/adm_{database,modules,services,backup,logs}.png`。
