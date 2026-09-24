# 源流 · fetchScript-jyunlau

> 品牌名：**fetchScript-jyunlau**（中文名 **源流**，粤拼 jyun4 lau4）· 技术标识：`fetchscript`（包名/命令/路径）

> **一句话**：把从 App 里复制出来的**整段分享文案**（带口令、短链、追踪参数）变成"本地文件"。
> 本模块**不自己写平台解析**——抖音/快手/小红书的风控签名是活的，自己养不划算；
> 它只做三件事：**归一化链接 → 按平台挑引擎 → 下载并分型报错**。

来源：`~/dev/fetchscript/`（开发场）→ 装到 `/opt/fetchscript/`（安装位）。
调研依据：`~/dev/fetchscript-research/README.md`（含本机实测：yt-dlp 无快手提取器、小红书提取器当前失效、抖音需 cookie）。

## 它是什么（一句话）

**贴一条平台分享链接 → 干净的本地视频 / 音频 / 文字稿。**
`fetchScript`（抓取）+ `jyunlau 源流`（源头活水）+ 中文「源流」三者同指一个东西：**从源头把资源取下来，顺手抄成文字。**

## 它为什么存在

| 问题 | 现状（本机实测 2026-09-21） | 本模块的对策 |
|---|---|---|
| 分享文案不是 URL | yt-dlp / lux 只吃干净 URL | `share_links.py`：抠 URL、去追踪参数、判平台、标出短链 |
| 单一引擎覆盖不全 | yt-dlp **没有快手提取器**；小红书提取器当前失效；抖音要 cookie | **双引擎路由**：yt-dlp（海外强）+ lux（B站/抖音/快手/小红书/微博） |
| 失败原因分不清 | 一律报错，看不出是"缺 cookie"还是"平台改接口" | 失败分型：`UnsupportedPlatform` / `NoEngineAvailable` / `EngineFailed` |
| 视频号做不到 | 无公开直链，只能装证书做 MITM 嗅探 | **明确拒绝**并说明原因，不给跑不通的方案 |

## 落位（FHS）

| 东西 | 位置 |
|---|---|
| 程序位（纯 stdlib，零第三方依赖） | `/opt/fetchscript/lib/fetchscript/` |
| 随包二进制 | `/opt/fetchscript/vendor/lux` |
| 启动器 | `/usr/local/bin/fetchscript` |
| 引擎路径配置 | `/etc/fetchscript/env` |
| 状态位（默认下载目录） | `/var/lib/fetchscript/` |

## 装 / 验 / 卸

```bash
cd ~/Documents/repo/fetchscript
sudo ./install.sh --dry-run     # 先看要做什么
sudo ./install.sh               # 幂等，可重复跑
bash ./verify.sh                # 通过/失败清单
sudo ./uninstall.sh             # 移除（状态位保留，--purge 连它一起删）
```

**引擎是可选后端**：`yt-dlp` 默认复用 b2t 那份（`/opt/bili2text/app/.venv/bin/yt-dlp`），
也可用 `FETCHSCRIPT_YTDLP` 指到别处；`lux` 用 `bash tools/get-lux.sh` 下载（多镜像 + sha256 校验）后随包安装。

## 平板接入（常驻服务）

装好后会起一个 systemd 服务，**监听 0.0.0.0:8901**（局域网可访问），平板浏览器直接开：

```
http://<笔记本IP>:8901/?k=<token>
```

- token 在 `/etc/fetchscript/token`（600，只有 root 与 systemd 能读）；**把它一起存成平板书签**，否则会 401。
- 页面：贴分享文案 → 「解析」（只探测）／「下载」（后台任务）→ 列表里点文件名下载。
- JSON API（同一 token）：`POST /api/resolve`、`POST /api/download`、`GET /api/jobs`、`GET /api/health`（免 token）。
- 并发上限 2 个下载任务；产物落 `/var/lib/fetchscript/media/`。
- 安全：**fail-closed**（读不到 token 就拒绝所有受保护接口）；`/files/` 有扩展名白名单 + 目录穿越防护。
- 换端口/IP：改 `/etc/fetchscript/env` 里的 `FETCHSCRIPT_PORT` / `FETCHSCRIPT_BIND`，`systemctl restart fetchscript`。

## 用法

```bash
fetchscript engines                # 本机装了哪些引擎 + 每个平台走哪条路
fetchscript plan "<分享文案>"       # 只判平台与路线（不联网）
fetchscript probe "<分享文案>"      # 取元信息（联网，不下载）
fetchscript download "<分享文案>" -o /var/lib/fetchscript/media --json
```

退出码：`0` 成功｜`2` 平台不可用｜`3` 引擎缺失｜`4` 引擎失败。

## 与 b2t 的关系（边界）

- **接口是文件，不是代码**：fetchscript 只管把媒体落到目录；b2t 用**现有**的 `本地文件` 入口吃它
  （`bili2text tx /path/to/video.mp4`），**b2t 不需要为多平台改任何代码**。
- **`share_links.py` 有两份副本**（b2t 与 fetchscript），`verify.sh` 会比对 sha256：
  **改一处必须同步另一处**，否则验收会红。这是刻意的"防漂移"设计，不是冗余。

## ✅ 已跑通（2026-09-22 实测，产物在人眼前验过）

