"""
SoccerBurst - 足球盘口扫描器
数据来源：sporttery.cn（比赛列表）+ titan007.com（Crow* 亚让赔率）

流程：
  1. 访问 sporttery.cn/jc/jsq/zqspf/ 获取当天竞彩比赛列表
  2. 将所有比赛映射到 titan007 比赛ID（通过 jc.titan007.com/index.aspx）
  3. 对每场比赛快速获取 Crow* 亚让详情页的记录数（热度探针）
  4. 按 Crow* 记录数降序排列，选取前3场（记录数最多 = 热度最高）
  5. 对选出的3场进行完整扫描：解析赔率历史，筛选"即"状态记录（开赛前40分钟内）
  6. 在相同盘口下计算赔率变化，≥0.10 触发报警
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# DrissionPage 用于 bet365 抓取（绕过反爬）
try:
    from DrissionPage import Chromium, ChromiumOptions
    DRISSION_AVAILABLE = True
except ImportError:
    DRISSION_AVAILABLE = False
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning("DrissionPage 未安装，bet365 抓取将不可用。运行: pip install DrissionPage")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

CROW_COMPANY_ID = 3  # Crow* 的 companyID
TOP_N_MATCHES = 3    # 最终扫描前N场（按Crow*记录数排序，热度最高的）


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

            matches.append({
                "match_num": match_num,
                "league": league,
                "home": home,
                "away": away,
                "match_time": match_time,
            })

        logger.info(f"解析到 {len(matches)} 场竞彩比赛（含今明两天）")

    except Exception as e:
        logger.error(f"获取 sporttery 比赛列表失败: {e}")

    # 过滤"今日赛程"的比赛：
    # 规则：以11:00为分界线，一个"赛日"从当天11:00到次日10:59
    #   - 当前时间 >= 11:00：赛日 = 今天，接受今天日期 + 明天凌晨(<10:00) 的比赛
    #   - 当前时间 < 11:00（即凌晨）：赛日 = 昨天，接受昨天日期 + 今天凌晨(<10:00) 的比赛
    from datetime import timedelta
    now = datetime.now()

    if now.hour >= 11:
        # 今天11:00之后：赛日为今天
        session_date = now.strftime("%m-%d")
        next_date = (now + timedelta(days=1)).strftime("%m-%d")
    else:
        # 今天凌晨（0:00~10:59）：赛日为昨天
        session_date = (now - timedelta(days=1)).strftime("%m-%d")
        next_date = now.strftime("%m-%d")

    today_matches = []
    for m in matches:
        mt = m.get("match_time", "")
        parts = mt.replace("\n", " ").split()
        date_part = parts[0].strip() if parts else ""
        time_part = parts[-1].strip() if len(parts) >= 2 else ""

        if date_part == session_date:
            # 赛日当天的比赛：只接受 >= 11:00 的（下午/晚上场次）
            try:
                hour = int(time_part.split(":")[0])
                if hour >= 11:
                    today_matches.append(m)
                # 赛日当天 < 11:00 的比赛属于前一个赛日，跳过
            except:
                today_matches.append(m)  # 无法解析时间时保留
        elif date_part == next_date:
            # 次日的比赛：只接受 < 10:00 的（凌晨场次）
            try:
                hour = int(time_part.split(":")[0])
                if hour < 10:
                    today_matches.append(m)
            except:
                pass

    logger.info(f"今日赛程（赛日:{session_date} 11:00 ~ {next_date} 10:00）：{len(today_matches)} 场")
    return today_matches



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
# 3. 热度探针：快速获取 Crow* 记录数（不解析详情）
# ─────────────────────────────────────────────
async def fetch_crow_record_count(context, match_id: str) -> int:
    """
    快速访问 Crow* 亚让详情页，只统计记录行数（不做完整解析）
    用于热度排序：记录数越多 = 盘口变化越频繁 = 热度越高
    """
    detail_url = f"http://vip.titan007.com/changeDetail/handicap.aspx?id={match_id}&companyID={CROW_COMPANY_ID}&l=0"
    count = 0
    page = None
    try:
        page = await context.new_page()
        await page.goto(detail_url, timeout=15000)
        await page.wait_for_timeout(1500)
        rows = await page.query_selector_all("#odds2 table tr")
        # 粗略统计：有7列以上的行才算有效记录
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 7:
                count += 1
        # 减去表头行（通常1行）
        count = max(0, count - 1)
    except Exception as e:
        logger.debug(f"热度探针 {match_id} 失败: {e}")
    finally:
        if page:
            try:
                await page.close()
            except:
                pass
    return count


# ─────────────────────────────────────────────
# 4. 获取单场比赛的 Crow* 亚让赔率详情
# ─────────────────────────────────────────────
async def fetch_crow_detail(context, match_id: str, home: str, away: str,
                             match_kickoff: str = None) -> dict:
    """
    直接访问 Crow* 亚让详情页，解析赔率历史
    只有"即"状态记录才触发报警（开赛前40分钟内）
    match_kickoff: 比赛开赛时间，格式 "HH:MM"，用于判断是否在开赛前40分钟内
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

        # 筛选"即"状态记录（titan007标记的开赛前约40分钟）
        ji_records = [r for r in all_records if r.get("status") == "即"]
        result["ji_records"] = ji_records

        logger.info(f"比赛 {match_id}: 共 {len(all_records)} 条记录，其中 {len(ji_records)} 条'即'状态")

        # ★ 计算开赛时间，确定40分钟窗口的起始时间点
        # 只有在"开赛前40分钟内"产生的"即"记录才参与报警计算
        # 即：记录时间 >= (开赛时间 - 40分钟)
        window_records = []  # 40分钟窗口内的"即"记录
        kickoff_dt = None
        minutes_to_kickoff = None

        if match_kickoff:
            now = datetime.now()
            try:
                from datetime import timedelta
                ko_h, ko_m = map(int, match_kickoff.split(":"))
                ko_dt = now.replace(hour=ko_h, minute=ko_m, second=0, microsecond=0)
                if ko_dt < now - timedelta(hours=2):
                    ko_dt += timedelta(days=1)
                kickoff_dt = ko_dt
                minutes_to_kickoff = (ko_dt - now).total_seconds() / 60
                result["minutes_to_kickoff"] = round(minutes_to_kickoff, 1)
                logger.info(f"比赛 {match_id}: 距开赛 {minutes_to_kickoff:.0f} 分钟")

                # 计算40分钟窗口起始时间（开赛前40分钟）
                window_start = ko_dt - timedelta(minutes=40)

                # 过滤：只保留时间戳 >= window_start 的"即"记录
                # titan007 记录时间格式如 "3-4 02:50"
                for r in ji_records:
                    rec_time_str = r.get("time", "")
                    try:
                        # 解析记录时间，格式 "M-D HH:MM"
                        parts = rec_time_str.strip().split()
                        if len(parts) >= 2:
                            date_p = parts[0]  # "3-4"
                            time_p = parts[1]  # "02:50"
                            m_d = date_p.split("-")
                            h_m = time_p.split(":")
                            rec_dt = now.replace(
                                month=int(m_d[0]), day=int(m_d[1]),
                                hour=int(h_m[0]), minute=int(h_m[1]),
                                second=0, microsecond=0
                            )
                            # 如果记录时间比当前时间早超过12小时，说明是昨天的记录
                            if rec_dt > now + timedelta(hours=1):
                                rec_dt -= timedelta(days=1)
                            r["_rec_dt"] = rec_dt.strftime("%Y-%m-%d %H:%M:%S")
                            if rec_dt >= window_start:
                                r["in_window"] = True
                                window_records.append(r)
                            else:
                                r["in_window"] = False
                        else:
                            r["in_window"] = False
                    except Exception:
                        r["in_window"] = False

            except Exception as e:
                logger.debug(f"解析开赛时间失败: {e}")
                window_records = ji_records  # 降级：使用所有即记录
        else:
            window_records = ji_records  # 无开赛时间时不限制

        result["window_records"] = window_records
        logger.info(f"比赛 {match_id}: 40分钟窗口内 {len(window_records)} 条记录")

        # ★ 只用窗口内的记录计算报警
        if window_records:
            result = analyze_odds_change(result, window_records)
        # 无窗口记录：不报警，不计算，保持默认值

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
def analyze_odds_change(result: dict, records_to_analyze: list = None) -> dict:
    """
    在相同盘口的记录中，计算赔率变化幅度。
    如果主队或客队赔率变化 >= 0.10，触发报警。
    records_to_analyze: 要分析的记录列表（默认使用 ji_records）
    """
    ji_records = records_to_analyze if records_to_analyze is not None else result["ji_records"]

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
# 5. 中文→英文球队名映射表 + bet365 搜索
# ─────────────────────────────────────────────

