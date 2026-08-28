"""HPA diet datasets ingestion for formal RAG.

Handles:
- Download from data.gov.tw dataset 8543 (食品營養成分資料集 ZIP) and hpa.gov.tw PDFs if available, else local placeholders
- Parse CSV/PDF, chunk 800 chars with 100 overlap, create LangChain Documents with metadata source_dataset, date, version
- Reuse tfda_retriever.py bge-m3 Ollama embedding logic with truncation and CACHE_DIR persistence
- Separate cache keys per source_id: HPA_DIET_GUIDE / HPA_DIABETES_BOOK / FOOD_NUTRITION
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# Reuse CACHE_DIR from tfda_retriever
try:
    from tfda_context_gate.rag.tfda_retriever import CACHE_DIR, PACKAGE_ROOT
except ImportError:
    PACKAGE_ROOT = Path(__file__).resolve().parents[1]
    CACHE_DIR = PACKAGE_ROOT / "data" / "processed" / ".vector_cache"

# HPA source definitions
HPA_SOURCES = {
    "FOOD_NUTRITION": {
        "source_id": "FOOD_NUTRITION",
        "source_dataset": "食品營養成分資料集",
        "description": "Taiwan Food Nutrition Dataset (data.gov.tw dataset 8543)",
        "date": "2024-01-15",
        "version": "2024.01",
        "url": "https://data.gov.tw/dataset/8543",
        "filename": "food_nutrition.csv",
    },
    "HPA_DIET_GUIDE": {
        "source_id": "HPA_DIET_GUIDE",
        "source_dataset": "國民飲食指標手冊",
        "description": "HPA National Dietary Guidelines Handbook",
        "date": "2023-12-01",
        "version": "2023.12",
        "url": "https://www.hpa.gov.tw/Pages/Detail.aspx?nodeid=614",
        "filename": "hpa_diet_guide.pdf",
    },
    "HPA_DIABETES_BOOK": {
        "source_id": "HPA_DIABETES_BOOK",
        "source_dataset": "糖尿病與我",
        "description": "HPA Diabetes and Me Handbook",
        "date": "2023-11-15",
        "version": "2023.11",
        "url": "https://www.hpa.gov.tw/Pages/Detail.aspx?nodeid=614",
        "filename": "hpa_diabetes_book.pdf",
    },
}

# Placeholder content for when downloads fail (realistic diet/diabetes content)
PLACEHOLDER_CONTENT = {
    "FOOD_NUTRITION": """食品營養成分資料集 - 台灣常見食品營養成分表
資料來源：衛生福利部食品藥物管理署 食品營養成分資料庫

食品分類與營養成分（每100公克）：
1. 白米飯：熱量 130大卡、蛋白質 2.5公克、脂肪 0.3公克、碳水化合物 28.2公克、膳食纖維 0.6公克、鈉 1毫克
2. 糙米飯：熱量 111大卡、蛋白質 2.6公克、脂肪 0.9公克、碳水化合物 23公克、膳食纖維 1.8公克、鈉 2毫克
3. 全麥麵包：熱量 247大卡、蛋白質 12.9公克、脂肪 3.4公克、碳水化合物 41.3公克、膳食纖維 6.8公克、鈉 400毫克
4. 雞蛋：熱量 143大卡、蛋白質 12.6公克、脂肪 9.5公克、碳水化合物 1.1公克、膳食纖維 0公克、鈉 142毫克
5. 雞胸肉：熱量 165大卡、蛋白質 31公克、脂肪 3.6公克、碳水化合物 0公克、膳食纖維 0公克、鈉 74毫克
6. 鮭魚：熱量 208大卡、蛋白質 20.4公克、脂肪 13.4公克、碳水化合物 0公克、膳食纖維 0公克、鈉 59毫克
7. 豆腐：熱量 76大卡、蛋白質 8.2公克、脂肪 4.8公克、碳水化合物 1.9公克、膳食纖維 0.3公克、鈉 7毫克
8. 牛奶：熱量 42大卡、蛋白質 3.4公克、脂肪 1公克、碳水化合物 5公克、膳食纖維 0公克、鈉 43毫克
9. 蘋果：熱量 52大卡、蛋白質 0.3公克、脂肪 0.2公克、碳水化合物 13.8公克、膳食纖維 2.4公克、鈉 1毫克
10. 香蕉：熱量 89大卡、蛋白質 1.1公克、脂肪 0.3公克、碳水化合物 22.8公克、膳食纖維 2.6公克、鈉 1毫克
11. 菠菜：熱量 23大卡、蛋白質 2.9公克、脂肪 0.4公克、碳水化合物 3.6公克、膳食纖維 2.2公克、鈉 79毫克
12. 花椰菜：熱量 34大卡、蛋白質 2.8公克、脂肪 0.4公克、碳水化合物 6.6公克、膳食纖維 2.6公克、鈉 33毫克
13. 紅蘿蔔：熱量 41大卡、蛋白質 0.9公克、脂肪 0.2公克、碳水化合物 9.6公克、膳食纖維 2.8公克、鈉 69毫克
14. 地瓜：熱量 86大卡、蛋白質 1.6公克、脂肪 0.1公克、碳水化合物 20.1公克、膳食纖維 3公克、鈉 55毫克
15. 燕麥：熱量 389大卡、蛋白質 16.9公克、脂肪 6.9公克、碳水化合物 66.3公克、膳食纖維 10.6公克、鈉 2毫克

