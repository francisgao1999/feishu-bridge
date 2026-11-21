# 工作区约定

这是你的专属工作区 `/workspace`。你通过飞书和 OpenCode 交互，容器内以 root 运行。

## 目录

- `/workspace`：工作目录，代码和文件都放这里
- `/workspace/uploads/`：用户从飞书发来的文件会放在这里（桥接器写入）
- `/workspace/outbox/`：**要回传给用户的文件必须写到这里**

## 回传文件

桥接器会在任务结束后扫描 `/workspace/outbox/`，把新文件发回飞书：

- 图片（png/jpg/jpeg/gif/webp/bmp）≤ 10 MB：以图片消息发送
- 其他文件 ≤ 30 MB：以文件消息发送
- 超过上限不会发送，只在聊天里提示；请压缩或改用链接

示例：

```bash
# Python 生成图表（python3 已预装）
python3 -c "import matplotlib" 2>/dev/null || apt-get install -y python3-matplotlib
python3 chart.py /workspace/outbox/chart.png
```

需要浏览器截图时，用已配置好的 Playwright MCP 工具（`browser_navigate`、`browser_take_screenshot` 等），不要自己 `apt install nodejs` 或 `npm install playwright`：

1. `browser_navigate` 打开目标页
2. `browser_take_screenshot`，`filename` 写 `/workspace/outbox/shot.png`（MCP 默认输出目录也是 `/workspace/outbox`）
3. 截图写完，桥接器会在任务结束后自动回传

也可以直接用命令行截图（Playwright 已全局安装）：

```bash
playwright screenshot --viewport-size=1280,800 https://example.com /workspace/outbox/shot.png
```

Chromium 已预装在 `/root/.cache/ms-playwright`，**不要重复执行 `playwright install`**。如果 MCP 或命令行提示浏览器未安装，先检查 `echo $PLAYWRIGHT_BROWSERS_PATH` 是否指向 `/root/.cache/ms-playwright`；路径不对时找管理员，不要直接重新下载（会多占约 700MB）。

不要重复写同名文件——桥接器按内容 sha256 去重，内容相同只发一次。

## MCP 工具

模板已配置以下 MCP 服务器，需要时直接调用：

- `playwright`：浏览器导航、截图；
- `ast_grep`：结构化代码搜索；
- `context7`：查库文档；
- `exa`：网页搜索与研究；
- `grep_app`：搜索 GitHub 公开代码。

远程 MCP（`context7`、`exa`）需要出网，已随 opencode 进程走代理。不要自己改 MCP 配置或往 `opencode.jsonc` 里写密钥。

## 安装软件

容器内是 root，直接 `apt install`：

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y postgresql
```

默认没有预装数据库。需要时自己装，用 `systemctl` 管理，重启会自动启动。

## 长任务

- 大 SQL 导入用 `psql -f` / `mysql < file`，不要 `cat` 到上下文里
- 单条命令超过 10 分钟请分片执行，避免任务超时
- 任务超时上限 7200 秒（2 小时）

## 网络与代理

容器不能直连外网，所有出站流量必须走代理。代理已经配置好，不要删改：

- `HTTP_PROXY` / `HTTPS_PROXY`：`http://192.168.110.11:10808`
- `ALL_PROXY`：`socks5h://192.168.110.11:10808`
- `NO_PROXY`：`localhost`、`127.0.0.1` 和私有网段，内网直连
- 访问用户的客户生产环境时，可用本机 SOCKS5 代理 `127.0.0.1:40808`；需要用户先在 meshloom 中手动建立连接。

opencode 服务启动时已注入这些变量，它执行的命令会自动继承，`curl`、`git`、`pip`、`npm` 一般不需要额外参数。

如果某个工具不认环境变量，显式指定：

```bash
curl -x "$HTTPS_PROXY" https://example.com
git -c http.proxy="$HTTPS_PROXY" clone https://github.com/xxx/yyy.git
pip install --proxy "$HTTPS_PROXY" requests
npm config set proxy "$HTTP_PROXY" --location=global
```

`apt` 已写入 `/etc/apt/apt.conf.d/99proxy`，直接 `apt install` 即可。

- 不要在命令里写死代理地址，统一用 `$HTTP_PROXY` / `$HTTPS_PROXY` / `$ALL_PROXY`
- 不要把代理变量清空或 `unset`；确需直连内网时用 `NO_PROXY`
- 代理配置属于管理员维护，修改请找管理员改 `/etc/opencode/proxy.env`

## 不要做的事

- 不要修改 `/etc/opencode/` 下的配置（含 `proxy.env`）
- 不要关闭或重启 `opencode-serve` 服务
- 不要删除 `/workspace/uploads/` 和 `/workspace/outbox/`