# 中文球队名 → bet365 英文搜索关键词
TEAM_NAME_MAP = {
    # 英超
    "曼联": "Man Utd",
    "曼城": "Man City",
    "利物浦": "Liverpool",
    "切尔西": "Chelsea",
    "阿森纳": "Arsenal",
    "热刺": "Tottenham",
    "纽卡斯尔": "Newcastle",
    "西汉姆": "West Ham",
    "阿斯顿维拉": "Aston Villa",
    "维拉": "Aston Villa",
    "埃弗顿": "Everton",
    "莱斯特": "Leicester",
    "布莱顿": "Brighton",
    "水晶宫": "Crystal Palace",
    "富勒姆": "Fulham",
    "伯恩利": "Burnley",
    "布伦特福德": "Brentford",
    "诺丁汉森林": "Nott'm Forest",
    "诺丁汉": "Nott'm Forest",
    "狼队": "Wolves",
    "伍尔弗汉普顿": "Wolves",
    "南安普顿": "Southampton",
    "伊普斯维奇": "Ipswich",
    "博尔顿": "Bolton",
    "谢菲尔德联": "Sheffield Utd",
    "谢联": "Sheffield Utd",
    "卢顿": "Luton",
    "伯恩茅斯": "Bournemouth",
    "诺丁汉": "Nott'm Forest",
    # 西甲
    "皇马": "Real Madrid",
    "皇家马德里": "Real Madrid",
    "巴萨": "Barcelona",
    "巴塞罗那": "Barcelona",
    "马竞": "Atletico Madrid",
    "马德里竞技": "Atletico Madrid",
    "塞维利亚": "Sevilla",
    "皇家社会": "Real Sociedad",
    "毕尔巴鄂": "Athletic Club",
    "比利亚雷亚尔": "Villarreal",
    "贝蒂斯": "Real Betis",
    "皇家贝蒂斯": "Real Betis",
    "瓦伦西亚": "Valencia",
    "赫塔菲": "Getafe",
    "奥萨苏纳": "Osasuna",
    "拉斯帕尔马斯": "Las Palmas",
    "赫罗纳": "Girona",
    "马洛卡": "Mallorca",
    "莱加内斯": "Leganes",
    "阿拉维斯": "Alaves",
    "塞尔塔": "Celta Vigo",
    "埃斯帕尼奥尔": "Espanyol",
    "巴拉多利德": "Valladolid",
    "拉科鲁尼亚": "Deportivo",
    # 德甲
    "拜仁": "Bayern Munich",
    "拜仁慕尼黑": "Bayern Munich",
    "多特蒙德": "Dortmund",
    "莱比锡": "RB Leipzig",
    "勒沃库森": "Leverkusen",
    "法兰克福": "Frankfurt",
    "弗赖堡": "Freiburg",
    "霍芬海姆": "Hoffenheim",
    "门兴格拉德巴赫": "M'gladbach",
    "门兴": "M'gladbach",
    "沙尔克": "Schalke",
    "斯图加特": "Stuttgart",
    "沃尔夫斯堡": "Wolfsburg",
    "柏林联合": "Union Berlin",
    "柏林赫塔": "Hertha",
    "奥格斯堡": "Augsburg",
    "波鸿": "Bochum",
    "达姆施塔特": "Darmstadt",
    "海登海姆": "Heidenheim",
    "基尔": "Holstein Kiel",
    "圣保利": "St. Pauli",
    "不来梅": "Werder Bremen",
    "美因茨": "Mainz",
    # 意甲
    "尤文图斯": "Juventus",
    "尤文": "Juventus",
    "国际米兰": "Inter Milan",
    "国米": "Inter Milan",
    "AC米兰": "AC Milan",
    "米兰": "AC Milan",
    "那不勒斯": "Napoli",
    "罗马": "Roma",
    "拉齐奥": "Lazio",
    "亚特兰大": "Atalanta",
    "佛罗伦萨": "Fiorentina",
    "博洛尼亚": "Bologna",
    "都灵": "Torino",
    "萨索洛": "Sassuolo",
    "维罗纳": "Verona",
    "萨勒尼塔纳": "Salernitana",
    "莱切": "Lecce",
    "蒙扎": "Monza",
    "弗罗西诺内": "Frosinone",
    "热那亚": "Genoa",
    "卡利亚里": "Cagliari",
    "乌迪内斯": "Udinese",
    "恩波利": "Empoli",
    "帕尔马": "Parma",
    "科莫": "Como",
    "威尼斯": "Venezia",
    # 法甲
    "巴黎圣日耳曼": "Paris SG",
    "巴黎": "Paris SG",
    "马赛": "Marseille",
    "里昂": "Lyon",
    "摩纳哥": "Monaco",
    "尼斯": "Nice",
    "雷恩": "Rennes",
    "斯特拉斯堡": "Strasbourg",
    "波尔多": "Bordeaux",
    "南特": "Nantes",
    "蒙彼利埃": "Montpellier",
    "图卢兹": "Toulouse",
    "朗斯": "Lens",
    "里尔": "Lille",
    "布雷斯特": "Brest",
    "勒阿弗尔": "Le Havre",
    "昂热": "Angers",
    "圣埃蒂安": "St Etienne",
    "奥克塞尔": "Auxerre",
    # 葡超
    "本菲卡": "Benfica",
    "波尔图": "Porto",
    "体育里斯本": "Sporting CP",
    "布拉加": "Braga",
    "吉马良斯": "Guimaraes",
    # 荷甲
    "阿贾克斯": "Ajax",
    "费耶诺德": "Feyenoord",
    "埃因霍温": "PSV",
    "PSV": "PSV",
    "阿尔克马尔": "AZ Alkmaar",
    # 苏超
    "凯尔特人": "Celtic",
    "流浪者": "Rangers",
    # 土超
    "加拉塔萨雷": "Galatasaray",
    "费内巴切": "Fenerbahce",
    "贝西克塔斯": "Besiktas",
    "特拉布宗": "Trabzonspor",
    # 俄超
    "泽尼特": "Zenit",
    "斯巴达克": "Spartak Moscow",
    "莫斯科中央陆军": "CSKA Moscow",
    # 女足
    "越南女": "Vietnam W",
    "印度女": "India W",
    "中国女": "China W",
    "日本女": "Japan W",
    "韩国女": "Korea Republic W",
    "澳大利亚女": "Australia W",
    "美国女": "USA W",
    "英格兰女": "England W",
    "法国女": "France W",
    "德国女": "Germany W",
    "西班牙女": "Spain W",
    "荷兰女": "Netherlands W",
    "瑞典女": "Sweden W",
    "挪威女": "Norway W",
    "丹麦女": "Denmark W",
    "加拿大女": "Canada W",
    "巴西女": "Brazil W",
    "阿根廷女": "Argentina W",
    "哥伦比亚女": "Colombia W",
    "新西兰女": "New Zealand W",
    "菲律宾女": "Philippines W",
    "泰国女": "Thailand W",
    "朝鲜女": "Korea DPR W",
    "缅甸女": "Myanmar W",
    "印尼女": "Indonesia W",
    "马来西亚女": "Malaysia W",
    "新加坡女": "Singapore W",
    "香港女": "Hong Kong W",
    "台湾女": "Chinese Taipei W",
    "蒙古女": "Mongolia W",
    "哈萨克斯坦女": "Kazakhstan W",
    "乌兹别克斯坦女": "Uzbekistan W",
    "伊朗女": "Iran W",
    "约旦女": "Jordan W",
    "伊拉克女": "Iraq W",
    "沙特女": "Saudi Arabia W",
    "阿联酋女": "UAE W",
    "卡塔尔女": "Qatar W",
    "巴林女": "Bahrain W",
    "科威特女": "Kuwait W",
    "阿曼女": "Oman W",
    "也门女": "Yemen W",
    "叙利亚女": "Syria W",
    "黎巴嫩女": "Lebanon W",
    "巴勒斯坦女": "Palestine W",
    "以色列女": "Israel W",
    "土耳其女": "Turkey W",
    "格鲁吉亚女": "Georgia W",
    "亚美尼亚女": "Armenia W",
    "阿塞拜疆女": "Azerbaijan W",
    "乌克兰女": "Ukraine W",
    "俄罗斯女": "Russia W",
    "白俄罗斯女": "Belarus W",
    "波兰女": "Poland W",
    "捷克女": "Czech Republic W",
    "斯洛伐克女": "Slovakia W",
    "匈牙利女": "Hungary W",
    "罗马尼亚女": "Romania W",
    "保加利亚女": "Bulgaria W",
    "塞尔维亚女": "Serbia W",
    "克罗地亚女": "Croatia W",
    "斯洛文尼亚女": "Slovenia W",
    "波黑女": "Bosnia & Herz. W",
    "北马其顿女": "North Macedonia W",
    "阿尔巴尼亚女": "Albania W",
    "黑山女": "Montenegro W",
    "科索沃女": "Kosovo W",
    "葡萄牙女": "Portugal W",
    "意大利女": "Italy W",
    "比利时女": "Belgium W",
    "奥地利女": "Austria W",
    "瑞士女": "Switzerland W",
    "苏格兰女": "Scotland W",
    "爱尔兰女": "Republic of Ireland W",
    "北爱尔兰女": "Northern Ireland W",
    "威尔士女": "Wales W",
    "冰岛女": "Iceland W",
    "芬兰女": "Finland W",
    "爱沙尼亚女": "Estonia W",
    "拉脱维亚女": "Latvia W",
    "立陶宛女": "Lithuania W",
    "摩尔多瓦女": "Moldova W",
    "卢森堡女": "Luxembourg W",
    "马耳他女": "Malta W",
    "塞浦路斯女": "Cyprus W",
    "希腊女": "Greece W",
    "北爱尔兰女": "Northern Ireland W",
    "法罗群岛女": "Faroe Islands W",
    "直布罗陀女": "Gibraltar W",
    "安道尔女": "Andorra W",
    "列支敦士登女": "Liechtenstein W",
    "圣马力诺女": "San Marino W",
    "梵蒂冈女": "Vatican W",
}