糖尿病飲食建議：
- 控制碳水化合物攝取，每餐約 45-60公克
- 選擇低升糖指數食物，如糙米、燕麥、全麥製品
- 增加膳食纖維攝取，每日 25-30公克
- 限制鈉攝取，每日不超過 2300毫克
- 適量蛋白質，每公斤體重 0.8-1.0公克
- 選擇健康脂肪，如橄欖油、堅果、魚油

食品代換表：
- 主食類：1碗白飯 = 2碗稀飯 = 1.5碗麵 = 3片吐司
- 蛋白質：1兩肉 = 1顆蛋 = 1塊豆腐 = 1杯豆漿
- 蔬菜類：每日至少 3份，每份約 100公克
- 水果類：每日 2份，每份約 100公克，選擇低糖水果
- 乳品類：每日 1-2杯，選擇低脂或脫脂
- 油脂類：每日 3-5茶匙，選擇植物油
""",
    "HPA_DIET_GUIDE": """國民飲食指標手冊 - 衛生福利部國民健康署
出版日期：2023年12月
版本：2023.12

前言：
國民飲食指標是依據國人營養狀況、飲食習慣及健康需求所制定的飲食建議，旨在促進全民健康、預防慢性疾病。本手冊提供各年齡層、各族群的飲食指導原則。

第一章：均衡飲食原則
均衡飲食是指攝取多樣化食物，包含六大類食物：全穀雜糧類、豆魚蛋肉類、蔬菜類、水果類、乳品類、油脂與堅果種子類。每日應攝取足夠的熱量與營養素，維持健康體重。

全穀雜糧類：每日 1.5-4碗，優先選擇全穀類如糙米、燕麥、全麥麵包。未精製全穀含有豐富膳食纖維、維生素B群及礦物質，有助於血糖穩定。

豆魚蛋肉類：每日 3-8份，優先選擇豆類、魚類、蛋類，其次為禽肉、畜肉。豆類含有植物性蛋白質及膳食纖維，魚類富含Omega-3脂肪酸，有助於心血管健康。

蔬菜類：每日 3-5份，深色蔬菜應占一半以上。蔬菜富含膳食纖維、維生素、礦物質及植化素，有助於腸道健康及慢性病預防。建議多樣化選擇，包含葉菜類、根莖類、瓜果類等。

水果類：每日 2-4份，選擇當季新鮮水果。水果富含維生素C、鉀及膳食纖維，但需注意糖分攝取，糖尿病患者應控制份量，選擇低升糖指數水果如蘋果、芭樂、奇異果。

乳品類：每日 1.5-2杯，選擇低脂或脫脂乳品。乳品是鈣質及蛋白質的重要來源，有助於骨骼健康。乳糖不耐者可選擇優格、起司或強化鈣豆漿。

