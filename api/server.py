import os
import requests
import time
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
from flask import Flask, render_template, jsonify, request, send_from_directory


# server.py 파일 자신이 있는 폴더를 기준으로 경로를 잡음
# → 로컬/Vercel 어떤 환경에서 실행되든 항상 같은 폴더의 파일들을 정확히 찾음
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ──────────────────────────────
# ⚙️ 캐시 설정
# ──────────────────────────────
CACHE_TTL = 300  # 캐시 유효 시간(초) — 이 시간 안엔 같은 키워드는 외부 API를 다시 안 부름

CACHE = {}              # { key: {"data": [...], "timestamp": float} }
CACHE_LOCK = threading.Lock()       # CACHE 딕셔너리 자체를 보호

KEY_LOCKS = {}          # { key: threading.Lock() } — 키워드별 크롤링 중복 방지
KEY_LOCKS_META_LOCK = threading.Lock()  # KEY_LOCKS 딕셔너리를 보호

# ──────────────────────────────
# 📝 로깅 설정 — 파일이 아니라 콘솔(stdout)로 출력
# Vercel 서버리스 환경은 파일시스템이 읽기 전용이라 파일 로깅이 실패함.
# 콘솔로 찍으면 로컬에서도 보이고, Vercel 대시보드의 함수 로그에서도 그대로 확인 가능.
# ──────────────────────────────
logger = logging.getLogger("dongbang")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
logger.addHandler(_handler)


def get_key_lock(key):
    """키워드별 락을 없으면 만들고, 있으면 재사용."""
    with KEY_LOCKS_META_LOCK:
        if key not in KEY_LOCKS:
            KEY_LOCKS[key] = threading.Lock()
        return KEY_LOCKS[key]


