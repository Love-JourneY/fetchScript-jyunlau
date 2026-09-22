# 第三方组件

本项目的**自有代码**以 **AGPL-3.0** 发布（见 `LICENSE`）。以下是运行时用到、但不属于本仓库的组件：

| 组件 | 用途 | 许可 | 与本项目的关系 |
|---|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 通用下载引擎 | Unlicense | **外部命令**调用（未内嵌代码） |
| [lux](https://github.com/iawia002/lux) | 国内平台下载引擎 | MIT | **二进制随包**（`vendor/lux`），许可证随包保留 |
| [Playwright](https://github.com/microsoft/playwright) + Chromium | 浏览器嗅探 | Apache-2.0 | 外部 Node 依赖调用 |
| [Node.js](https://nodejs.org/) | 跑嗅探脚本 | MIT | 外部运行时 |
| [bili2text](https://github.com/lanbinleo/bili2text)（我们的 fork） | 视频转文字（Qwen3-ASR） | MIT | **外部命令**调用（`bili2text tx`） |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) | 语音识别模型 | Apache-2.0 | 由 b2t 加载 |

说明：MIT/Unlicense/Apache-2.0 与 AGPL-3.0 兼容；本项目只**调用**它们，不复制其源码（`vendor/lux` 是原样二进制分发，保留其 MIT 许可）。
