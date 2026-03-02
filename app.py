"""
SoccerBurst - Flask Web 服务（云端展示模式）

架构说明：
  - 云端（Render）：只负责存储和展示数据，提供 /api/push 接口接收数据
  - 本地电脑：运行 scraper.py 抓取数据，通过 /api/push 推送到云端

环境变量：
  PUSH_SECRET  - 推送接口的密钥（本地和云端必须一致）
  MODE         - "cloud"（云端模式，不自动扫描）或 "local"（本地模式，自动扫描）
                 默认为 "cloud"（Render 上运行时）
"""

import asyncio
import json
import os
import threading
import time
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, request
from scraper import scan_all_matches, save_results

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
SCAN_INTERVAL = 300  # 5分钟 = 300秒

# 从环境变量读取配置
PUSH_SECRET = os.environ.get("PUSH_SECRET", "soccerburst2026")
MODE = os.environ.get("MODE", "cloud")  # "cloud" 或 "local"

# 全局状态
scan_status = {
    "is_scanning": False,
    "last_scan": None,
    "next_scan": None,
    "scan_count": 0,
    "error": None,
    "mode": MODE
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
    """在后台线程中执行扫描（仅本地模式使用）"""
    global scan_status

    if scan_status["is_scanning"]:
        logger.info("扫描已在进行中，跳过本次")
        return

    scan_status["is_scanning"] = True
    scan_status["error"] = None
    logger.info("开始扫描...")

    try:
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
    """后台定时扫描线程（仅本地模式）"""
    logger.info("后台扫描调度器启动（本地模式）")
    run_scan()
    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()


# ─────────────────────────────────────────────
# Flask 路由
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    data = load_data()
    data["scan_status"] = scan_status
    return jsonify(data)


@app.route("/api/push", methods=["POST"])
def api_push():
    """
    接收本地推送的扫描数据（云端模式专用）
    请求头需包含：X-Push-Secret: <PUSH_SECRET>
    请求体：JSON 格式的扫描结果
    """
    # 验证密钥
    secret = request.headers.get("X-Push-Secret", "")
    if secret != PUSH_SECRET:
        logger.warning(f"推送密钥错误，拒绝请求")
        return jsonify({"success": False, "message": "Invalid secret"}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "No data"}), 400

        # 保存数据
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 更新状态
        scan_status["last_scan"] = data.get("last_updated")
        scan_status["scan_count"] += 1
        scan_status["is_scanning"] = False

        match_count = data.get("total_matches", 0)
        alert_count = data.get("alert_count", 0)
        logger.info(f"收到推送数据：{match_count} 场比赛，{alert_count} 场报警")

        return jsonify({"success": True, "message": f"Received {match_count} matches"})

    except Exception as e:
        logger.error(f"处理推送数据失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """手动触发扫描（仅本地模式有效）"""
    if MODE == "cloud":
        return jsonify({"success": False, "message": "云端模式不支持手动扫描，请在本地运行 scraper.py"})

    if scan_status["is_scanning"]:
        return jsonify({"success": False, "message": "扫描正在进行中，请稍候..."})

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "扫描已启动"})


@app.route("/api/status")
def api_status():
    return jsonify(scan_status)


@app.route("/api/stream")
def api_stream():
    """SSE 实时推送"""
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
    if MODE == "local":
        # 本地模式：启动后台扫描线程
        scheduler_thread = threading.Thread(target=background_scheduler, daemon=True)
        scheduler_thread.start()
        logger.info("SoccerBurst 启动（本地模式）！访问 http://localhost:5000")
    else:
        logger.info(f"SoccerBurst 启动（云端模式）！等待本地推送数据...")
        logger.info(f"推送接口：POST /api/push  密钥：{PUSH_SECRET}")

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)