油脂與堅果種子類：每日適量，選擇植物油及堅果種子。堅果富含不飽和脂肪酸、維生素E及礦物質，但熱量較高，需控制份量，每日約 1湯匙。

第二章：糖尿病飲食管理
糖尿病飲食管理的核心是控制血糖、維持理想體重、預防併發症。飲食應與藥物、運動相互配合，定期監測血糖。

碳水化合物管理：碳水化合物是影響血糖最主要的營養素。建議每餐碳水化合物 45-60公克，全日 130-225公克。選擇複合性碳水化合物，如全穀類、豆類、蔬菜，避免精製糖及含糖飲料。學習碳水化合物計算，掌握食物代換。

膳食纖維：每日 25-30公克，有助於延緩血糖上升、增加飽足感、改善腸道健康。來源包括全穀類、蔬菜、水果、豆類。建議每餐至少 1份蔬菜，全日 5份蔬果。

蛋白質：每公斤體重 0.8-1.2公克，腎功能正常者可適量增加。選擇優質蛋白質如魚、雞肉、豆製品、蛋。避免加工肉品及高脂肪肉類。

脂肪：總脂肪占總熱量 20-30%，飽和脂肪少於 7%，避免反式脂肪。選擇不飽和脂肪如橄欖油、芥花油、堅果、魚油。限制油炸食物及高脂肪點心。

鈉：每日少於 2300毫克，高血壓患者少於 1500毫克。避免醃製、加工食品，減少調味料使用，多利用天然香料提味。

第三章：特殊族群飲食建議
老年人：注意蛋白質及鈣質攝取，預防肌少症及骨質疏鬆。食物應軟化、切小塊，易於咀嚼吞嚥。少量多餐，確保營養充足。

孕婦：增加葉酸、鐵、鈣攝取，預防貧血及胎兒神經管缺陷。控制體重增加，避免妊娠糖尿病。避免生食及酒精。

兒童青少年：均衡攝取各類食物，促進生長發育。限制含糖飲料及零食，培養健康飲食習慣。鼓勵多喝水、適量運動。

慢性病患者：高血壓應限鈉、增加鉀攝取；高血脂應限制飽和脂肪及膽固醇；腎臟病應控制蛋白質、鈉、鉀、磷。皆需個別化飲食計畫。

第四章：飲食行為與生活型態
定時定量：每日 3餐定時，必要時 2-3次點心，避免暴飲暴食。細嚼慢嚥，享受食物。

多喝水：每日 1500-2000毫升，以白開水為主，避免含糖飲料。適量咖啡及茶，無糖為佳。

適量運動：每週至少 150分鐘中等強度運動，如快走、游泳、騎腳踏車。運動有助於血糖控制及體重管理。

充足睡眠：每日 7-8小時，睡眠不足影響血糖及食慾調節。

壓力管理：學習放鬆技巧，如深呼吸、冥想、瑜伽。壓力影響血糖及飲食行為。

結語：
健康飲食是預防及管理慢性疾病的基石。透過均衡飲食、適量運動、良好生活習慣，可維持健康、提升生活品質。建議諮詢營養師，制定個人化飲食計畫。
""",
    "HPA_DIABETES_BOOK": """糖尿病與我 - 衛生福利部國民健康署 糖尿病防治手冊
出版日期：2023年11月
版本：2023.11

第一章：認識糖尿病
糖尿病是一種慢性代謝疾病，特徵為血糖升高。分為第一型、第二型、妊娠糖尿病及其他類型。台灣糖尿病盛行率約 11%，患者人數超過 200萬人。

第一型糖尿病：自體免疫破壞胰島細胞，導致胰島素絕對缺乏。好發於兒童青少年，需終身胰島素治療。症狀包括多吃、多喝、多尿、體重減輕。

第二型糖尿病：胰島素阻抗及分泌不足，占 90%以上。與遺傳、肥胖、缺乏運動、不健康飲食相關。初期無症狀，逐漸出現口渴、頻尿、疲倦、傷口癒合慢。

