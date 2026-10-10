# 金十数据 静态公开资源（provenance）

来源：从金十数据 APK 反编译报告 `data_sources.md` 提取的公开 CDN 资源（无需鉴权）。

| 文件 | 来源 URL | 说明 |
|------|----------|------|
| cates.json | https://cdn.jin10.com/json/index/cates.json | 金十 新闻/视频 栏目 taxonomy（317 个栏目，非行业指数） |
| tradetime.csv | https://cdn.jin10.com/jiaoyixia/config/tradeTimeConf/tradetime.csv | 品种→交易时段映射（A股/期货/外盘/贵金属） |
| dst_rule.csv | https://cdn.jin10.com/jiaoyixia/config/tradeTimeConf/dst_rule.csv | 外盘夏令时规则 |
| app_configs.json | https://cdn.jin10.com/app-configs.json | 应用配置（含 flashMarketNameTable 行情市场名、flash_menu 栏目） |

## 已落地到面板
- `server/sector_sentiment.py` 的 SECTOR_KEYWORDS 已并入金十 flashMarketNameTable 行情市场名词
  （布伦特原油/WTI原油→能源；现货黄金/伦敦金/现货白银/铂钯→有色；美元指数/欧元美元→金融）。

## 未使用（需 APK 或官方 API，暂不可达）
- `assets/astock_router/*.json`（A股板块路由/成分股）——APK 内打包资源，公开 CDN 404，无 APK 拿不到。
- 快讯流 flash / 实时行情 WS / REST——需 MitM 抓包 + 逆向 sign/token + 绕过 SSL Pinning，违反 ToS，未接入。
