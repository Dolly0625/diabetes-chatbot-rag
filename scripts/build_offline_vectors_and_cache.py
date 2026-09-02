"""Offline precomputation for:
1. New datasets (food_nutrition, hpa_diet_guide, hpa_diabetes_book) -> nutrition_diet_chunks_embedded.json
2. Classic FAQ queries -> query_vector_cache.json
"""

import json
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "diabetes-rag" / "src"))
from rag_retrieval.embedding import embed_document, embed_query

DATA_DIR = Path(__file__).resolve().parents[1] / "diabetes-rag" / "src" / "rag_retrieval" / "data"
HPA_RAW_DIR = Path(__file__).resolve().parents[1] / "tfda_context_gate" / "data" / "processed" / "hpa_raw"

# Classic FAQ queries for diabetes education
FAQ_QUERIES = [
    # 飲食分配與飲食原則
    "糖尿病可以吃什麼？飲食該怎麼分配？",
    "糖尿病可以吃什麼",
    "糖尿病飲食該怎麼吃",
    "糖尿病飲食原則",
    "糖尿病三餐怎麼吃",
    "糖尿病外食怎麼吃",
    "糖尿病可以吃白飯嗎",
    "糖尿病可以吃地瓜嗎",
    "糖尿病可以吃燕麥嗎",
    "糖尿病可以吃麵包嗎",
    "糖尿病可以吃稀飯嗎",
    "糖尿病可以吃哪些水果",
    "糖尿病可以吃水果嗎",
    "糖尿病可以吃芭樂嗎",
    "糖尿病可以吃蘋果嗎",
    "糖尿病可以吃香蕉嗎",
    "糖尿病可以喝牛奶嗎",
    "糖尿病可以喝豆漿嗎",
    "糖尿病可以吃蛋嗎",
    "糖尿病可以吃肉嗎",
    "糖尿病可以吃甜點嗎",
    "糖尿病可以喝酒嗎",
    "糖尿病一天可以吃多少碳水化合物",
    "糖尿病熱量計算",
    "糖尿病食物代換表",
    "全穀雜糧類一天吃多少",
    "低升糖指數食物有哪些",
    "六大類食物有哪些",
    
    # 低血糖急症與處理
    "低血糖怎麼辦",
    "低血糖處理",
    "低血糖症狀有哪些",
    "低血糖會怎樣",
    "冒冷汗手抖是低血糖嗎",
    "嚴重低血糖怎麼處理",
    "低血糖吃什麼",
    "15-15法則是什麼",
    "睡前低血糖怎麼辦",
    "運動後低血糖怎麼辦",
    "半夜低血糖症狀",
    
    # 血糖指標與標準
    "空腹血糖標準是多少",
    "飯後血糖標準是多少",
    "糖尿病診斷標準",
    "糖化血色素是什麼",
    "糖化血色素標準是多少",
    "糖化血色素多久驗一次",
    "自我血糖監測方法",
    "血糖一天要測幾次",
    "血糖太高怎麼辦",
    "高血糖症狀有哪些",
    
    # 用藥與胰島素
    "Metformin 怎麼吃",
    "Metformin 副作用",
    "二甲雙胍是什麼",
    "糖尿病藥忘記吃怎麼辦",
    "胰島素一定要打一輩子嗎",
    "胰島素注射部位",
    "胰島素怎麼保存",
    "糖尿病可以自己停藥嗎",
    "SGLT2抑制劑副作用",
    "DPP4抑制劑是什麼",
    "糖尿病藥物副作用",
    
    # 運動與生活作息
    "糖尿病運動建議",
    "糖尿病適合做什麼運動",
    "運動前後要注意什麼",
    "糖尿病可以重訓嗎",
    "糖尿病要睡多久",
    "熬夜會影響血糖嗎",
    "壓力大血糖會高嗎",
    
    # 併發症與照護
    "糖尿病併發症有哪些",
    "糖尿病足部護理",
    "糖尿病視網膜病變",
    "糖尿病腎病變症狀",
    "糖尿病傷口難癒合怎麼辦",
    "糖尿病神經病變症狀",
    "糖尿病酮酸中毒症狀",
]


def build_new_documents_vectors():
    print("=== 1. Building vectors for new documents ===")
    out_file = DATA_DIR / "nutrition_diet_chunks_embedded.json"
    
    source_files = [
        ("FOOD_NUTRITION", "food_nutrition_documents.json", "food-nutrition", "2024-01-15", "2024.01"),
        ("HPA_DIET_GUIDE", "hpa_diet_guide_documents.json", "hpa-diet-guide", "2023-12-01", "2023.12"),
        ("HPA_DIABETES_BOOK", "hpa_diabetes_book_documents.json", "hpa-diabetes-book", "2023-11-15", "2023.11"),
    ]
    
    records = []
    for source_id, fname, source_slug, default_date, default_ver in source_files:
        fpath = HPA_RAW_DIR / fname
        if not fpath.is_file():
            print(f"Skipping {fname}: file not found")
            continue
        with open(fpath, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        
        print(f"Embedding {len(items)} chunks from {fname}...")
        for item in items:
            cid = item.get("id") or item.get("metadata", {}).get("document_id", f"{source_slug}_chunk")
            content = item.get("page_content", "").strip()
            if not content:
                continue
            meta = item.get("metadata", {})
            date_str = meta.get("date") or default_date
            ver_str = meta.get("version") or default_ver
            
            print(f"  Embedding doc chunk: {cid} ({len(content)} chars)...")
            emb = embed_document(content)
            records.append({
                "chunk_id": cid,
                "source": source_slug,
                "version": ver_str,
                "date": date_str,
                "status": "active",
                "content": content,
                "retriever": "vector",
                "embedding_dim": len(emb),
                "embedding": emb,
            })
            time.sleep(0.2)
            
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    print(f"Saved {len(records)} document chunks to {out_file}")


def build_faq_query_vectors():
    print("\n=== 2. Building FAQ Query Vector Cache ===")
    cache_file = DATA_DIR / "query_vector_cache.json"
    cache = {}
    if cache_file.is_file():
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
            print(f"Existing cache has {len(cache)} entries.")
        except Exception:
            cache = {}

    queries_to_embed = [q for q in FAQ_QUERIES if q not in cache]
    print(f"Need to embed {len(queries_to_embed)} queries...")
    
    for i, q in enumerate(queries_to_embed):
        print(f"  [{i+1}/{len(queries_to_embed)}] Embedding query: {q}")
        try:
            emb = embed_query(q)
            cache[q] = emb
            time.sleep(0.15)
        except Exception as e:
            print(f"  Error embedding {q}: {e}")
            
    with open(cache_file, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    print(f"Saved {len(cache)} query vectors to {cache_file}")


if __name__ == "__main__":
    build_new_documents_vectors()
    build_faq_query_vectors()
