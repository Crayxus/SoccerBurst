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
import re
import threading
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, request
# scraper 函数在本地模式下惰性导入，避免云端因缺少依赖（DrissionPage等）崩溃

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
SCAN_INTERVAL = 300  # 5分钟 = 300秒

# 从环境变量读取配置
PUSH_SECRET = os.environ.get("PUSH_SECRET", "soccerburst2026")
MODE = os.environ.get("MODE", "cloud")  # "cloud" 或 "local"
RENDER_URL = os.environ.get("RENDER_URL", "https://soccerburst.onrender.com")

# 全局状态
scan_status = {
    "is_scanning": False,
    "last_scan": None,
    "next_scan": None,
    "scan_count": 0,
    "error": None,
    "mode": MODE
}


def _push_data_to_render(data: dict):
    """将 data.json 内容立即推送到 Render 云端（后台调用）"""
    if MODE != "local":
        return
    url = f"{RENDER_URL.rstrip('/')}/api/push"
    try:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Push-Secret": PUSH_SECRET,
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                logger.info(f"✅ bet365数据已推送到云端")
            else:
                logger.warning(f"云端推送失败: {result.get('message')}")
    except Exception as e:
        logger.warning(f"云端推送异常（非致命）: {e}")


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
        from scraper import scan_all_matches, save_results  # 仅本地模式需要
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


# ─────────────────────────────────────────────
# bet365 历史记录接口
# ─────────────────────────────────────────────
BET365_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet365_history.json")