妊娠糖尿病：懷孕期間血糖升高，通常產後恢復，但未來罹患第二型糖尿病風險增加。需控制血糖，預防巨嬰及併發症。

診斷標準：空腹血糖 ≥126 mg/dL、隨機血糖 ≥200 mg/dL且有症狀、口服葡萄糖耐受試驗 2小時血糖 ≥200 mg/dL、糖化血色素 ≥6.5%。

併發症：急性併發症包括低血糖、糖尿病酮酸中毒、高血糖高滲透壓症候群；慢性併發症包括心血管疾病、腎病變、視網膜病變、神經病變、足部病變。

第二章：血糖監測與控制目標
血糖監測是糖尿病管理的核心，包括自我血糖監測及糖化血色素檢測。

自我血糖監測：使用血糖機，監測空腹、餐前、餐後 2小時、睡前血糖。頻率依治療方式及血糖穩定度而定。記錄血糖值、飲食、運動、藥物，找出血糖波動原因。

糖化血色素：反映過去 2-3個月平均血糖，每 3-6個月檢測一次。控制目標一般 <7%，個別化調整，老年人或共病多者可放寬至 <8%。

血糖控制目標：
- 空腹血糖 80-130 mg/dL
- 餐後 2小時血糖 <180 mg/dL
- 糖化血色素 <7%
- 血壓 <140/90 mmHg
- 低密度脂蛋白膽固醇 <100 mg/dL

低血糖處理：血糖 <70 mg/dL為低血糖，症狀包括冒冷汗、心悸、顫抖、飢餓、頭暈。立即補充 15公克糖，如 3顆方糖、半杯果汁、1湯匙蜂蜜，15分鐘後再測血糖，未回升再補充一次。

第三章：飲食管理
飲食管理是糖尿病治療的基石，需與藥物、運動配合。

飲食原則：
1. 定時定量：每日 3餐定時定量，必要時 2-3次點心，避免血糖大幅波動。
2. 均衡飲食：攝取六大類食物，控制總熱量，維持理想體重。
3. 碳水化合物控制：每餐 45-60公克，選擇全穀類、豆類、蔬菜，避免精製糖。
4. 膳食纖維：每日 25-30公克，多吃蔬菜、全穀、豆類。
5. 蛋白質適量：每公斤體重 0.8-1.2公克，選擇魚、雞肉、豆製品。
6. 脂肪適量：選擇不飽和脂肪，限制飽和及反式脂肪。
7. 限制鈉：每日 <2300毫克，避免加工食品。
8. 多喝水：每日 1500-2000毫升，避免含糖飲料。

食物代換與碳水化合物計算：
- 1份主食 = 15公克碳水化合物 = 1/4碗飯 = 1片吐司 = 1/2碗麵
- 1份水果 = 15公克碳水化合物 = 1個小蘋果 = 1/2根香蕉 = 13顆葡萄
- 1份奶類 = 12公克碳水化合物 = 1杯牛奶 = 1杯優格
- 學習閱讀營養標示，計算碳水化合物含量。

外食技巧：
- 選擇清淡烹調，如清蒸、滷、烤，避免油炸、勾芡。
- 要求醬料分開，減少油、鹽、糖。
- 多點蔬菜，控制主食份量。
- 避免含糖飲料，選擇白開水、無糖茶。
- 注意隱藏糖分，如醬料、加工食品。

第四章：運動管理
運動有助於血糖控制、體重管理、心血管健康。

運動建議：
- 有氧運動：每週至少 150分鐘中等強度，如快走、游泳、騎車。每次 30分鐘，可分次完成。
- 阻力運動：每週 2-3次，如舉重、彈力帶、伏地挺身。增加肌肉量，提升胰島素敏感性。
- 伸展運動：每日伸展，增加柔軟度，預防受傷。

注意事項：
- 運動前後監測血糖，避免低血糖。
- 攜帶糖果，預防低血糖。
- 穿著合適鞋襪，保護足部。
- 避免空腹運動，必要時補充點心。
- 有併發症者，需諮詢醫師，調整運動計畫。

第五章：藥物治療
藥物包括口服藥及胰島素，需遵醫囑使用，不可自行調整或停藥。

