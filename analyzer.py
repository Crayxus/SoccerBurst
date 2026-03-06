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

# 初始因子权重
DEFAULT_WEIGHTS = {
    "line_compression":      0.40,   # 盘口压缩方向（聪明钱，最重要）
    "compression_magnitude": 0.25,   # 压缩幅度
    "water_alignment":       0.20,   # 水位与盘口方向一致性
    "drift_consistency":     0.15,   # 水位漂移连贯性
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

    ji_records  = match_data.get("ji_records", [])
    all_records = match_data.get("all_records", [])
    home        = match_data.get("home", "")
    away        = match_data.get("away", "")

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
        "records":      len(ji_records),
    }

    # ── 综合决策 ────────────────────────────────────────────
    if compression_dir == "neutral":
        bet_direction = water_dir
        signal_type   = "水位跟随"
    else:
        bet_direction = compression_dir
        if not aligned:
            # 盘口与水位方向背离 = 公众资金 vs 聪明钱对立 = 经典爆冷信号
            signal_type = "背离爆冷"
        else:
            signal_type = "同向确认"

    # ── 信心指数 ────────────────────────────────────────────
    s_compression = min(compression_size / 4.0, 1.0) * weights["line_compression"]
    s_magnitude   = min(compression_size / 3.0, 1.0) * weights["compression_magnitude"]
    s_alignment   = (1.0 if aligned else 0.5) * min(water_imbalance / 0.15, 1.0) * weights["water_alignment"]
    s_drift       = consistency * weights["drift_consistency"]

    confidence = min(int((s_compression + s_magnitude + s_alignment + s_drift) * 100), 95)

    # ── 最佳 bet365 盘口 ─────────────────────────────────────
    best_line = pick_best_line(bet365_lines, bet_direction)

    return {
        "home":           home,
        "away":           away,
        "direction":      bet_direction,
        "direction_team": home if bet_direction == "home" else away,
        "signal_type":    signal_type,
        "best_line":      best_line,
        "confidence":     confidence,
        "factors":        factors,
        "weights_used":   weights,
        "analyzed_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def pick_best_line(bet365_lines: list, direction: str) -> dict:
    """
    从 bet365 盘口中找最佳投注线
    甜蜜点：赔率 1.75 ~ 2.20（风险收益平衡）
    """
    best       = None
    best_score = -1

    for line in bet365_lines:
        if direction == "home":
            odds = line.get("home_odds", 0)
            hc   = line.get("home_handicap", "")
        else:
            odds = line.get("away_odds", 0)
            hc   = line.get("away_handicap", "")

        if not odds or odds <= 1.0:
            continue

        # 甜蜜点评分（确保甜蜜点始终优先于高赔率）
        if 1.75 <= odds <= 2.20:
            score = odds              # 1.75 ~ 2.20
        elif 1.60 <= odds < 1.75:
            score = odds * 0.75      # 最高 1.31，低于甜蜜点下限
        elif 2.20 < odds <= 2.50:
            score = odds * 0.60      # 最高 1.50，低于甜蜜点下限
        else:
            score = odds * 0.15      # 极低，排除高赔率异常盘

        if score > best_score:
            best_score = score
            best = {"handicap": hc, "odds": odds}

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


def tune_weights(history: list) -> dict:
    """基于历史胜负自动微调因子权重"""
    settled = [r for r in history if r.get("correct") is not None]
    if len(settled) < 5:
        return load_weights()

    recent    = settled[-10:]
    win_rate  = sum(1 for r in recent if r.get("correct") is True) / len(recent)
    weights   = load_weights()

    if win_rate < 0.45:
        # 胜率偏低：加大盘口压缩权重，压低水位权重
        weights["line_compression"]      = min(weights["line_compression"]      + 0.03, 0.55)
        weights["compression_magnitude"] = min(weights["compression_magnitude"] + 0.02, 0.35)
        weights["water_alignment"]       = max(weights["water_alignment"]       - 0.03, 0.10)
        weights["drift_consistency"]     = max(weights["drift_consistency"]     - 0.02, 0.08)
    elif win_rate > 0.65:
        # 胜率良好：小幅强化最有效因子（暂时保持）
        pass

    # 归一化
    total   = sum(weights.values())
    weights = {k: round(v / total, 3) for k, v in weights.items()}

    save_weights(weights)
    return weights


def reanalyze_history(history: list) -> dict:
    """
    对历史所有已结算记录进行因子提炼分析
    找出哪些因子组合胜率最高，输出规律
    """
    settled = [r for r in history if r.get("correct") is not None and r.get("factors_snap")]
    if len(settled) < 3:
        return {"error": "已结算记录不足（需要至少3场）", "settled": len(settled)}

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
