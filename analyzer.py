"""
Crayxus Signal Analyzer
每日单注推荐引擎 - 因子分析 + 自我迭代
"""

import json
import os
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(BASE_DIR, "factor_weights.json")
HISTORY_FILE = os.path.join(BASE_DIR, "signal_history.json")

# 校准目标场次：积累到这个数才做权重提炼
CALIBRATION_TARGET = 30

# 初始因子权重（等权，待数据校准后自动调整）
DEFAULT_WEIGHTS = {
    "line_compression":      0.20,   # F1 盘口压缩方向（Crow* 主线漂移方向）
    "compression_magnitude": 0.15,   # F2 压缩幅度（漂移几步）
    "water_alignment":       0.15,   # F3 水位与盘口对比（同向 vs 背离）
    "drift_consistency":     0.10,   # F4 水位漂移连贯性（趋势一致程度）
    "reverse_signal":        0.20,   # F5 逆市背离强度（背离 = 聪明钱逆公众）
    "late_money":            0.12,   # F6 临场资金加速（最近几条记录的压力趋势）
    "handicap_level":        0.08,   # F7 盘口档位校正（受让方反弹信号更强）
}

# 盘口排序：index越小 = 主队越弱(受让越多)，越大 = 主队越强(让越多)
HANDICAP_RANK = {
    "受让两球":     0,
    "受让球半/两球": 1,
    "受让球半":     2,
    "受让半球/一球": 3,
    "受让半球":     4,
    "受让平手/半球": 5,
    "受让平手":     6,
    "平手":        7,
    "平手/半球":   8,
    "半球":        9,
    "半球/一球":   10,
    "一球":        11,
    "一球/球半":   12,
    "球半":        13,
    "球半/两球":   14,
    "两球":        15,
}


def get_handicap_rank(hc_str: str) -> int:
    for key, val in HANDICAP_RANK.items():
        if key in hc_str or hc_str in key:
            return val
    return -1


def load_weights() -> dict:
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_WEIGHTS.copy()