def get_english_team_name(chinese_name: str) -> str:
    """
    将中文球队名转换为英文（用于 bet365 搜索）
    优先精确匹配，其次前缀匹配
    """
    # 精确匹配
    if chinese_name in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[chinese_name]

    # 前缀/包含匹配（处理带排名标注的情况）
    for cn, en in TEAM_NAME_MAP.items():
        if cn in chinese_name or chinese_name in cn:
            return en

    # 降级：返回原名（bet365 可能支持部分中文或拼音）
    logger.warning(f"未找到球队英文名映射: {chinese_name}，使用原名")
    return chinese_name


def _truncate_to_2dp(num):
    return int(num * 100) / 100


def _fraction_to_decimal(od_str: str) -> float:
    """
    将分数赔率转换为小数赔率（欧赔）
    例如: "21/4" -> 6.25, "3/1" -> 4.0, "1/4" -> 1.25
    """
    od_str = od_str.strip()
    try:
        if '/' in od_str:
            num, den = od_str.split('/')
            return round(int(num) / int(den) + 1, 2)
        else:
            return round(float(od_str) + 1, 2)
    except:
        return 0.0


def _extract_hd_od(text, teams):
    """
    从 bet365 API 响应文本中提取 HD、OD 值（DrissionPage 方案）
    
    API 格式示例（partial 接口）：
    |MA;ID=M50138;FI=189510142;PF=189510141;NA=Newcastle;SY=da;...
    |PA;ID=602740283;HD=-1.5, -2.0;HA=-1.75;SU=0;OD=21/4;
    |PA;ID=602740282;HD=-1.5;HA=-1.5;SU=0;OD=19/5;
    ...
    |MA;ID=M50138;FI=189510142;PF=189510141;NA=Man Utd;SY=da;...
    |PA;ID=602740285;HD=+1.5, +2.0;HA=1.75;SU=0;OD=3/25;
    ...
    
    OD 格式为英式分数赔率（如 21/4），需转换为欧赔（+1）
    """
    result = []

    # 按 |MA; 分割，每个 MA 块包含一个球队的所有盘口
    # 格式: |MA;...;NA=TeamName;...|PA;...;HD=...;OD=...;|PA;...
    ma_blocks = re.split(r'\|MA;', text)

    dic_team = {}
    for block in ma_blocks:
        if not block.strip():
            continue
        # 提取球队名
        na_match = re.search(r'NA=([^;|]+)', block)
        if not na_match:
            continue
        team_name = na_match.group(1).strip()

        # 提取所有 PA 记录
        pa_records = re.findall(r'\|PA;[^|]+', block)
        for pa in pa_records:
            hd_match = re.search(r'HD=([^;]+)', pa)
            od_match = re.search(r'OD=([^;|]+)', pa)
            if hd_match and od_match:
                hd = hd_match.group(1).strip()
                od_raw = od_match.group(1).strip()
                od_decimal = _fraction_to_decimal(od_raw)
                if team_name not in dic_team:
                    dic_team[team_name] = []
                dic_team[team_name].append((hd, od_decimal))

    # 尝试匹配球队名（支持模糊匹配）
    def find_team_key(target, dic):
        if target in dic:
            return target
        for k in dic:
            if target.lower() in k.lower() or k.lower() in target.lower():
                return k
        return None

    home_key = find_team_key(teams[0], dic_team)
    away_key = find_team_key(teams[1], dic_team)

    if not home_key or not away_key:
        logger.warning(f"bet365 未找到球队数据: teams={teams}, 找到的球队={list(dic_team.keys())}")
        return result

    home_list = dic_team[home_key]
    away_list = dic_team[away_key]

    for v1, v2 in zip(home_list, away_list):
        h1, o1 = v1
        h2, o2 = v2
        result.append({
            "home_handicap": h1,
            "home_odds": o1,
            "away_handicap": h2,
            "away_odds": o2,
        })
    return result


