# 评见

把一份抖音评论导出，读成总览、内容机会和评论明细。

上传 CSV 或 XLSX。空内容、非一级评论、没有平台评论 ID 的行会在清洗时丢掉。留下来的评论按四类归好：明确需求、常见问题、消极反馈、内容灵感。页面上对应三块结果：这条视频的评论规模和主题、按分数列出的选题、可以筛选的评论原文。

浏览器负责登录、导入和展示。清洗、调用模型和写报告在 FastAPI 里完成。DeepSeek 做定位、分类和选题，Python 做计数、门禁和证据校验。DeepSeek Harness（DSH）按顺序调用分析工具，模型的思考文本不会进页面。

在线使用：<https://pingjian.rachelzhang.cn>

## 本地运行

需要 Docker，以及 Node.js 20.19+ 或 22.12+。分析服务跑在容器里，因为 DSH 需要 Python 3.10+，以及 Linux 或较新的 macOS。

```bash
cp .env.example .env
```

在 `.env` 里填上这三项，模型名可以保持默认：

| 变量 | 作用 |
| --- | --- |
| `CLOUDBASE_ENV_ID` | 腾讯云 CloudBase 环境 |
| `CLOUDBASE_API_KEY` | 仅服务端使用的 CloudBase 密钥 |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-flash` |

`.env` 不会进仓库。缺密钥时服务仍能启动，`GET /api/health` 的 `missing` 会写出缺的名字。导入和分析要等密钥补齐。

CloudBase 控制台需要先打开邮箱验证码登录。评论、任务和报告按登录用户隔开，浏览器不保存管理员密钥。

然后在两个终端里分别启动：

```bash
docker compose up --build
npm install
npm run dev
```

浏览器打开 Vite 的地址，一般是 <http://localhost:5173>。开发服务器把 `/api` 代理到本机 `127.0.0.1:8787`。容器的 8787 只绑在本机，不直接对公网开放。

使用顺序：邮箱收取验证码并登录，导入一份评论导出，看清洗摘要，确认后等分析跑完。同一进程同时只分析一条视频，其余任务排队。成功后可以改选题文字、收藏，并导出 Markdown 或 CSV。同一条视频已导入过时，需要先删除再重新导入。

## 测试

测试使用内存库和脚本模型，不连接 CloudBase，也不调用真实模型。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s server/tests -v
```

样例评论表在 `files/`。

## 目录

```
src/                         登录、导入、总览、内容机会、评论明细
server/                      FastAPI、清洗、分析、导出
dsh-plugin/pingjian-tools/   DSH 调用的分析工具
docker/                      容器启动脚本
deploy/                      Ubuntu 上的 Nginx 与 HTTPS
docs/                        方案和实现笔记
```

## 部署

计算放在一台一直开着的 Linux 服务器上，页面和 `/api` 走同一个域名。把代码和填好的 `.env` 放到服务器后：

```bash
sudo bash deploy/setup.sh 你的域名
```

脚本会安装 Docker 与 Nginx、构建页面、启动唯一的 API 容器，并用 Certbot 申请证书。安全组只放行 22、80、443。