# ──────────────────────────────
# 🕸️ 외부 API 크롤링
# ──────────────────────────────
def bunjang(key):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(
            "https://api.bunjang.co.kr/api/search/v8/pw/product/specs/keyword",
            headers=headers,
            params={"q": key},
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.info(f"번개장터 API 요청 실패 (key={key}): {e}")
        return []

    payload = response.json()
    blocks = payload.get("data", {}).get("searchSpec", {}).get("uiBlockList", [])

    items = []
    for block in blocks:
        result_list = block.get("searchResponse", {}).get("data", [])
        if isinstance(result_list, list):
            items.extend(result_list)

    return items


def _hellomarket_request(key):
    """헬로마켓 검색 API를 실제로 한 번 호출하는 내부 헬퍼."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(
            "https://www.hellomarket.com/api/search/items",
            headers=headers,
            params={"q": key},
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.info(f"헬로마켓 API 요청 실패 (key={key}): {e}")
        return []

    payload = response.json()
    return payload.get("list", {})


def hellomarket(key):
    items = _hellomarket_request(key)

    # "동방OO" 검색어면 접두어를 뗀 결과도 같이 검색해서 합침 (둘 중 하나에서만 걸리는 매물이 있어서)
    if key[:2] == "동방":
        stripped_key = key[2:]
        stripped_items = _hellomarket_request(stripped_key)

        # itemIdx 기준으로 중복 제거하며 합치기
        seen_idx = {i.get("itemIdx") for i in items}
        for i in stripped_items:
            if i.get("itemIdx") not in seen_idx:
                items.append(i)
                seen_idx.add(i.get("itemIdx"))

    return items


def is_relevant(key, name):
    """검색 키워드와 상품명이 아예 무관하면 False.
    번개장터가 검색결과 외에 추천/인기 상품 블록을 같이 내려줄 때가 있어서,
    키워드 단어 중 하나도 상품명에 없으면 걸러낸다."""
    if not name:
        return False
    name_norm = name.replace(" ", "").lower()
    # 키워드를 공백 기준으로 쪼개서, 2글자 이상인 토큰 중 하나라도 상품명에 포함되면 관련있다고 판단
    tokens = [t for t in key.split() if len(t) >= 2]
    if not tokens:
        tokens = [key]
    # "동방OO" 검색어는 헬로마켓 폴백 검색처럼 "동방"이 빠진 상품명도 있을 수 있어서 같이 확인
    if key[:2] == "동방" and len(key) > 2:
        tokens.append(key[2:])
    for t in tokens:
        if t.replace(" ", "").lower() in name_norm:
            return True
    return False


def fetch_from_apis(key):
    """실제로 외부 사이트를 크롤링해서 통합 결과를 만드는 함수 (캐시 미적용 원본 로직).
    두 사이트를 순차 호출하면 최악의 경우 대기시간이 합산되어 Vercel 함수 실행시간
    제한(무료 플랜 기본 10초)에 걸릴 수 있어서, 동시에 병렬로 호출한다."""
    item = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_bunjang = executor.submit(bunjang, key)
        future_hellomarket = executor.submit(hellomarket, key)

        try:
            lbunjang = future_bunjang.result()
        except Exception as e:
            logger.info(f"번개장터 처리 중 예외 (key={key}): {e}")
            lbunjang = []

        try:
            lhellomarket = future_hellomarket.result()
        except Exception as e:
            logger.info(f"헬로마켓 처리 중 예외 (key={key}): {e}")
            lhellomarket = []

    try:
        for i in lbunjang:
            item.append({
                "name": i.get("name"),
                "price": i.get("price"),
                "url": f"https://m.bunjang.co.kr/products/{i.get('pid')}",
                "img": i.get("productImage"),
                "domain": "bunjang",
            })
    except Exception:
        pass

    try:
        for i in lhellomarket:
            item.append({
                "name": i.get("title"),
                "price": i.get("price"),
                "url": f"https://hellomarket.com/item/{i.get('itemIdx')}",
                "img": (i.get("imageUrl").split("?")[0] if i.get("imageUrl") else None),
                "domain": "hellomarket",
            })
    except Exception:
        pass

    # 🔎 키워드와 무관한 항목(추천/인기 상품 등) 걸러내기
    before_count = len(item)
    item = [i for i in item if is_relevant(key, i.get("name"))]
    filtered_count = before_count - len(item)
    if filtered_count > 0:
        logger.info(f"key={key} 무관 항목 {filtered_count}개 필터링됨 (전체 {before_count}개 중)")

    return item


# ──────────────────────────────
# 🗄️ 캐시를 거친 조회 함수
# ──────────────────────────────
def get_items_cached(key):
    now = time.time()

    # 1) 캐시 확인 (유효하면 바로 반환, 외부 API 호출 없음)
    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and (now - cached["timestamp"] < CACHE_TTL):
            return cached["data"], True  # (데이터, 캐시 히트 여부)

    # 2) 캐시 만료/없음 → 실제 크롤링. 같은 키워드 동시 요청은 락으로 한 번만 실행
    key_lock = get_key_lock(key)
    with key_lock:
        # 락을 기다리는 동안 다른 요청이 이미 채워놨을 수 있으니 한 번 더 확인
        with CACHE_LOCK:
            cached = CACHE.get(key)
            if cached and (time.time() - cached["timestamp"] < CACHE_TTL):
                return cached["data"], True

        data = fetch_from_apis(key)

        with CACHE_LOCK:
            CACHE[key] = {"data": data, "timestamp": time.time()}

        return data, False


# ──────────────────────────────
# 📄 list.json 로드
# ──────────────────────────────
with open(os.path.join(BASE_DIR, "list.json"), "r", encoding="utf-8") as file:
    dlist = json.load(file)


app = Flask(__name__, template_folder=BASE_DIR)


@app.route("/")
def home():
    return render_template("index.html", dlist=dlist)


@app.route("/list")
def test_page():
    return jsonify(dlist)


@app.route("/noimg.webp")
def noimg():
    # index.html의 <img src="noimg.webp">가 정상적으로 로드되도록
    # 폴더 전체를 열어주는 대신 이 파일 하나만 명시적으로 서빙
    return send_from_directory(BASE_DIR, "noimg.webp")


@app.route("/item/<key>")
def item(key):
    # Vercel 환경에서는 경로의 URL 인코딩(%EB%8F%99... 등)이 자동으로 안 풀릴 때가 있어서
    # 명시적으로 디코딩. 이미 디코딩된 상태(로컬 등)여도 다시 적용해도 안전함.
    key = unquote(key)

    # 들어오는 모든 요청은 캐시 여부와 무관하게 항상 기록
    ip = request.remote_addr
    data, from_cache = get_items_cached(key)
    logger.info(f"key={key} ip={ip} cache_hit={from_cache} result_count={len(data)}")

    return jsonify(data)


@app.route("/debug/<key>")
def debug(key):
    """배포 환경에서 외부 API 호출이 실제로 어떤 상태코드/에러를 내는지 바로 확인용.
    문제 원인 파악 후에는 지워도 되는 임시 라우트."""
    key = unquote(key)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    result = {"key": key}

    try:
        r = requests.get(
            "https://api.bunjang.co.kr/api/search/v8/pw/product/specs/keyword",
            headers=headers, params={"q": key}, timeout=8,
        )
        result["bunjang_status"] = r.status_code
        result["bunjang_body_preview"] = r.text[:300]
    except Exception as e:
        result["bunjang_error"] = str(e)

    try:
        r = requests.get(
            "https://www.hellomarket.com/api/search/items",
            headers=headers, params={"q": key}, timeout=8,
        )
        result["hellomarket_status"] = r.status_code
        result["hellomarket_body_preview"] = r.text[:300]
    except Exception as e:
        result["hellomarket_error"] = str(e)

    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, threaded=True)