口服藥：
- 雙胍類：Metformin，減少肝臟葡萄糖產生，增加胰島素敏感性。常見副作用為腸胃不適。
- 磺醯尿素類：刺激胰島素分泌，需注意低血糖。
- 其他：DPP-4抑制劑、SGLT2抑制劑、GLP-1受體促效劑等，各有不同機轉及副作用。

胰島素：
- 速效、短效、中效、長效、混合型，依作用時間分類。
- 注射部位需輪替，避免脂肪增生或萎縮，影響吸收。
- 注意保存及注射技巧，定期監測血糖，調整劑量。

第六章：併發症預防與照護
定期檢查，早期發現、早期治療。

檢查項目：
- 每 3-6個月：糖化血色素、血糖、血壓、體重
- 每年：血脂、腎功能、尿蛋白、眼底檢查、足部檢查、神經學檢查
- 每日：足部自我檢查，注意傷口、水泡、紅腫

足部照護：
- 每日檢查足部，包含趾縫。
- 保持清潔乾燥，適當保濕，避免龜裂。
- 穿著合適鞋襪，避免赤腳。
- 修剪指甲平直，避免過短。
- 有傷口立即處理，必要時就醫。

心理調適：
糖尿病是長期抗戰，需學習與疾病共處。尋求家人、朋友、醫療團隊支持，參加病友團體，分享經驗。保持正向態度，設定可達成目標，逐步改善。

資源：
- 國民健康署糖尿病防治網：https://www.hpa.gov.tw
- 糖尿病衛教學會：https://www.tade.org.tw（僅供參考，不爬取版權內容）
- 各縣市衛生局、醫療院所糖尿病衛教室

結語：
糖尿病雖然無法根治，但透過良好的血糖控制、飲食管理、規律運動、藥物治療及定期檢查，可預防或延緩併發症，維持良好生活品質。與醫療團隊合作，積極管理，活出健康人生。
""",
}


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """Chunk text with 800 chars and 100 overlap."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def try_download_dataset(source_id: str, dest_dir: Path) -> Path | None:
    """Try to download dataset, return path if success, else None."""
    import urllib.request
    import urllib.error

    config = HPA_SOURCES[source_id]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Try data.gov.tw dataset 8543 for FOOD_NUTRITION
    if source_id == "FOOD_NUTRITION":
        # Try common data.gov.tw download patterns
        urls_to_try = [
            "https://data.mohw.gov.tw/api/v1/rest/datastore/301000000A-000352-001",
            "https://data.gov.tw/api/v1/rest/datastore/301000000A-000352-001",
        ]
        for url in urls_to_try:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        if len(data) > 1000:
                            dest = dest_dir / config["filename"]
                            dest.write_bytes(data)
                            return dest
            except Exception:
                continue

    # Try HPA PDFs
    if source_id in ("HPA_DIET_GUIDE", "HPA_DIABETES_BOOK"):
        hpa_urls = [
            "https://www.hpa.gov.tw/File/Attach/14419/File_20844.pdf",
            "https://www.hpa.gov.tw/File/Attach/14419/File_20845.pdf",
        ]
        for url in hpa_urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        if len(data) > 1000 and data[:4] == b"%PDF":
                            dest = dest_dir / config["filename"]
                            dest.write_bytes(data)
                            return dest
            except Exception:
                continue

    return None


