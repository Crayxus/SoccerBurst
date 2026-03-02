"""
SoccerBurst - Flask Web 服务
提供本地网页界面，每5分钟自动扫描一次盘口数据
"""

import asyncio
import json
import os
import threading
import time
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template, Response
from scraper import scan_all_matches, save_results

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
SCAN_INTERVAL = 300  # 5分钟 = 300秒

# 全局状态
scan_status = {
    "is_scanning": False,
    "last_scan": None,
    "next_scan": None,
    "scan_count": 0,
    "error": None
}


def load_data() -> dict:
    """读取最新扫描数据"""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取数据文件失败: {e}")
    return {
        "last_updated": None,
        "total_matches": 0,
        "alert_count": 0,
        "matches": []
    }


def run_scan():
    """在后台线程中执行扫描"""
    global scan_status

    if scan_status["is_scanning"]:
        logger.info("扫描已在进行中，跳过本次")
        return

    scan_status["is_scanning"] = True
    scan_status["error"] = None
    logger.info("开始扫描...")

    try:
        # 在新的事件循环中运行异步扫描
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(scan_all_matches())
        loop.close()

        save_results(results)
        scan_status["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        scan_status["scan_count"] += 1

        alerts = [r for r in results if r.get("alert")]
        logger.info(f"扫描完成：{len(results)} 场比赛，{len(alerts)} 场报警")

    except Exception as e:
        scan_status["error"] = str(e)
        logger.error(f"扫描失败: {e}")
    finally:
        scan_status["is_scanning"] = False
        scan_status["next_scan"] = datetime.fromtimestamp(
            time.time() + SCAN_INTERVAL
        ).strftime("%Y-%m-%d %H:%M:%S")


def background_scheduler():
    """后台定时扫描线程"""
    logger.info("后台扫描调度器启动")

    # 启动时立即扫描一次
    run_scan()

    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()


# ─────────────────────────────────────────────
# Flask 路由
# ─────────────────────────────────────────────

@app.route("/")
def index():
    """主页"""
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    """返回最新扫描数据"""
    data = load_data()
    data["scan_status"] = scan_status
    return jsonify(data)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """手动触发扫描"""
    if scan_status["is_scanning"]:
        return jsonify({"success": False, "message": "扫描正在进行中，请稍候..."})

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "扫描已启动"})


@app.route("/api/status")
def api_status():
    """返回扫描状态"""
    return jsonify(scan_status)


@app.route("/api/stream")
def api_stream():
    """SSE 实时推送（用于前端自动刷新）"""
    def generate():
        last_update = None
        while True:
            data = load_data()
            current_update = data.get("last_updated")
            if current_update != last_update:
                last_update = current_update
                payload = json.dumps({
                    "data": data,
                    "status": scan_status
                }, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            time.sleep(5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    # 启动后台扫描线程
    scheduler_thread = threading.Thread(target=background_scheduler, daemon=True)
    scheduler_thread.start()

    logger.info("SoccerBurst 启动！访问 http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
