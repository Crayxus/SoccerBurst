"""
SoccerBurst - 本地推送脚本
在本地运行，每5分钟扫描一次并将数据推送到 Render 云端

使用方法：
  python push_to_cloud.py

配置：
  修改下方 RENDER_URL 为你的 Render 服务 URL
  修改 PUSH_SECRET 与 Render 环境变量中的 PUSH_SECRET 一致
"""

import asyncio
import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime

# 修改为你的 Render URL（部署后从 Render 控制台获取）
RENDER_URL = os.environ.get("RENDER_URL", "https://soccerburst.onrender.com")
PUSH_SECRET = os.environ.get("PUSH_SECRET", "soccerburst2026")
SCAN_INTERVAL = 300  # 5分钟

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# 确保能导入 scraper
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import scan_all_matches, save_results


def push_bet365_history_to_cloud() -> bool:
    """将本地 bet365 历史记录推送到 Render 云端"""
    history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet365_history.json")
    if not os.path.exists(history_file):
        logger.info("bet365 历史记录文件不存在，跳过推送")
        return True

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        logger.error(f"读取 bet365 历史记录失败: {e}")
        return False

    url = f"{RENDER_URL.rstrip('/')}/api/history/push"
    payload = json.dumps(history, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Push-Secret": PUSH_SECRET,
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                logger.info(f"✅ bet365 历史记录推送成功：{len(history)} 条")
                return True
            else:
                logger.error(f"❌ bet365 历史推送失败：{result.get('message')}")
                return False
    except urllib.error.HTTPError as e:
        logger.error(f"❌ bet365 历史推送 HTTP错误 {e.code}: {e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"❌ bet365 历史推送网络错误: {e.reason}")
        return False
    except Exception as e:
        logger.error(f"❌ bet365 历史推送异常: {e}")
        return False


def push_data_to_cloud(data: dict) -> bool:
    """将扫描数据推送到 Render 云端"""
    url = f"{RENDER_URL.rstrip('/')}/api/push"
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Push-Secret": PUSH_SECRET,
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                logger.info(f"✅ 数据推送成功：{result.get('message')}")
                return True
            else:
                logger.error(f"❌ 推送失败：{result.get('message')}")
                return False
    except urllib.error.HTTPError as e:
        logger.error(f"❌ HTTP错误 {e.code}: {e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"❌ 网络错误: {e.reason}")
        return False
    except Exception as e:
        logger.error(f"❌ 推送异常: {e}")
        return False


async def scan_and_push():
    """扫描并推送数据"""
    logger.info("=" * 50)
    logger.info(f"开始扫描... {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        results = await scan_all_matches()

        # 保存到本地
        save_results(results)

        # 构建推送数据
        # 加载本地已有数据进行合并
        data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
        existing_data = {}
        if os.path.exists(data_file):
            try:
                with open(data_file, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    for m in old_data.get("matches", []):
                        if "match_id" in m:
                            existing_data[m["match_id"]] = m
            except Exception as e:
                logger.error(f"合并历史数据失败: {e}")

        # 合并新数据
        for r in results:
            if "match_id" in r:
                existing_data[r["match_id"]] = r

        final_matches = list(existing_data.values())
        final_matches.sort(key=lambda x: x.get("heat_score", 0), reverse=True)
        final_matches = final_matches[:50]  # 保留最多50场记录

        data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_matches": len(final_matches),
            "alert_count": sum(1 for r in final_matches if r.get("alert")),
            "matches": final_matches
        }

        # 同时更新本地的 data.json 保持同步
        try:
            with open(data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存本地合并数据失败: {e}")

        alerts = [r for r in results if r.get("alert")]
        logger.info(f"扫描完成：{len(results)} 场比赛，{len(alerts)} 场报警")

        if alerts:
            for a in alerts:
                logger.info(f"  ⚠️  {a['home']} vs {a['away']}: {a['alert_reason']}")

        # 推送到云端
        logger.info(f"推送数据到 {RENDER_URL}...")
        push_data_to_cloud(data)

        # 同步推送 bet365 历史记录到云端
        push_bet365_history_to_cloud()

    except Exception as e:
        logger.error(f"扫描失败: {e}")


def main():
    logger.info("SoccerBurst 本地推送模式启动")
    logger.info(f"云端地址: {RENDER_URL}")
    logger.info(f"扫描间隔: {SCAN_INTERVAL} 秒")
    logger.info("=" * 50)

    while True:
        asyncio.run(scan_and_push())
        next_time = datetime.fromtimestamp(time.time() + SCAN_INTERVAL)
        logger.info(f"下次扫描: {next_time.strftime('%H:%M:%S')}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