| 平台 | 命令 | 结果 |
|---|---|---|
| **抖音** | `fetchscript download "<分享文案>" -o DIR` | ✅ `~/Videos/dy-test/杨超越-小小水手带你去远航.mp4`（6.0MB / 1080x1920 / 19.7s），抽帧确认**无水印** |
| **小红书** | 同上 | ✅ `~/Videos/xhs-test/噜噜和回声较上劲了---小红书.mp4`（1.5MB / 1280x720 / 13.9s），抽帧 + 用户目视确认**无水印** |

**关键实现细节（别人不会告诉你）**：
1. **真浏览器打开页面，让页面自己的 JS 去请求**，我们只**读响应** —— 不签名、不逆向。抖音的详情 JSON 里 `play_addr`（CDN 直链）才是无水印那一路；`download_addr` 反而带 `watermark=1`。
2. **小红书是 `blob:`+MSE 播放器**：URL 里根本没有可下载的直链 ⇒ **必须按响应头 `content-type: video/*` 抓媒体**（只按 `.mp4` 后缀抓必定全错过）。这条是小红书能不能成的分水岭。
3. **抖音要登录态，小红书不用**（实测：小红书登录墙只是弹窗，笔记照常播）。
4. 稳定性不保证：同一链接**时好时坏**，已内置多 UA + 3 次重试。

## 本机实测（2026-09-22，lux v0.24.1 / yt-dlp 2026.08.19，无 cookie）

| 平台 | yt-dlp | lux | 结论 |
|---|---|---|---|
| B 站 | ✅ 元数据秒回 | ✅ 元数据 + 清晰度列表 | **今天就能用** |
| 抖音 | ❌ `Fresh cookies ... needed` | ❌ `HTTP 403`（同一个 `aweme/v1/web/aweme/detail/` 端点，lux 签了 `X-Bogus` 仍被拦） | **需要 cookie/身份；两个引擎都不通** |
| 快手 | ❌ 无提取器 | ⚠️ 未实测（没有真实分享链接） | 待实测 |
| 小红书 | ❌ `No video formats found!` | ⚠️ 未实测 | 待实测（调研显示要 cookie） |
| 视频号 | — | — | 本模块明确拒绝 |

⇒ **别信"装上引擎就全能"**：抖音的门槛是 **Argus/uifid 风控**，不是"有没有引擎"。
真正可行的下一步是给它配 **cookies.txt**（`--cookies`），或用真浏览器（Camofox）兜底。

## 已知边界（诚实清单）

- **抖音/小红书/快手要能真下下来，取决于引擎自己跟平台风控的赛跑**；本模块不修这个。
- **视频号**：本模块拒绝；要做只能另立 MITM 模块（装根证书 + 常驻代理，参考 `putyy/res-downloader`、`ltaoo/wx_channels_download`）。
- **图文（小红书图集）**：lux/yt-dlp 都不擅长 ⇒ 另需 `gallery-dl` 或专用项目，本模块暂不覆盖。
- **未在本机验证**：lux 对抖音/快手/小红书的真实成功率（下载二进制后需逐平台实测）。


## 许可（自研 · AGPL-3.0）

- **本项目的自有代码以 AGPL-3.0 发布**（`LICENSE`）——Nija 2026-09-22 定调：**自研 + AGPL 开源**。
- 运行时调用的第三方组件（yt-dlp / lux / Playwright / b2t）许可见 `THIRD-PARTY.md`；它们只被**调用**，源码不内嵌。
- 意义：谁把这个服务摆到公网给别人用，就必须把改动也开源 —— 正是我们要的效果。

## 两个"合体"点（解析下载 + 视频转文字）

| 能力 | 怎么用 |
|---|---|
| 文件名=视频标题 | 下载产物统一按解析出的标题命名（`names.safe_title`，去非法字符、压空白、截断 80 字） |
| **三种模式，互不绑定** | 网页选「只要视频 / 视频+文字 / 只要文字」；CLI 用 `--transcribe`、`--text-only`、`--audio-only` |
| 只要文字（推荐给音频类内容） | `fetchscript download "<链接>" -o DIR --text-only` —— **只下音轨 → 转写 → 删掉媒体**，只留 `.txt`。理由：文字存储成本低、检索/引用效率高；视频内容本质是音频时，留视频是浪费 |
| 单独转文字（跟下载彻底解耦） | `fetchscript transcribe <本地文件>` —— 给任何音/视频文件出同名 `.txt`（调本机 b2t） |


## 致谢与引用

- **[bili2text](https://github.com/lanbinleo/bili2text)**（MIT，lanbinleo）—— 视频转文字这一半的设计起点。
  我们早期 fork 过它、在里面把运行时换成 Qwen3-ASR(ONNX)，后来**把它当外部命令调用**（`bili2text tx`）而不是继续分叉。
  本项目的"字幕优先 / 失败分型 / 中间物回收"等工程经验，也受它以及社区若干同类项目的启发。**感谢原作者与贡献者。**
- **yt-dlp / lux / Playwright** —— 下载与浏览器嗅探的引擎（见 `THIRD-PARTY.md`）。
- 调研阶段参考过的众多开源项目（抖音/小红书/视频号解析、VAD 与字幕工程等），其**精华已内化**为本项目自己的实现，
  为避免把别人的源码/文档留进本仓库，**参考资料已清理**。

> 本项目是**独立实现**：不包含也不分发上述项目的源码（`vendor/lux` 是原样二进制，保留其 MIT 许可）。
