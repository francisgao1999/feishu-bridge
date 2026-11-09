from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

import lark_oapi as lark
from lark_oapi.ws.client import loop as lark_loop

from bridge import Bridge
from config import load_config
from db import Database
from feishu import FeishuClient
from opencode import OpenCodeClient, OpenCodeSyncClient
from ws_patch import patch_ws_client


def setup_logging(log_path: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    for noisy in ("httpx", "httpcore", "Lark"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_path)
    log = logging.getLogger("main")
    log.info("starting feishu-opencode bridge")
    log.info("bridge home=%s data=%s", cfg.bridge_home, cfg.data_dir)

    db = Database(cfg.db_path)
    db.init()

    oc = OpenCodeClient(
        cfg.opencode_base_url,
        cfg.opencode_username,
        cfg.opencode_password,
    )
    oc_sync = OpenCodeSyncClient(
        cfg.opencode_base_url,
        cfg.opencode_username,
        cfg.opencode_password,
    )
    feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret)

    bridge = Bridge(cfg, db, oc, oc_sync, feishu)
    bridge.startup()

    patch_ws_client()

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(bridge.on_message)
        .register_p2_card_action_trigger(bridge.on_card_action)
        .build()
    )

    client = lark.ws.Client(
        cfg.feishu_app_id,
        cfg.feishu_app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

    lark_loop.create_task(bridge.sse_loop())
    lark_loop.create_task(bridge.permission_poll_loop())
    lark_loop.create_task(bridge.permission_expiry_loop())
    lark_loop.create_task(bridge.schedule_loop())
    lark_loop.create_task(bridge.task_watchdog_loop())
    lark_loop.create_task(bridge.notify_stale_tasks())

    try:
        client.start()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        try:
            lark_loop.run_until_complete(bridge.shutdown())
        except Exception:
            pass
        db.close()
        oc_sync.close()
        log.info("bridge stopped")


if __name__ == "__main__":
    main()