def load_bet365_history() -> list:
    if os.path.exists(BET365_HISTORY_FILE):
        try:
            with open(BET365_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []


@app.route("/api/history")
def api_history():
    """获取 bet365 历史记录"""
    history = load_bet365_history()
    # 最新的排在前面
    history = list(reversed(history))
    return jsonify({"history": history, "total": len(history)})


@app.route("/api/history/push", methods=["POST"])
def api_history_push():
    """接收本地推送的 bet365 历史记录（云端模式）"""
    secret = request.headers.get("X-Push-Secret", "")
    if secret != PUSH_SECRET:
        return jsonify({"success": False, "message": "Invalid secret"}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "No data"}), 400

        with open(BET365_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"收到 bet365 历史记录推送：{len(data)} 条")
        return jsonify({"success": True, "message": f"Received {len(data)} records"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/history/set_url", methods=["POST"])
def api_history_set_url():
    """保存 bet365 比赛链接到历史记录"""
    try:
        data = request.get_json()
        match_key = data.get("match_key")
        bet365_url = data.get("bet365_url", "").strip()

        if not match_key:
            return jsonify({"success": False, "message": "缺少 match_key"}), 400

        history = load_bet365_history()
        found = False
        for record in history:
            if record.get("match_key") == match_key:
                record["bet365_url"] = bet365_url
                record["url"] = bet365_url
                found = True
                break

        if not found:
            return jsonify({"success": False, "message": f"未找到记录: {match_key}"}), 404

        with open(BET365_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        logger.info(f"保存 bet365 链接: {match_key} -> {bet365_url}")
        return jsonify({"success": True, "message": "链接已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/history/fetch_now", methods=["POST"])
def api_history_fetch_now():
    """立即抓取指定比赛的 bet365 赔率（使用 DrissionPage）"""
    try:
        data = request.get_json()
        match_key = data.get("match_key")
        home = data.get("home", "")
        away = data.get("away", "")
        bet365_url = data.get("bet365_url", "").strip()

        if not match_key or not bet365_url:
            return jsonify({"success": False, "message": "缺少必要参数"}), 400

        # 先保存链接
        history = load_bet365_history()
        target_record = None
        for record in history:
            if record.get("match_key") == match_key:
                record["bet365_url"] = bet365_url
                record["url"] = bet365_url
                target_record = record
                break

        if not target_record:
            return jsonify({"success": False, "message": f"未找到记录: {match_key}"}), 404

        # 使用 DrissionPage 抓取（同步方式）
        from scraper import fetch_bet365_asian_handicap_drission, DRISSION_AVAILABLE
        if not DRISSION_AVAILABLE:
            return jsonify({"success": False, "message": "DrissionPage 未安装，请运行: pip install DrissionPage"}), 500

        logger.info(f"立即抓取 bet365: {home} vs {away} -> {bet365_url}")
        result = fetch_bet365_asian_handicap_drission(bet365_url, home, away)

        # 更新历史记录
        if result.get("found") or result.get("handicaps"):
            target_record.update({
                "handicaps": result.get("handicaps", []),
                "found": result.get("found", False),
                "url": bet365_url,
                "bet365_url": bet365_url,
                "scraped_at": result.get("scraped_at", ""),
                "error": result.get("error", ""),
            })
            if result.get("home"):
                target_record["home"] = result["home"]
            if result.get("away"):
                target_record["away"] = result["away"]
        else:
            target_record["error"] = result.get("error", "抓取失败")
            target_record["bet365_url"] = bet365_url
            target_record["url"] = bet365_url

        with open(BET365_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        handicap_count = len(result.get("handicaps", []))
        if result.get("found") or handicap_count > 0:
            logger.info(f"立即抓取成功: {match_key}, {handicap_count} 个盘口")
            return jsonify({"success": True, "message": f"抓取成功", "handicap_count": handicap_count})
        else:
            logger.warning(f"立即抓取失败: {match_key}, {result.get('error', '')}")
            return jsonify({"success": False, "message": result.get("error", "抓取失败，请检查链接是否正确")})

    except Exception as e:
        logger.error(f"立即抓取失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/bet365/fetch_direct", methods=["POST"])
def api_bet365_fetch_direct():
    """直接抓取 bet365 赔率并更新 data.json（本地模式，无需历史记录）"""
    try:
        req_data = request.get_json()
        bet365_url = req_data.get("bet365_url", "").strip()
        home = req_data.get("home", "")
        away = req_data.get("away", "")
        match_id = req_data.get("match_id", "")

        if not bet365_url:
            return jsonify({"success": False, "message": "缺少 bet365_url"}), 400

        from scraper import fetch_bet365_asian_handicap_drission, DRISSION_AVAILABLE
        if not DRISSION_AVAILABLE:
            return jsonify({"success": False, "message": "DrissionPage 未安装，bet365抓取仅支持本地模式（运行 pip install DrissionPage）"}), 500

        logger.info(f"直接抓取 bet365: {home} vs {away} -> {bet365_url}")
        result = fetch_bet365_asian_handicap_drission(bet365_url, home, away)

        handicaps = result.get("handicaps", [])
        success = result.get("found", False) or len(handicaps) > 0

        # 如果提供了 match_id，同步更新 data.json
        if match_id and success:
            try:
                data_content = load_data()
                for m in data_content.get("matches", []):
                    if m.get("match_id") == match_id:
                        m["bet365_handicaps"] = handicaps
                        m["bet365_url"] = bet365_url
                        break
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(data_content, f, ensure_ascii=False, indent=2)
                # 本地模式：FETCH 成功后立即推送到云端（后台线程，不阻塞响应）
                if MODE == "local":
                    threading.Thread(target=_push_data_to_render, args=(data_content,), daemon=True).start()
            except Exception as e:
                logger.warning(f"更新data.json失败: {e}")

        return jsonify({
            "success": success,
            "handicaps": handicaps,
            "scraped_at": result.get("scraped_at", ""),
            "error": result.get("error", "")
        })

    except Exception as e:
        logger.error(f"直接抓取bet365失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/result", methods=["POST"])
def api_result():
    """
    更新比赛结果（比赛结束后手动输入）
    请求体: {"match_key": "2026-03-04_纽卡斯尔_曼联", "home_score": 1, "away_score": 0}
    """
    try:
        data = request.get_json()
        match_key = data.get("match_key")
        home_score = data.get("home_score")
        away_score = data.get("away_score")

        if not match_key or home_score is None or away_score is None:
            return jsonify({"success": False, "message": "缺少必要参数"}), 400

        history = load_bet365_history()
        found = False
        for record in history:
            if record.get("match_key") == match_key:
                # 计算哪些盘口赢了
                winning_handicaps = calculate_winning_handicaps(
                    record.get("handicaps", []),
                    record.get("home", ""),
                    home_score, away_score
                )
                record["result"] = {
                    "home_score": home_score,
                    "away_score": away_score,
                    "settled": True,
                    "winning_handicaps": winning_handicaps
                }
                found = True
                break

        if not found:
            return jsonify({"success": False, "message": f"未找到比赛记录: {match_key}"}), 404

        with open(BET365_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        logger.info(f"更新比赛结果: {match_key} -> {home_score}:{away_score}")
        return jsonify({"success": True, "message": "结果已更新", "winning_handicaps": winning_handicaps})

    except Exception as e:
        logger.error(f"更新结果失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


def calculate_winning_handicaps(handicaps: list, home_team: str,
                                  home_score: int, away_score: int) -> list:
    """
    根据比赛结果计算哪些亚让盘口赢了
    亚让盘规则：主队让球数 + 主队实际进球 vs 客队实际进球
    正数盘口 = 主队让球（主队强），负数 = 主队受让（客队强）
    """
    winning = []
    goal_diff = home_score - away_score  # 主队净胜球

    for h in handicaps:
        home_hc_str = h.get("home_handicap", "")
        away_hc_str = h.get("away_handicap", "")
        home_odds = h.get("home_odds", 0)
        away_odds = h.get("away_odds", 0)

        # 解析盘口（可能是 "-1.0" 或 "-0.5, -1.0" 格式）
        try:
            # 取第一个数字作为主要盘口
            hc_nums = re.findall(r'[-+]?\d+\.?\d*', home_hc_str)
            if not hc_nums:
                continue

            # 亚让盘：主队净胜球 + 盘口 > 0 则主队赢
            # 如果是区间盘（如 -0.5,-1.0），分别计算两个盘口取平均
            results = []
            for hc_str in hc_nums[:2]:  # 最多取两个
                hc = float(hc_str)
                adjusted = goal_diff + hc  # 主队调整后净胜球
                if adjusted > 0:
                    results.append("home_win")
                elif adjusted < 0:
                    results.append("away_win")
                else:
                    results.append("push")  # 退水

            # 判断最终结果
            if all(r == "home_win" for r in results):
                winner = "home"
                winning.append({
                    "home_handicap": home_hc_str,
                    "home_odds": home_odds,
                    "away_handicap": away_hc_str,
                    "away_odds": away_odds,
                    "winner": "home",
                    "result": "主队赢"
                })
            elif all(r == "away_win" for r in results):
                winning.append({
                    "home_handicap": home_hc_str,
                    "home_odds": home_odds,
                    "away_handicap": away_hc_str,
                    "away_odds": away_odds,
                    "winner": "away",
                    "result": "客队赢"
                })
            elif "push" in results:
                winning.append({
                    "home_handicap": home_hc_str,
                    "home_odds": home_odds,
                    "away_handicap": away_hc_str,
                    "away_odds": away_odds,
                    "winner": "push",
                    "result": "退水"
                })
        except Exception as e:
            logger.debug(f"计算盘口胜负失败: {e}")
            continue

    return winning


# ─────────────────────────────────────────────
# Crayxus Signal 接口
# ─────────────────────────────────────────────

@app.route("/api/today_signal")
def api_today_signal():
    """分析今日最热比赛，返回推荐信号"""
    from analyzer import analyze_match, record_prediction, load_history, get_stats

    data = load_data()
    matches = data.get("matches", [])

    # 找热度最高且有 bet365 数据的比赛
    target = None
    for m in sorted(matches, key=lambda x: x.get("heat_score", 0), reverse=True):
        if m.get("bet365_handicaps") and m.get("ji_records"):
            target = m
            break

    if not target:
        # 退而求其次：热度最高的比赛（即使没 bet365）
        for m in sorted(matches, key=lambda x: x.get("heat_score", 0), reverse=True):
            if m.get("ji_records"):
                target = m
                break

    if not target:
        return jsonify({"error": "暂无可分析数据，请先扫描"})

    bet365_lines = target.get("bet365_handicaps", [])
    result = analyze_match(target, bet365_lines)

    if "error" not in result:
        match_key = f"{data.get('last_updated', '')[:10]}_{target.get('home')}_{target.get('away')}"
        record_prediction(match_key, result)
        result["match_key"] = match_key
        result["league"]     = target.get("league", "")
        result["match_time"] = target.get("match_time", "")
        result["heat_score"] = target.get("heat_score", 0)

    history = load_history()
    result["stats"]   = get_stats(history)
    result["history"] = list(reversed(history[-10:]))

    return jsonify(result)


@app.route("/api/signal_result", methods=["POST"])
def api_signal_result():
    """录入比赛结果"""
    from analyzer import record_result, load_history, get_stats

    data      = request.get_json()
    match_key  = data.get("match_key")
    home_score = data.get("home_score")
    away_score = data.get("away_score")

    if not match_key or home_score is None or away_score is None:
        return jsonify({"success": False, "message": "缺少参数"})

    rec   = record_result(match_key, int(home_score), int(away_score))
    stats = get_stats(__import__("analyzer").load_history())

    return jsonify({"success": True, "record": rec, "stats": stats})


@app.route("/api/signal_history")
def api_signal_history():
    """获取历史记录和统计"""
    from analyzer import load_history, get_stats

    history = load_history()
    return jsonify({
        "history": list(reversed(history)),
        "stats":   get_stats(history),
        "weights": __import__("analyzer").load_weights(),
    })


@app.route("/api/reanalyze")
def api_reanalyze():
    """对全部历史记录进行因子提炼分析"""
    from analyzer import load_history, reanalyze_history
    history = load_history()
    return jsonify(reanalyze_history(history))


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

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5100)), debug=False, use_reloader=False)
