# 参考与许可

本插件在研究现有 AstrBot 插件后实现，未整体打包任何已有插件。

- [proactive_care](https://github.com/yuntaojinghong/astrbot_plugin_proactive_care)，AGPL-3.0：参考其低成本闸门、会话与日期确定性随机时刻、无人回应退避的设计。调度代码独立重写，未复制该项目源代码；原插件跟进的是机器人提问无人回答，本插件另外实现用户话题的延时跟进。
- [hotboard](https://github.com/qdie1546-source/astrbot_plugin_hotboard)，MIT：参考公开热榜 API 的 aiohttp 接入与字段归一化方案；请求、筛选和缓存代码重新实现。未采用其源代码内的凭据。为保留出处，附原 MIT 声明如下。
- [bilibili_ai_bot](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot)，MIT：参考主动推荐与人格短评的产品思路，不使用账号托管、点赞或评论功能，未复制源码。
- [netease_music](https://github.com/xiaoyunsha/astrbot_plugin_netease_music)：参考网易云歌曲链接和榜单接入方式，未使用其播放、下载或签名模块。
- [minimax_music](https://github.com/byydzh/astrbot_plugin_minimax_music)：研究远程歌曲生成协议；本插件根据公开 API 协议独立实现可选适配器，无默认服务、密钥或模型。
- [nai_brush](https://github.com/exynos967/astrbot_plugin_nai_brush)：对照其自定义 NAI GET 网关、API 地址与 Token 配置方式，未复制源码或预设。
- [ppnai](https://github.com/dafeiwu666/astrbot_plugin_ppnai)，MIT：研究 NovelAI 官方 JSON 请求与 ZIP 返回协议、Opus 模式和额度管理；本插件未复制源码，暂不实现该官方 ZIP 协议。
- [seedream_image](https://github.com/MarcoHuanxing/astrbot_plugin_seedream_image)：对照火山方舟接口及限流思路，再按公开 API 文档独立实现单张文生图适配，未复制源码。

## hotboard 原许可

MIT License

Copyright (c) 2026 qdie1546-source

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
