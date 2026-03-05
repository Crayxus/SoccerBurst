import time
import json
from DrissionPage import Chromium, ChromiumOptions

def scrape_bet365_alt_asian_lines(match_url: str):
    """
    Crayxus ♠️ 专用：获取 Bet365 附加亚洲让球盘口数据
    :param match_url: bet365 比赛的完整 URL，如 https://www.bet365.com.au/#/AC/B1/C1/D8/E189510146/F3/I0/
    """
    print(f"♠️ [Crayxus] 正在启动隐身浏览器连接目标: {match_url}")
    co = ChromiumOptions()
    co.incognito()
    co.headless(False)  # 本地调试时建议开启可见，确认元素位置
    
    browser = Chromium(co)
    page = browser.latest_tab
    
    try:
        # 1. 访问比赛主页
        page.get(match_url)
        page.wait.load_start()
        print("♠️ 页面加载完成，寻找 'Asian Lines' 标签...")
        time.sleep(3) # 缓冲等待反爬检测
        
        # 2. 点击 Asian Lines 标签
        # 找包含文本 'Asian Lines' 的元素，通常是顶部的导航 Tab
        asian_lines_tab = page.ele('text=Asian Lines', timeout=10)
        if not asian_lines_tab:
            print("❌ 未找到 'Asian Lines' 标签，可能比赛不支持或未加载完成")
            return []
            
        asian_lines_tab.click()
        print("♠️ 已点击 'Asian Lines'，正在寻找附加盘口...")
        time.sleep(2)
        
        # 3. 点击 Alternative Asian Handicap 展开面板
        alt_asian_header = page.ele('text=Alternative Asian Handicap', timeout=10)
        if alt_asian_header:
            # 检查是否已经展开，如果没有则点击
            parent_container = alt_asian_header.parent(3) # 根据 DOM 结构上溯寻找状态
            # 直接尝试点击展开
            alt_asian_header.click()
            print("♠️ 已点击展开 'Alternative Asian Handicap'")
            time.sleep(1)
        else:
            print("❌ 未找到 'Alternative Asian Handicap' 面板")
            return []
            
        # 4. 抓取下方的所有盘口行
        # 这里需要精准定位盘口表格。根据常见的 bet365 结构，它下方会有多行数据
        # 我们可以通过查找包含盘口数字（如 -1.5, -1.0, 0.0）的行列来解析
        results = []
        
        # 定位 Alternative Asian Handicap 所在的区块
        block = alt_asian_header.parent('.gl-MarketGroup') if alt_asian_header.parent('.gl-MarketGroup') else alt_asian_header.parent(5)
        
        # 在该区块内寻找所有的 odds 行
        # 假设左侧是主队盘口，右侧是客队盘口，或者依次排列
        rows = block.eles('.gl-ParticipantCentered_Name') # 这是一个常见的类名，如果找不到可以根据实际情况微调
        if not rows:
            # 备用方案：抓取区块内的所有文本，进行正则解析
            print("♠️ 尝试使用备用文本解析法读取盘口矩阵...")
            block_text = block.text
            print("--- 抓取到的区块文本 ---")
            print(block_text)
            print("------------------------")
            # 在这里可以编写更精细的解析逻辑
            # 例如解析 " -1.5 6.000 " 这样的格式
            
            return block_text

        print(f"♠️ 成功读取附加盘口区块。")
        return results

    except Exception as e:
        print(f"❌ 抓取过程发生异常: {e}")
    finally:
        browser.quit()

if __name__ == "__main__":
    # 测试样例 URL
    sample_url = "https://www.bet365.com.au/#/AC/B1/C1/D8/E189510146/F3/I0/"
    scrape_bet365_alt_asian_lines(sample_url)
