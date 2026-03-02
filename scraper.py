"""
SoccerBurst - 足球盘口扫描器
数据来源：sporttery.cn（比赛列表）+ titan007.com（Crow* 亚让赔率）

流程：
  1. 访问 sporttery.cn/jc/jsq/zqspf/ 获取当天竞彩比赛列表（8-10场）
  2. 按联赛权重排序，选取前3场（五大联赛优先）
  3. 将球队名映射到 titan007 比赛ID（通过 jc.titan007.com/index.aspx）
  4. 对每场比赛访问 Crow* 亚让详情页：
     http://vip.titan007.com/changeDetail/handicap.aspx?id=xxx&companyID=3&l=0
  5. 解析赔率历史，筛选状态为"即"的记录（开赛前40分钟内）
  6. 在相同盘口下计算赔率变化，≥0.10 触发报警

联赛权重（Betfair流动性代理）：
  西甲/英超/德甲/意甲/法甲 = 5（五大联赛）
  英冠/葡超/荷甲/比甲 = 4
  其他欧洲联赛 = 3
  亚洲/其他 = 1
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

CROW_COMPANY_ID = 3  # Crow* 的 companyID
TOP_N_MATCHES = 3    # 只扫描前N场（按联赛权重）

# 联赛权重（用于代替 Betfair matched value 排序）
LEAGUE_WEIGHTS = {
    # 五大联赛 - 最高流动性
    "西甲": 5, "英超": 5, "德甲": 5, "意甲": 5, "法甲": 5,
    # 次级联赛
    "英冠": 4, "葡超": 4, "荷甲": 4, "比甲": 4, "苏超": 4,
    "西乙": 3, "德乙": 3, "意乙": 3, "法乙": 3,
    "欧冠": 5, "欧联": 4, "欧会": 3,
    # 其他
    "韩职": 2, "日职": 2, "中超": 2,
}


def get_league_weight(league: str) -> int:
    """获取联赛权重，用于排序"""
    for key, weight in LEAGUE_WEIGHTS.items():
        if key in league:
            return weight
    return 1


# ─────────────────────────────────────────────
# 1. 从 sporttery.cn 获取当天竞彩比赛列表
# ─────────────────────────────────────────────
async def fetch_sporttery_today_matches(page) -> list[dict]:
    """
    从 sporttery.cn/jc/jsq/zqspf/ 解析当天竞彩比赛
    返回: [{"match_num": "008", "league": "西甲", "home": "皇马", "away": "赫塔菲",
             "match_time": "03-03 04:00", "weight": 5}, ...]
    """
    matches = []
    try:
        logger.info("访问 sporttery.cn 获取当天竞彩比赛...")
        await page.goto("https://www.sporttery.cn/jc/jsq/zqspf/", timeout=30000)
        await page.wait_for_timeout(3000)

        rows = await page.query_selector_all("tr")
        logger.info(f"找到 {len(rows)} 行")

        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            cell_texts = []
            for cell in cells:
                t = (await cell.inner_text()).strip()
                cell_texts.append(t)

            # 比赛行：第0列包含"周X\n001"格式
            first_cell = cell_texts[0]
            match_num_match = re.search(r'(\d{3})', first_cell)
            if not match_num_match:
                continue

            match_num = match_num_match.group(1)
            league = cell_texts[1] if len(cell_texts) > 1 else ""
            match_time = cell_texts[2] if len(cell_texts) > 2 else ""

            # 解析主客队（第3列，格式如 "[意甲19]比萨 VS 博洛尼亚[意甲9]"）
            teams_text = cell_texts[3] if len(cell_texts) > 3 else ""
            vs_match = re.search(r'(.+?)\s+VS\s+(.+)', teams_text)
            if not vs_match:
                continue

            home_raw = vs_match.group(1).strip()
            away_raw = vs_match.group(2).strip()

            # 去掉排名标注如 [意甲19]
            home = re.sub(r'\[.*?\]', '', home_raw).strip()
            away = re.sub(r'\[.*?\]', '', away_raw).strip()

            weight = get_league_weight(league)

            matches.append({
                "match_num": match_num,
                "league": league,
                "home": home,
                "away": away,
                "match_time": match_time,
                "weight": weight,
            })

        logger.info(f"解析到 {len(matches)} 场竞彩比赛")

    except Exception as e:
        logger.error(f"获取 sporttery 比赛列表失败: {e}")

    return matches


# ─────────────────────────────────────────────
# 2. 从 titan007 获取比赛ID（通过球队名匹配）
# ─────────────────────────────────────────────
async def fetch_titan007_match_ids(page) -> list[dict]:
    """
    从 jc.titan007.com/index.aspx 获取所有比赛的ID和球队名
    返回: [{"match_id": "2804551", "home": "皇马", "away": "赫塔菲"}, ...]
    """
    titan_matches = []
    try:
        logger.info("访问 titan007 获取比赛ID...")
        await page.goto("https://jc.titan007.com/index.aspx", timeout=30000)
        await page.wait_for_timeout(3000)

        rows = await page.query_selector_all("tr[id^='tr1_']")
        logger.info(f"titan007 找到 {len(rows)} 场比赛")

        for row in rows:
            try:
                row_id = await row.get_attribute("id") or ""
                match_id = row_id.replace("tr1_", "")
                if not match_id:
                    continue

                home_el = await row.query_selector(f"#team1_{match_id}")
                away_el = await row.query_selector(f"#team2_{match_id}")
                home = (await home_el.inner_text()).strip() if home_el else ""
                away = (await away_el.inner_text()).strip() if away_el else ""

                if home and away:
                    titan_matches.append({
                        "match_id": match_id,
                        "home": home,
                        "away": away,
                    })
            except Exception as e:
                logger.debug(f"解析titan007行失败: {e}")
                continue

    except Exception as e:
        logger.error(f"获取titan007比赛ID失败: {e}")

    return titan_matches


def find_titan007_match_id(sporttery_home: str, sporttery_away: str,
                            titan_matches: list[dict]) -> str | None:
    """
    通过球队名模糊匹配，找到对应的 titan007 比赛ID
    sporttery 和 titan007 的球队名可能略有不同（如"皇马"vs"皇家马德里"）
    """
    def normalize(name: str) -> str:
        """标准化球队名：去掉空格、特殊字符，取前4个字"""
        name = re.sub(r'[^\u4e00-\u9fff\w]', '', name)
        return name[:4]

    sh = normalize(sporttery_home)
    sa = normalize(sporttery_away)

    for tm in titan_matches:
        th = normalize(tm["home"])
        ta = normalize(tm["away"])

        # 精确匹配或前缀匹配
        home_match = sh == th or sh in th or th in sh
        away_match = sa == ta or sa in ta or ta in sa

        if home_match and away_match:
            return tm["match_id"]

    return None


# ─────────────────────────────────────────────
# 3. 获取单场比赛的 Crow* 亚让赔率详情
# ─────────────────────────────────────────────
async def fetch_crow_detail(context, match_id: str, home: str, away: str) -> dict:
    """
    直接访问 Crow* 亚让详情页，解析赔率历史
    只有"即"状态记录才触发报警（开赛前40分钟内）
    """
    result = {
        "match_id": match_id,
        "home": home,
        "away": away,
        "all_records": [],
        "ji_records": [],
        "alert": False,
        "alert_reason": "",
        "max_change": 0.0,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    detail_url = f"http://vip.titan007.com/changeDetail/handicap.aspx?id={match_id}&companyID={CROW_COMPANY_ID}&l=0"

    try:
        page = await context.new_page()
        logger.info(f"访问详情页: {detail_url}")

        await page.goto(detail_url, timeout=20000)
        await page.wait_for_timeout(2000)

        rows = await page.query_selector_all("#odds2 table tr")
        logger.info(f"比赛 {match_id} 详情页找到 {len(rows)} 行")

        all_records = []
        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 7:
                    continue

                cell_texts = []
                for cell in cells:
                    t = (await cell.inner_text()).strip()
                    cell_texts.append(t)

                # 跳过表头
                if "时间" in cell_texts[0] or "比分" in cell_texts[1]:
                    continue

                home_odds_str = cell_texts[2]
                handicap = cell_texts[3]
                away_odds_str = cell_texts[4]
                change_time = cell_texts[5]
                status = cell_texts[6]

                # 验证赔率格式（0.xx 或 1.xx）
                odds_pattern = re.compile(r'^[01]\.\d{2}$')
                if not odds_pattern.match(home_odds_str) or not odds_pattern.match(away_odds_str):
                    continue

                record = {
                    "home_odds": float(home_odds_str),
                    "handicap": handicap,
                    "away_odds": float(away_odds_str),
                    "time": change_time,
                    "status": status,  # 早/即/滚
                }
                all_records.append(record)

            except Exception as e:
                logger.debug(f"解析行失败: {e}")
                continue

        await page.close()

        result["all_records"] = all_records

        # 筛选"即"状态记录（开赛前约40分钟）
        ji_records = [r for r in all_records if r.get("status") == "即"]
        result["ji_records"] = ji_records

        logger.info(f"比赛 {match_id}: 共 {len(all_records)} 条记录，其中 {len(ji_records)} 条'即'状态")

        # ★ 关键：只有存在"即"状态记录时才分析报警
        # 平时（"早"状态）的赔率变化不触发报警
        if ji_records:
            result = analyze_odds_change(result)
        else:
            # 无"即"记录时，仍计算历史最大变化供参考（不触发报警）
            if all_records:
                result["ji_records"] = all_records
                result = analyze_odds_change(result)
                result["ji_records"] = ji_records  # 还原
                result["alert"] = False  # 强制不报警
                result["alert_reason"] = ""

    except Exception as e:
        logger.error(f"抓取比赛 {match_id} 详情失败: {e}")
        try:
            await page.close()
        except:
            pass

    return result


# ─────────────────────────────────────────────
# 4. 分析赔率变化，判断是否报警
# ─────────────────────────────────────────────
def analyze_odds_change(result: dict) -> dict:
    """
    在相同盘口的记录中，计算赔率变化幅度。
    如果主队或客队赔率变化 >= 0.10，触发报警。
    """
    ji_records = result["ji_records"]

    if len(ji_records) < 2:
        return result

    # 按盘口分组
    handicap_groups: dict[str, list] = {}
    for rec in ji_records:
        hc = rec.get("handicap", "")
        if hc not in handicap_groups:
            handicap_groups[hc] = []
        handicap_groups[hc].append(rec)

    max_change = 0.0
    alert_reasons = []

    for handicap, records in handicap_groups.items():
        if len(records) < 2:
            continue

        home_odds_list = [r["home_odds"] for r in records]
        away_odds_list = [r["away_odds"] for r in records]

        home_max = max(home_odds_list)
        home_min = min(home_odds_list)
        away_max = max(away_odds_list)
        away_min = min(away_odds_list)

        home_change = round(home_max - home_min, 3)
        away_change = round(away_max - away_min, 3)

        if home_change > max_change:
            max_change = home_change
        if away_change > max_change:
            max_change = away_change

        if home_change >= 0.10:
            alert_reasons.append(
                f"盘口[{handicap}] 主队赔率变化 {home_change:.2f} "
                f"(最高{home_max:.2f}→最低{home_min:.2f})"
            )
        if away_change >= 0.10:
            alert_reasons.append(
                f"盘口[{handicap}] 客队赔率变化 {away_change:.2f} "
                f"(最高{away_max:.2f}→最低{away_min:.2f})"
            )

    result["max_change"] = max_change
    if alert_reasons:
        result["alert"] = True
        result["alert_reason"] = "；".join(alert_reasons)

    return result


# ─────────────────────────────────────────────
# 5. 主扫描函数
# ─────────────────────────────────────────────
async def scan_all_matches() -> list[dict]:
    """
    完整扫描流程：
    1. 从 sporttery.cn 获取当天竞彩比赛（8-10场）
    2. 按联赛权重排序，选取前3场
    3. 从 titan007 获取比赛ID（通过球队名匹配）
    4. 对每场比赛抓取 Crow* 亚让赔率详情
    5. 只有"即"状态记录才触发报警
    """
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN"
        )

        # Step 1: 获取 sporttery 当天比赛列表
        sporttery_page = await context.new_page()
        sporttery_matches = await fetch_sporttery_today_matches(sporttery_page)
        await sporttery_page.close()

        if not sporttery_matches:
            logger.warning("未获取到 sporttery 当天比赛")
            await browser.close()
            return results

        # Step 2: 按联赛权重排序，选取前3场
        sporttery_matches.sort(key=lambda m: m["weight"], reverse=True)
        top_matches = sporttery_matches[:TOP_N_MATCHES]

        logger.info(f"当天共 {len(sporttery_matches)} 场竞彩比赛，选取前 {len(top_matches)} 场扫描：")
        for m in top_matches:
            logger.info(f"  [{m['league']}] {m['home']} vs {m['away']} (权重:{m['weight']})")

        # Step 3: 获取 titan007 比赛ID
        titan_page = await context.new_page()
        titan_matches = await fetch_titan007_match_ids(titan_page)
        await titan_page.close()

        # Step 4: 匹配并扫描
        for match in top_matches:
            home = match["home"]
            away = match["away"]

            # 查找 titan007 比赛ID
            match_id = find_titan007_match_id(home, away, titan_matches)

            if not match_id:
                logger.warning(f"未找到 {home} vs {away} 的 titan007 比赛ID，跳过")
                # 仍然添加到结果中，但无数据
                results.append({
                    "match_id": None,
                    "home": home,
                    "away": away,
                    "league": match["league"],
                    "match_time": match["match_time"],
                    "match_num": match["match_num"],
                    "weight": match["weight"],
                    "all_records": [],
                    "ji_records": [],
                    "alert": False,
                    "alert_reason": "未找到titan007比赛ID",
                    "max_change": 0.0,
                    "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                continue

            logger.info(f"扫描: {home} vs {away} (ID: {match_id})")

            result = await fetch_crow_detail(context, match_id, home, away)
            result["league"] = match["league"]
            result["match_time"] = match["match_time"]
            result["match_num"] = match["match_num"]
            result["weight"] = match["weight"]
            results.append(result)

            await asyncio.sleep(1.5)

        await browser.close()

    return results


# ─────────────────────────────────────────────
# 6. 保存结果到 JSON 文件
# ─────────────────────────────────────────────
def save_results(results: list[dict], filepath: str = None):
    if filepath is None:
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_matches": len(results),
        "alert_count": sum(1 for r in results if r.get("alert")),
        "matches": results
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存到 {filepath}")


if __name__ == "__main__":
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    async def main():
        results = await scan_all_matches()
        save_results(results)
        print(f"\n扫描完成，共 {len(results)} 场比赛")
        alerts = [r for r in results if r.get("alert")]
        print(f"报警比赛：{len(alerts)} 场")
        for a in alerts:
            print(f"  ⚠️  {a['home']} vs {a['away']}: {a['alert_reason']}")

        print("\n所有扫描比赛：")
        for r in results:
            ji_count = len(r.get("ji_records", []))
            all_count = len(r.get("all_records", []))
            print(f"  [{r.get('league','')}] {r['home']} vs {r['away']}: "
                  f"总{all_count}条, 即{ji_count}条, 最大变化{r.get('max_change', 0):.3f}, "
                  f"报警={r.get('alert')}")

    asyncio.run(main())