def fetch_bet365_asian_handicap_drission(url: str, home: str, away: str) -> dict:
    """
    使用 DrissionPage（真实 Chrome）抓取 bet365 Alternative Asian Handicap 赔率
    通过监听 API 响应获取数据，绕过反爬检测
    url: bet365 比赛直接链接（如 https://www.bet365.com.au/#/AC/B1/C1/D8/E189510141/F3/I3/）
    """
    result = {
        "found": False,
        "url": url,
        "home": home,
        "away": away,
        "handicaps": [],
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": ""
    }

    if not DRISSION_AVAILABLE:
        result["error"] = "DrissionPage 未安装，请运行: pip install DrissionPage"
        return result

    try:
        co = ChromiumOptions()
        co.mute(True)
        # 明确指定 Chrome 路径（避免 WinError 14001 找不到浏览器）
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        import os as _os
        if _os.path.exists(chrome_path):
            co.set_browser_path(chrome_path)
        browser = Chromium(co)

        tab = browser.new_tab(url)

        import time as _time

        # 等待页面加载（15秒）
        _time.sleep(15)
        logger.info(f"bet365 页面标题: {tab.title}")

        # 点击 Asian Lines 标签，监听 API 响应
        api1 = 'matchbettingcontentapi/coupon'
        tab.listen.start([api1])
        btn1 = tab.ele('text:Asian Lines', timeout=5)
        if not btn1:
            tab.close()
            browser.quit()
            result["error"] = "未找到 Asian Lines 标签"
            return result

        btn1.click()
        text1 = ""
        for _ in range(10):
            resp = tab.listen.wait(timeout=3)
            if resp:
                try:
                    raw = resp.response.body
                    text1 = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
                except:
                    text1 = ""
                if 'Asian Handicap' in text1:
                    logger.info(f"bet365 coupon API 响应: {len(text1)} 字符")
                    break

        _time.sleep(2)

        # 点击 Alternative Asian Handicap，监听 API 响应
        api2 = 'matchbettingcontentapi/partial'
        tab.listen.start([api2])
        btn2 = tab.ele('text:Alternative Asian Handicap', timeout=5)
        if not btn2:
            tab.close()
            browser.quit()
            result["error"] = "未找到 Alternative Asian Handicap 按钮"
            return result

        btn2.click()
        text2 = ""
        for _ in range(15):
            resp = tab.listen.wait(timeout=3)
            if not resp:
                btn2.click()
                continue
            try:
                raw = resp.response.body
                text2 = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
            except:
                text2 = ""
            if 'Alternative Asian Handicap' in text2:
                logger.info(f"bet365 partial API 响应: {len(text2)} 字符")
                break

        tab.close()
        browser.quit()

        # 从 API 响应中提取球队名（NA= 字段）
        # 格式: |MA;...;NA=Newcastle;...
        team_names = re.findall(r'\|MA;[^|]*NA=([^;|]+)', text2)
        team_names = [t.strip() for t in team_names if t.strip()]
        # 去重保序
        seen = set()
        unique_teams = []
        for t in team_names:
            if t not in seen:
                seen.add(t)
                unique_teams.append(t)
        team_names = unique_teams[:2]

        if len(team_names) == 2:
            team1, team2 = team_names
            logger.info(f"bet365 识别球队: {team1} vs {team2}")
        else:
            # 降级：使用传入的球队名
            team1 = get_english_team_name(home)
            team2 = get_english_team_name(away)
            logger.warning(f"bet365 未能从 API 识别球队名，使用映射名: {team1} vs {team2}")

        # 解析赔率数据
        combined_text = text1 + text2
        handicaps = _extract_hd_od(combined_text, [team1, team2])
        result["handicaps"] = handicaps
        result["found"] = len(handicaps) > 0
        result["home"] = team1
        result["away"] = team2
        logger.info(f"bet365 DrissionPage 抓取到 {len(handicaps)} 个盘口赔率")

    except Exception as e:
        logger.error(f"bet365 DrissionPage 抓取失败: {e}")
        result["error"] = str(e)
        try:
            tab.close()
            browser.quit()
        except:
            pass

    return result