def save_weights(weights: dict):
    with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_history(history: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def analyze_match(match_data: dict, bet365_lines: list, weights: dict = None) -> dict:
    """
    核心分析函数
    Returns: { direction, direction_team, signal_type, best_line, confidence, factors }
    """
    if weights is None:
        weights = load_weights()

    all_records = match_data.get("all_records", [])
    home        = match_data.get("home", "")
    away        = match_data.get("away", "")

    # 使用早+即全部记录（排除开球后的滚球记录），按新→旧排列
    ji_records = [r for r in all_records if r.get("status") in ("早", "即")]

    if not ji_records or not bet365_lines:
        return {"error": "数据不足，无法分析", "home": home, "away": away}

    factors = {}

    # ── Factor 1: 盘口压缩方向 ──────────────────────────────
    # 找最早的"早"状态盘口（all_records 从新到旧排列，所以取最后一条早）
    early_hc = ""
    for r in reversed(all_records):
        if r.get("status") == "早":
            early_hc = r.get("handicap", "")
            break

    latest_hc   = ji_records[0].get("handicap", "") if ji_records else ""
    early_rank  = get_handicap_rank(early_hc)
    latest_rank = get_handicap_rank(latest_hc)

    if early_rank >= 0 and latest_rank >= 0:
        delta = latest_rank - early_rank
        # delta > 0: 主队地位上升（主队让球变多 or 受让减少）= 聪明钱押主队
        # delta < 0: 主队地位下降（主队让球变少 or 受让增加）= 聪明钱押客队
        compression_dir  = "home" if delta > 0 else "away" if delta < 0 else "neutral"
        compression_size = abs(delta)
    else:
        delta, compression_dir, compression_size = 0, "neutral", 0

    factors["line_compression"] = {
        "early_hc":  early_hc,
        "latest_hc": latest_hc,
        "delta":     delta,
        "direction": compression_dir,
        "size":      compression_size,
    }

    # ── Factor 2: 水位方向 & 与盘口的对齐度 ────────────────
    latest_rec  = ji_records[0]
    home_odds   = latest_rec.get("home_odds", 0.95)
    away_odds   = latest_rec.get("away_odds", 0.95)

    # home_odds < away_odds = 主队方资金更多（主队这边被压低）
    water_dir       = "home" if home_odds < away_odds else "away"
    water_imbalance = round(abs(away_odds - home_odds), 3)
    aligned         = (water_dir == compression_dir)

    factors["water"] = {
        "home_odds":  home_odds,
        "away_odds":  away_odds,
        "direction":  water_dir,
        "imbalance":  water_imbalance,
        "aligned":    aligned,
    }

    # ── Factor 3: 水位漂移一致性 ────────────────────────────
    if len(ji_records) >= 3:
        first_home   = ji_records[-1].get("home_odds", 0)
        last_home    = ji_records[0].get("home_odds", 0)
        overall_down = last_home < first_home   # home_odds 整体在下降

        same_dir    = 0
        total_pairs = 0
        for i in range(len(ji_records) - 1):
            c = ji_records[i].get("home_odds", 0)
            n = ji_records[i + 1].get("home_odds", 0)
            if c != n:
                total_pairs += 1
                pair_down = c < n
                if pair_down == overall_down:
                    same_dir += 1

        consistency  = round(same_dir / total_pairs, 2) if total_pairs > 0 else 0.5
        total_drift  = round(abs(last_home - first_home), 3)
    else:
        consistency = 0.5
        total_drift = 0.0

    factors["drift"] = {
        "consistency":  consistency,
        "total_drift":  total_drift,
        "records":      len(ji_records),  # 早+即合计
    }

    # ── Factor 5: 逆市背离强度 ──────────────────────────────
    # 盘口方向与水位方向相反 = 公众押一侧但庄家盘口往另一侧移 = 聪明钱逆市
    # 背离幅度越大、水位偏差越大 = 信号越强
    if not aligned and compression_dir != "neutral":
        reverse_score = min(compression_size / 3.0, 1.0) * min(water_imbalance / 0.10, 1.0)
    else:
        reverse_score = 0.0

    factors["reverse_signal"] = {
        "score":     round(reverse_score, 3),
        "diverging": not aligned and compression_dir != "neutral",
        "magnitude": round(compression_size * water_imbalance, 3),
    }

    # ── Factor 6: 临场资金加速 ──────────────────────────────
    # 最近2条记录的水位压力是否比均值更强，且方向一致
    if len(ji_records) >= 3:
        imbalances    = [abs(r.get("away_odds", 0.95) - r.get("home_odds", 0.95)) for r in ji_records]
        avg_imbal     = sum(imbalances) / len(imbalances)
        recent_imbal  = imbalances[0]
        acceleration  = recent_imbal / (avg_imbal + 0.001) - 1.0   # >0 = 临场压力上升
        # 检查最近2条是否方向一致
        last2_home    = [ji_records[i].get("home_odds", 0) for i in range(min(3, len(ji_records)))]
        last2_moves   = [last2_home[i] - last2_home[i+1] for i in range(len(last2_home)-1)]
        consistent_dir = all(m < 0 for m in last2_moves) or all(m > 0 for m in last2_moves)
        late_score    = min(max(acceleration, 0.0), 1.0) * (1.2 if consistent_dir else 0.8)
        late_score    = min(late_score, 1.0)
    else:
        recent_imbal, avg_imbal, late_score = water_imbalance, water_imbalance, 0.3

    factors["late_money"] = {
        "score":          round(late_score, 3),
        "recent_imbal":   round(recent_imbal, 3),
        "avg_imbal":      round(avg_imbal, 3),
        "accelerating":   late_score > 0.4,
    }

    # ── Factor 7: 盘口档位校正 ──────────────────────────────
    # 受让方（弱队）的盘口向强队方向压缩 = 市场在重新评估弱队 = 信号更可信
    # 让球方（强队）的盘口继续向强队方向压缩 = 公众追热门 = 信号可信度打折
    pivot_val = crow_hc_to_numeric(latest_hc)
    if compression_dir == "home":
        # home 受让（正值）= home 是弱队，弱队压缩更可信
        level_score = 1.0 if pivot_val >= 0.25 else (0.5 if pivot_val > -0.25 else 0.25)
    elif compression_dir == "away":
        # away 受让（pivot负值）= away 是弱队，弱队压缩更可信
        level_score = 1.0 if pivot_val <= -0.25 else (0.5 if pivot_val < 0.25 else 0.25)
    else:
        level_score = 0.5

    factors["handicap_level"] = {
        "pivot_val":   pivot_val,
        "score":       round(level_score, 2),
        "context":     "弱队反压" if level_score >= 1.0 else ("均势" if level_score >= 0.5 else "强队顺势"),
    }

    # ── 综合决策 ────────────────────────────────────────────
    if compression_dir == "neutral":
        bet_direction = water_dir
        signal_type   = "水位跟随"
    else:
        bet_direction = compression_dir
        if not aligned:
            signal_type = "背离爆冷"
        else:
            signal_type = "同向确认"

    # ── 信心指数（7因子加权）────────────────────────────────
    s_compression = min(compression_size / 4.0, 1.0) * weights["line_compression"]
    s_magnitude   = min(compression_size / 3.0, 1.0) * weights["compression_magnitude"]
    # 背离时满分，同向时 0.6（同向可能是公众钱）
    s_alignment   = (1.0 if not aligned else 0.6) * min(water_imbalance / 0.15, 1.0) * weights["water_alignment"]
    s_drift       = consistency * weights["drift_consistency"]
    s_reverse     = reverse_score * weights["reverse_signal"]
    s_late        = late_score    * weights["late_money"]
    s_level       = level_score   * weights["handicap_level"]

    confidence = min(int((s_compression + s_magnitude + s_alignment + s_drift +
                          s_reverse + s_late + s_level) * 100), 95)

    # ── 预测全部盘口 & 最佳线 ────────────────────────────────
    predicted_lines = predict_winning_lines(bet365_lines, bet_direction, latest_hc, confidence)
    best_line       = pick_best_line(bet365_lines, bet_direction, latest_hc, confidence)
    crow_pivot      = crow_hc_to_numeric(latest_hc)

    win_count  = sum(1 for p in predicted_lines if
                     (bet_direction == "home" and p["home_predicted"]) or
                     (bet_direction == "away" and p["away_predicted"]))

    return {
        "home":             home,
        "away":             away,
        "direction":        bet_direction,
        "direction_team":   home if bet_direction == "home" else away,
        "signal_type":      signal_type,
        "best_line":        best_line,
        "confidence":       confidence,
        "factors":          factors,
        "weights_used":     weights,
        "predicted_lines":  predicted_lines,
        "crow_pivot":       crow_pivot,
        "predicted_win_count": win_count,
        "total_lines":      len(predicted_lines),
        "analyzed_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


CROW_HC_NUMERIC = {
    "受让两球":      2.0,
    "受让球半/两球":  1.75,
    "受让球半":      1.5,
    "受让半球/一球":  0.75,
    "受让半球":      0.5,
    "受让平手/半球":  0.25,
    "受让平手":      0.0,
    "平手":         0.0,
    "平手/半球":    -0.25,
    "半球":        -0.5,
    "半球/一球":   -0.75,
    "一球":        -1.0,
    "一球/球半":   -1.25,
    "球半":        -1.5,
    "球半/两球":   -1.75,
    "两球":        -2.0,
}


def crow_hc_to_numeric(hc_str: str) -> float:
    """Crow* 盘口字符串 → 数值（主队视角，正数=主队受让，负数=主队让球）"""
    for key, val in CROW_HC_NUMERIC.items():
        if key in hc_str or hc_str in key:
            return val
    return 0.0


def bet365_hc_to_numeric(hc_str: str) -> float:
    """
    bet365 盘口字符串 → 数值（主队视角）
    '0.0, +0.5' → 0.25,  '+1.0' → 1.0,  '-0.5, -1.0' → -0.75
    """
    import re
    nums = re.findall(r'[+-]?\d+\.?\d*', hc_str)
    if not nums:
        return 0.0
    vals = [float(n) for n in nums[:2]]
    return sum(vals) / len(vals)


def predict_winning_lines(bet365_lines: list, direction: str, crow_hc: str, confidence: int = 55) -> list:
    """
    对每条 bet365 盘口线计算概率并打标签。

    原理：
    - 用市场赔率反推各盘口隐含概率（去除庄家利润后归一化）
    - 在隐含概率基础上叠加信号方向的调整量（信心越高调整越大）
    - 预测赢 = 调整后概率 > 50%
    - 期望值 EV = 调整后概率 × 赔率 - 1（>0 = 正期望）
    """
    pivot         = crow_hc_to_numeric(crow_hc)
    # 信号强度：confidence 50% = 无信号，95% = 最强，对应最多 ±12% 概率偏移
    signal_shift  = ((confidence - 50) / 50.0) * 0.12
    result        = []

    for line in bet365_lines:
        home_hc_str  = line.get("home_handicap", "")
        away_hc_str  = line.get("away_handicap", "")
        home_odds    = line.get("home_odds", 0)
        away_odds    = line.get("away_odds", 0)
        home_numeric = bet365_hc_to_numeric(home_hc_str)

        # 市场隐含概率（去除双向盘水分后归一化）
        if home_odds > 1.0 and away_odds > 1.0:
            raw_h = 1.0 / home_odds
            raw_a = 1.0 / away_odds
            total = raw_h + raw_a
            home_impl = raw_h / total
        else:
            home_impl = 0.5

        # 叠加信号方向偏移
        if direction == "home":
            home_prob = min(0.92, max(0.08, home_impl + signal_shift))
        elif direction == "away":
            home_prob = min(0.92, max(0.08, home_impl - signal_shift))
        else:
            home_prob = home_impl
        away_prob = 1.0 - home_prob

        # 期望值
        home_ev = round(home_prob * home_odds - 1.0, 3) if home_odds > 1.0 else -1.0
        away_ev = round(away_prob * away_odds - 1.0, 3) if away_odds > 1.0 else -1.0

        home_predicted = home_prob > 0.50
        away_predicted = away_prob > 0.50

        # 最佳价值：正期望 + 赔率在甜蜜区间（信号方向那侧）
        home_value = (home_ev > 0) and (1.70 <= home_odds <= 2.30) and (direction == "home")
        away_value = (away_ev > 0) and (1.70 <= away_odds <= 2.30) and (direction == "away")

        result.append({
            "home_handicap":   home_hc_str,
            "home_odds":       home_odds,
            "away_handicap":   away_hc_str,
            "away_odds":       away_odds,
            "home_numeric":    home_numeric,
            "home_prob":       round(home_prob, 3),
            "away_prob":       round(away_prob, 3),
            "home_ev":         home_ev,
            "away_ev":         away_ev,
            "home_predicted":  home_predicted,
            "away_predicted":  away_predicted,
            "home_best_value": home_value,
            "away_best_value": away_value,
        })

    result.sort(key=lambda x: x["home_numeric"], reverse=True)
    return result


def pick_best_line(bet365_lines: list, direction: str, crow_hc: str = "", confidence: int = 55) -> dict:
    """选期望值最高的一条线（正期望优先，再看赔率甜蜜区间）"""
    predictions = predict_winning_lines(bet365_lines, direction, crow_hc, confidence)

    best    = None
    best_ev = -999.0

    for p in predictions:
        if direction == "home" and p["home_predicted"]:
            ev   = p["home_ev"]
            odds = p["home_odds"]
            hc   = p["home_handicap"]
            prob = p["home_prob"]
        elif direction == "away" and p["away_predicted"]:
            ev   = p["away_ev"]
            odds = p["away_odds"]
            hc   = p["away_handicap"]
            prob = p["away_prob"]
        else:
            continue

        if odds <= 1.0:
            continue

        # 甜蜜区间内 EV 直接比较；区间外打折惩罚
        if 1.70 <= odds <= 2.30:
            score = ev
        else:
            score = ev - 0.05  # 赔率偏低或偏高时降权

        if score > best_ev:
            best_ev = score
            best = {"handicap": hc, "odds": odds, "prob": round(prob, 3), "ev": round(ev, 3)}

    return best or {}


# ── 历史记录 & 自动调权 ──────────────────────────────────────

def record_prediction(match_key: str, analysis: dict):
    """将预测写入历史（每场比赛唯一）"""
    history = load_history()
    record  = {
        "match_key":      match_key,
        "date":           datetime.now().strftime("%Y-%m-%d"),
        "home":           analysis.get("home"),
        "away":           analysis.get("away"),
        "direction":      analysis.get("direction"),
        "direction_team": analysis.get("direction_team"),
        "signal_type":    analysis.get("signal_type"),
        "best_line":      analysis.get("best_line"),
        "confidence":     analysis.get("confidence"),
        "factors_snap":   analysis.get("factors"),
        "weights_snap":   analysis.get("weights_used"),
        "predicted_at":   analysis.get("analyzed_at"),
        "result":         None,   # 赛后填
        "correct":        None,
        "pnl":            None,
    }

    for i, r in enumerate(history):
        if r.get("match_key") == match_key:
            # 保留已有结果，更新预测部分
            record["result"]  = r.get("result")
            record["correct"] = r.get("correct")
            record["pnl"]     = r.get("pnl")
            history[i]        = record
            save_history(history)
            return

    history.append(record)
    save_history(history)


def record_result(match_key: str, home_score: int, away_score: int) -> dict:
    """录入比赛结果，判断预测对错，触发权重调整"""
    history = load_history()

    for record in history:
        if record.get("match_key") == match_key:
            record["result"] = f"{home_score}-{away_score}"

            best_line = record.get("best_line", {})
            direction = record.get("direction")

            if best_line and direction:
                hc   = best_line.get("handicap", "")
                odds = best_line.get("odds", 0)
                win  = evaluate_asian_handicap(hc, direction, home_score, away_score)
                record["correct"] = win
                record["pnl"]     = round(odds - 1, 2) if win else -1.0

            save_history(history)
            tune_weights(history)
            return record

    return {}


def evaluate_asian_handicap(hc_str: str, direction: str, home_score: int, away_score: int):
    """
    判断亚让盘是否赢
    返回 True=赢，False=输，None=退水(平)
    """
    nums = re.findall(r'[+-]?\d+\.?\d*', hc_str)
    if not nums:
        return False

    goal_diff = home_score - away_score   # 主队净胜球

    results = []
    for n in nums[:2]:
        hc = float(n)
        # direction=home: 主队投注方，adjusted = goal_diff + hc（主队视角）
        # direction=away: 客队投注方，adjusted = -goal_diff + hc（客队视角）
        adjusted = (goal_diff + hc) if direction == "home" else (-goal_diff + hc)
        if adjusted > 0:
            results.append("win")
        elif adjusted < 0:
            results.append("lose")
        else:
            results.append("push")

    if all(r == "win"  for r in results): return True
    if all(r == "lose" for r in results): return False
    return None   # 半赢半退水


def get_calibration_status(history: list) -> dict:
    """返回校准进度和当前是否可信"""
    settled = [r for r in history if r.get("correct") is not None]
    n = len(settled)
    return {
        "settled":   n,
        "target":    CALIBRATION_TARGET,
        "progress":  round(n / CALIBRATION_TARGET * 100, 1),
        "calibrated": n >= CALIBRATION_TARGET,
        "message":   f"已校准（{n}场）" if n >= CALIBRATION_TARGET else f"校准中 {n}/{CALIBRATION_TARGET}场",
    }


def tune_weights(history: list) -> dict:
    """
    基于历史实证自动提炼因子权重。
    核心逻辑：计算每个因子与胜负的相关性，相关越高权重越大，无相关的清零。
    达到 CALIBRATION_TARGET 场后做完整提炼，否则仅微调。
    """
    settled = [r for r in history if r.get("correct") is not None and r.get("factors_snap")]
    if len(settled) < 5:
        return load_weights()

    weights = load_weights()

    # ── 30场后做完整因子相关性提炼 ──────────────────────────
    if len(settled) >= CALIBRATION_TARGET:
        factor_scores = {k: [] for k in DEFAULT_WEIGHTS}

        for r in settled:
            fs  = r.get("factors_snap", {})
            win = 1.0 if r.get("correct") is True else 0.0

            # 提取每个因子的得分（0~1），与胜负做相关
            factor_scores["line_compression"].append(
                (min(fs.get("line_compression", {}).get("size", 0) / 4.0, 1.0), win))
            factor_scores["compression_magnitude"].append(
                (min(fs.get("line_compression", {}).get("size", 0) / 3.0, 1.0), win))
            factor_scores["water_alignment"].append(
                (1.0 if not fs.get("water", {}).get("aligned", True) else 0.6, win))
            factor_scores["drift_consistency"].append(
                (fs.get("drift", {}).get("consistency", 0.5), win))
            factor_scores["reverse_signal"].append(
                (fs.get("reverse_signal", {}).get("score", 0.0), win))
            factor_scores["late_money"].append(
                (fs.get("late_money", {}).get("score", 0.3), win))
            factor_scores["handicap_level"].append(
                (fs.get("handicap_level", {}).get("score", 0.5), win))

        # 计算每个因子的均值差：赢场平均得分 - 输场平均得分
        new_weights = {}
        for k, pairs in factor_scores.items():
            wins_scores   = [s for s, w in pairs if w == 1.0]
            losses_scores = [s for s, w in pairs if w == 0.0]
            if not wins_scores or not losses_scores:
                new_weights[k] = DEFAULT_WEIGHTS[k]  # 数据不足保留默认
                continue
            win_avg  = sum(wins_scores)   / len(wins_scores)
            loss_avg = sum(losses_scores) / len(losses_scores)
            # 边际预测力：赢场得分比输场高多少
            edge = win_avg - loss_avg
            new_weights[k] = max(edge, 0.01)  # 负相关因子保留最低权重 0.01

        # 归一化
        total = sum(new_weights.values())
        weights = {k: round(v / total, 3) for k, v in new_weights.items()}

    else:
        # 校准前：基于近10场胜率做简单微调
        recent   = settled[-10:]
        win_rate = sum(1 for r in recent if r.get("correct") is True) / len(recent)

        if win_rate < 0.45:
            weights["line_compression"]  = min(weights.get("line_compression", 0.20)  + 0.02, 0.35)
            weights["reverse_signal"]    = min(weights.get("reverse_signal", 0.20)    + 0.02, 0.35)
            weights["water_alignment"]   = max(weights.get("water_alignment", 0.15)   - 0.02, 0.05)

    # 确保所有新因子都在权重表里（升级兼容）
    for k, v in DEFAULT_WEIGHTS.items():
        if k not in weights:
            weights[k] = v

    total = sum(weights.values())
    weights = {k: round(v / total, 3) for k, v in weights.items()}

    save_weights(weights)
    return weights


def reanalyze_history(history: list) -> dict:
    """
    对历史所有已结算记录进行因子提炼分析
    找出哪些因子组合胜率最高，输出规律
    """
    settled = [r for r in history if r.get("correct") is not None and r.get("factors_snap")]
    calib   = get_calibration_status(history)
    if len(settled) < 3:
        return {"error": "已结算记录不足（需要至少3场）", "settled": len(settled), "calibration": calib}

    wins   = [r for r in settled if r.get("correct") is True]
    losses = [r for r in settled if r.get("correct") is False]

    def avg_factors(records):
        if not records:
            return {}
        comp_sizes   = [r["factors_snap"].get("line_compression", {}).get("size", 0)        for r in records]
        consistencies= [r["factors_snap"].get("drift", {}).get("consistency", 0)             for r in records]
        imbalances   = [r["factors_snap"].get("water", {}).get("imbalance", 0)               for r in records]
        aligned_cnt  = sum(1 for r in records if r["factors_snap"].get("water", {}).get("aligned", False))
        cold_cnt     = sum(1 for r in records if r.get("signal_type") == "背离爆冷")
        return {
            "avg_compression_size":  round(sum(comp_sizes)    / len(comp_sizes),    2),
            "avg_consistency":       round(sum(consistencies) / len(consistencies), 2),
            "avg_water_imbalance":   round(sum(imbalances)    / len(imbalances),    3),
            "aligned_rate":          round(aligned_cnt / len(records), 2),
            "cold_signal_rate":      round(cold_cnt    / len(records), 2),
            "count":                 len(records),
        }

    win_avg  = avg_factors(wins)
    loss_avg = avg_factors(losses)

    # 规律提炼：找出赢场 vs 输场的关键差异
    patterns = []
    if wins and losses:
        wc = win_avg.get("avg_compression_size", 0)
        lc = loss_avg.get("avg_compression_size", 0)
        if wc - lc >= 0.5:
            patterns.append(f"赢场盘口压缩平均{wc}步 > 输场{lc}步，压缩幅度越大越可靠")

        wi = win_avg.get("avg_consistency", 0)
        li = loss_avg.get("avg_consistency", 0)
        if wi - li >= 0.1:
            patterns.append(f"赢场水位连贯性{int(wi*100)}% > 输场{int(li*100)}%，连贯性越高越准")

        wa = win_avg.get("avg_water_imbalance", 0)
        la = loss_avg.get("avg_water_imbalance", 0)
        if wa - la >= 0.02:
            patterns.append(f"赢场水位偏差{wa} > 输场{la}，偏差越大信号越强")

        if win_avg.get("aligned_rate", 0) > loss_avg.get("aligned_rate", 0) + 0.15:
            patterns.append("水位与盘口同向时胜率更高")
        elif loss_avg.get("aligned_rate", 0) > win_avg.get("aligned_rate", 0) + 0.15:
            patterns.append("背离信号（水位与盘口反向）胜率更高，应加大背离权重")

    # 按信心指数分段看胜率
    confidence_buckets = {"50-65": [], "65-80": [], "80+": []}
    for r in settled:
        c = r.get("confidence", 0)
        if c >= 80:
            confidence_buckets["80+"].append(r.get("correct"))
        elif c >= 65:
            confidence_buckets["65-80"].append(r.get("correct"))
        else:
            confidence_buckets["50-65"].append(r.get("correct"))

    confidence_stats = {}
    for bucket, results in confidence_buckets.items():
        if results:
            wr = round(sum(1 for x in results if x is True) / len(results) * 100, 1)
            confidence_stats[bucket] = {"count": len(results), "win_rate": wr}

    return {
        "total_settled": len(settled),
        "win_avg":        win_avg,
        "loss_avg":       loss_avg,
        "patterns":       patterns if patterns else ["数据不足，暂无规律"],
        "confidence_stats": confidence_stats,
        "recommendation": _suggest_weight_adjustment(win_avg, loss_avg),
    }


def _suggest_weight_adjustment(win_avg: dict, loss_avg: dict) -> str:
    if not win_avg or not loss_avg:
        return "数据不足"

    tips = []
    if win_avg.get("avg_compression_size", 0) > loss_avg.get("avg_compression_size", 0) + 0.5:
        tips.append("建议提高 line_compression 权重")
    if win_avg.get("avg_consistency", 0) > loss_avg.get("avg_consistency", 0) + 0.1:
        tips.append("建议提高 drift_consistency 权重")
    if loss_avg.get("aligned_rate", 0) > win_avg.get("aligned_rate", 0) + 0.15:
        tips.append("建议提高 water_alignment 权重（背离时更准）")

    return "；".join(tips) if tips else "当前权重配置合理，继续积累数据"


def get_stats(history: list) -> dict:
    """计算历史统计"""
    settled = [r for r in history if r.get("correct") is not None]
    if not settled:
        return {"total": 0, "wins": 0, "win_rate": 0, "total_pnl": 0}

    wins      = sum(1 for r in settled if r.get("correct") is True)
    total_pnl = sum(r.get("pnl", 0) or 0 for r in settled)

    return {
        "total":    len(settled),
        "wins":     wins,
        "win_rate": round(wins / len(settled) * 100, 1),
        "total_pnl": round(total_pnl, 2),
    }