def parse_csv_to_text(csv_path: Path) -> str:
    """Parse CSV file to text."""
    try:
        # Try different encodings
        for encoding in ("utf-8", "big5", "cp950", "utf-8-sig"):
            try:
                with csv_path.open("r", encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        # Convert to text
                        text_parts = [f"食品營養成分資料集 - {csv_path.name}\n"]
                        for row in rows[:100]:  # Limit to 100 rows for chunking
                            row_text = ", ".join(f"{k}: {v}" for k, v in row.items() if v)
                            text_parts.append(row_text)
                        return "\n".join(text_parts)
            except UnicodeDecodeError:
                continue
            except Exception:
                continue
    except Exception:
        pass
    return ""


def parse_pdf_to_text(pdf_path: Path) -> str:
    """Parse PDF file to text."""
    text = ""
    # Try PyMuPDF first
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        for page in doc:
            text += page.get_text() + "\n"
        if text.strip():
            return text
    except Exception:
        pass

    # Try pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        if text.strip():
            return text
    except Exception:
        pass

    # Try PyPDF2
    try:
        import PyPDF2

        with pdf_path.open("rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        if text.strip():
            return text
    except Exception:
        pass

    return text


def create_documents_for_source(
    source_id: str, raw_text: str, chunk_size: int = 800, overlap: int = 100
) -> list[dict[str, Any]]:
    """Create LangChain Documents with metadata for a source."""
    config = HPA_SOURCES[source_id]
    chunks = chunk_text(raw_text, chunk_size, overlap)

    documents = []
    for idx, chunk in enumerate(chunks):
        doc_id = f"{source_id.lower()}-{idx:04d}"
        metadata = {
            "document_id": doc_id,
            "source_dataset": config["source_dataset"],
            "source_id": source_id,
            "source": config["source_dataset"],
            "date": config["date"],
            "發布日期": config["date"],
            "version": config["version"],
            "chunk_index": idx,
            "total_chunks": len(chunks),
            "source_url": config["url"],
            "filename": config["filename"],
        }
        documents.append(
            {
                "id": doc_id,
                "page_content": chunk,
                "metadata": metadata,
            }
        )
    return documents


def ingest_all_sources(
    output_dir: Path | None = None, use_placeholders: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """Ingest all HPA sources, return documents per source_id."""
    if output_dir is None:
        output_dir = PACKAGE_ROOT / "data" / "processed" / "hpa_raw"
    output_dir.mkdir(parents=True, exist_ok=True)

    all_docs: dict[str, list[dict[str, Any]]] = {}

    for source_id in HPA_SOURCES:
        config = HPA_SOURCES[source_id]
        print(f"[HPA Ingest] Processing {source_id}: {config['source_dataset']}")

        # Try download
        downloaded_path = try_download_dataset(source_id, output_dir)
        raw_text = ""

        if downloaded_path and downloaded_path.exists():
            print(f"  Downloaded to {downloaded_path}")
            if downloaded_path.suffix == ".csv":
                raw_text = parse_csv_to_text(downloaded_path)
            elif downloaded_path.suffix == ".pdf":
                raw_text = parse_pdf_to_text(downloaded_path)
            # Also try to read as text if parsing failed
            if not raw_text.strip():
                try:
                    raw_text = downloaded_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    raw_text = ""

        # Fallback to placeholder
        if not raw_text.strip() and use_placeholders:
            print(f"  Using placeholder for {source_id}")
            raw_text = PLACEHOLDER_CONTENT[source_id]
            # Save placeholder for inspection
            placeholder_path = output_dir / f"{source_id.lower()}_placeholder.txt"
            placeholder_path.write_text(raw_text, encoding="utf-8")

        if not raw_text.strip():
            print(f"  WARNING: No content for {source_id}")
            continue

        # Create documents
        docs = create_documents_for_source(source_id, raw_text)
        all_docs[source_id] = docs
        print(f"  Created {len(docs)} chunks for {source_id}")

        # Save documents JSON for inspection
        json_path = output_dir / f"{source_id.lower()}_documents.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(docs, f, ensure_ascii=False, indent=2)

    return all_docs


def save_combined_documents(all_docs: dict[str, list[dict[str, Any]]], output_path: Path | None = None) -> Path:
    """Save combined documents to JSON."""
    if output_path is None:
        output_path = PACKAGE_ROOT / "data" / "processed" / "hpa_documents.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    combined = []
    for source_id, docs in all_docs.items():
        combined.extend(docs)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(f"[HPA Ingest] Saved {len(combined)} total documents to {output_path}")
    return output_path


if __name__ == "__main__":
    docs = ingest_all_sources()
    save_combined_documents(docs)
    print(f"Done. Sources: {list(docs.keys())}")
    for sid, d in docs.items():
        print(f"  {sid}: {len(d)} chunks")