async def fetch_bet365_asian_handicap(context, home: str, away: str, bet365_url: str = "") -> dict:
    """
    抓取 bet365 Alternative Asian Handicap 赔率
    优先使用 DrissionPage（真实 Chrome，绕过反爬）
    如果提供了 bet365_url，直接访问；否则尝试搜索
    """
    result = {
        "found": False,
        "url": bet365_url,
        "home": home,
        "away": away,
        "handicaps": [],
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": ""
    }

    home_en = get_english_team_name(home)
    away_en = get_english_team_name(away)
    logger.info(f"bet365 抓取: {home}({home_en}) vs {away}({away_en})")

    # 如果没有直接链接，无法自动搜索（bet365 反爬太强）
    if not bet365_url:
        result["error"] = "需要提供 bet365 比赛直接链接（在页面下方手动输入）"
        logger.warning(f"bet365 未提供直接链接，跳过抓取")
        return result

    # 使用 DrissionPage 抓取（在独立线程中运行，避免阻塞 asyncio）
    if DRISSION_AVAILABLE:
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            drission_result = await loop.run_in_executor(
                pool,
                fetch_bet365_asian_handicap_drission,
                bet365_url, home, away
            )
        return drission_result
    else:
        result["error"] = "DrissionPage 未安装，请运行: pip install DrissionPage"
        return result


