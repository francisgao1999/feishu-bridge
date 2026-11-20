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

需要浏览器截图时先装 Node.js（模板默认不装）：

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get install -y nodejs npm
npx playwright screenshot --viewport-size=1280,800 https://example.com /workspace/outbox/shot.png
```

不要重复写同名文件——桥接器按内容 sha256 去重，内容相同只发一次。

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

## 不要做的事

- 不要修改 `/etc/opencode/` 下的配置
- 不要关闭或重启 `opencode-serve` 服务
- 不要删除 `/workspace/uploads/` 和 `/workspace/outbox/`