def save_bet365_history(record: dict, filepath: str = None):
    """
    将 bet365 赔率快照追加到历史记录文件
    每条记录包含：比赛信息、赔率快照、结果（初始为空）
    """
    if filepath is None:
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet365_history.json")

    history = []
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                history = json.load(f)
        except:
            history = []

    # 检查是否已有同一场比赛的记录（同一天同一场）
    today = datetime.now().strftime("%Y-%m-%d")
    match_key = f"{today}_{record.get('home','')}_{record.get('away','')}"

    # 更新已有记录或追加新记录
    existing_idx = None
    for i, h in enumerate(history):
        if h.get("match_key") == match_key:
            existing_idx = i
            break

    record["match_key"] = match_key
    record["date"] = today

    if existing_idx is not None:
        # 保留已有的结果字段
        old_result = history[existing_idx].get("result", {})
        record["result"] = old_result
        history[existing_idx] = record
        logger.info(f"更新 bet365 历史记录: {match_key}")
    else:
        record["result"] = {
            "home_score": None,
            "away_score": None,
            "settled": False,
            "winning_handicaps": []
        }
        history.append(record)
        logger.info(f"新增 bet365 历史记录: {match_key}")

    # 只保留最近90天的记录
    history = history[-500:]

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# 6. 主扫描函数
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

        # Step 2: 获取 titan007 比赛ID（先获取所有ID，用于后续匹配）
        titan_page = await context.new_page()
        titan_matches = await fetch_titan007_match_ids(titan_page)
        await titan_page.close()

        # Step 3: 为所有 sporttery 比赛匹配 titan007 ID
        matched_matches = []
        for match in sporttery_matches:
            match_id = find_titan007_match_id(match["home"], match["away"], titan_matches)
            if match_id:
                match["match_id"] = match_id
                matched_matches.append(match)
            else:
                logger.debug(f"未找到 {match['home']} vs {match['away']} 的titan007 ID，跳过")

        logger.info(f"成功匹配 {len(matched_matches)}/{len(sporttery_matches)} 场比赛到titan007")

        if not matched_matches:
            logger.warning("没有任何比赛匹配到titan007 ID")
            await browser.close()
            return results

        # Step 4: 热度探针 —— 并发获取每场比赛的 Crow* 记录数
        logger.info(f"开始热度探针，共 {len(matched_matches)} 场比赛...")
        probe_tasks = []
        for match in matched_matches:
            probe_tasks.append(fetch_crow_record_count(context, match["match_id"]))

        # 并发执行（最多5个并发，避免被封）
        record_counts = []
        batch_size = 5
        for i in range(0, len(probe_tasks), batch_size):
            batch = probe_tasks[i:i+batch_size]
            batch_results = await asyncio.gather(*batch, return_exceptions=True)
            for r in batch_results:
                record_counts.append(r if isinstance(r, int) else 0)
            if i + batch_size < len(probe_tasks):
                await asyncio.sleep(1)

        # 将记录数写回比赛数据
        for match, cnt in zip(matched_matches, record_counts):
            match["crow_record_count"] = cnt
            logger.info(f"  [{match['league']}] {match['home']} vs {match['away']}: Crow*记录数={cnt}")

        # Step 5: 计算时间加权热度分，按分数降序排列，选取前 TOP_N_MATCHES 场
        # 热度分 = Crow*记录数 × 时间权重
        # 时间权重规则：距开赛越远，权重越高（早期盘口变化更有价值）
        #   距开赛 > 6小时：权重 = 3.0（早盘，变化最有参考价值）
        #   距开赛 3~6小时：权重 = 2.0
        #   距开赛 1~3小时：权重 = 1.5
        #   距开赛 < 1小时：权重 = 1.0（临场，变化频繁但噪音多）
        now = datetime.now()
        from datetime import timedelta

        for match in matched_matches:
            mt = match.get("match_time", "")
            kickoff_parts = mt.replace("\n", " ").replace("|", " ").split()
            kickoff_time_str = kickoff_parts[-1] if kickoff_parts else ""
            time_weight = 1.0
            try:
                ko_h, ko_m = map(int, kickoff_time_str.split(":"))
                ko_dt = now.replace(hour=ko_h, minute=ko_m, second=0, microsecond=0)
                if ko_dt < now - timedelta(hours=2):
                    ko_dt += timedelta(days=1)
                minutes_to_ko = (ko_dt - now).total_seconds() / 60
                if minutes_to_ko > 360:      # > 6小时
                    time_weight = 3.0
                elif minutes_to_ko > 180:    # 3~6小时
                    time_weight = 2.0
                elif minutes_to_ko > 60:     # 1~3小时
                    time_weight = 1.5
                else:                        # < 1小时
                    time_weight = 1.0
                match["minutes_to_kickoff"] = round(minutes_to_ko, 0)
            except Exception:
                match["minutes_to_kickoff"] = None

            match["time_weight"] = time_weight
            raw_count = match.get("crow_record_count", 0)
            match["heat_score"] = round(raw_count * time_weight, 1)

        # 过滤掉开赛前 1 分钟内的比赛（停止记录）
        matched_matches = [
            m for m in matched_matches 
            if m.get("minutes_to_kickoff") is None or m.get("minutes_to_kickoff") >= 1
        ]

        matched_matches.sort(key=lambda m: m.get("heat_score", 0), reverse=True)
        top_matches = matched_matches[:TOP_N_MATCHES]

        logger.info(f"\n🔥 热度TOP{TOP_N_MATCHES}（按时间加权热度分排序）：")
        for i, m in enumerate(top_matches, 1):
            logger.info(f"  #{i} [{m['league']}] {m['home']} vs {m['away']} "
                        f"(Crow*记录数:{m.get('crow_record_count',0)}, "
                        f"时间权重:{m.get('time_weight',1.0)}x, "
                        f"热度分:{m.get('heat_score',0)}, "
                        f"距开赛:{m.get('minutes_to_kickoff','?')}分钟, "
                        f"编号:{m['match_num']})")

        # Step 6: 对 TOP3 进行完整扫描
        for idx, match in enumerate(top_matches):
            home = match["home"]
            away = match["away"]
            match_id = match["match_id"]

            logger.info(f"完整扫描: {home} vs {away} (ID: {match_id})")

            # 提取开赛时间（格式 "HH:MM"），传给 fetch_crow_detail 做40分钟窗口判断
            mt = match.get("match_time", "")
            kickoff_time = mt.replace("\n", " ").replace("|", " ").split()
            kickoff_time = kickoff_time[-1] if kickoff_time else ""

            result = await fetch_crow_detail(context, match_id, home, away,
                                             match_kickoff=kickoff_time)
            result["league"] = match["league"]
            result["match_time"] = match["match_time"]
            result["match_num"] = match["match_num"]
            result["crow_record_count"] = match.get("crow_record_count", 0)
            result["heat_score"] = match.get("heat_score", 0)
            result["time_weight"] = match.get("time_weight", 1.0)
            result["rank"] = idx + 1  # 热度排名
            results.append(result)

            await asyncio.sleep(1.5)

        # Step 7: 对热度第一名抓取 bet365 Alternative Asian Handicap 赔率
        # 注意：需要在 bet365_history.json 中预先设置 bet365_url 才能抓取
        if top_matches:
            top1 = top_matches[0]
            logger.info(f"\n📊 抓取热度第一名的 bet365 赔率: {top1['home']} vs {top1['away']}")

            # 检查是否有预设的 bet365 链接（从历史记录中读取）
            bet365_url = ""
            history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet365_history.json")
            if os.path.exists(history_file):
                try:
                    with open(history_file, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    today = datetime.now().strftime("%Y-%m-%d")
                    match_key = f"{today}_{top1['home']}_{top1['away']}"
                    for h in history:
                        if h.get("match_key") == match_key and h.get("bet365_url"):
                            bet365_url = h["bet365_url"]
                            logger.info(f"找到预设 bet365 链接: {bet365_url}")
                            break
                except:
                    pass

            bet365_data = await fetch_bet365_asian_handicap(context, top1["home"], top1["away"], bet365_url)
            bet365_data["league"] = top1.get("league", "")
            bet365_data["match_time"] = top1.get("match_time", "")
            bet365_data["heat_score"] = top1.get("heat_score", 0)
            bet365_data["crow_record_count"] = top1.get("crow_record_count", 0)
            bet365_data["time_weight"] = top1.get("time_weight", 1.0)

            if bet365_data.get("found") or bet365_data.get("handicaps"):
                save_bet365_history(bet365_data)
                logger.info(f"✅ bet365 赔率已保存到历史记录")
            else:
                logger.warning(f"⚠️ bet365 未能抓取到赔率: {bet365_data.get('error','')}")
                # 即使没抓到也保存记录（方便追踪）
                save_bet365_history(bet365_data)

        await browser.close()

    return results


# ─────────────────────────────────────────────
# 6. 保存结果到 JSON 文件
# ─────────────────────────────────────────────
def save_results(results: list[dict], filepath: str = None):
    if filepath is None:
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    
    # 尝试加载已有数据进行合并，保留历史记录
    existing_data = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                for m in old_data.get("matches", []):
                    # 以 match_id 作为键保存已有比赛数据
                    if "match_id" in m:
                        existing_data[m["match_id"]] = m
        except Exception as e:
            logger.error(f"合并历史数据失败，将重新创建: {e}")

    # 用新的抓取结果覆盖已有的对应比赛（保留已抓取的 bet365 盘口）
    for r in results:
        if "match_id" in r:
            prev = existing_data.get(r["match_id"], {})
            if prev.get("bet365_handicaps") and not r.get("bet365_handicaps"):
                r["bet365_handicaps"] = prev["bet365_handicaps"]
            if prev.get("bet365_url") and not r.get("bet365_url"):
                r["bet365_url"] = prev["bet365_url"]
            existing_data[r["match_id"]] = r

    # 按照热度分排序并生成最终的 matches 列表
    final_matches = list(existing_data.values())
    final_matches.sort(key=lambda x: x.get("heat_score", 0), reverse=True)

    # 简单的数据清理：只保留今天（或最近12小时内更新过的，或者只是限制总数比如保留当天的50场）
    # 这里我们简化处理，由于每天的 match_id 都不同，可以保留所有合并后的结果，
    # 也可以限制最大保留最近的 30 场比赛。
    final_matches = final_matches[:50]

    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_matches": len(final_matches),
        "alert_count": sum(1 for r in final_matches if r.get("alert")),
        "matches": final_matches
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已合并保存到 {filepath}，当前共 {len(final_matches)} 场比赛记录")


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
