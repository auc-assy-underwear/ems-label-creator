"""
RMS EMS注文自動処理スクリプト
フェーズ3: ドライラン

DRY_RUN = True の間は updateOrder を呼ばずペイロードを表示するだけです。
"""

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
SERVICE_SECRET = os.getenv("RMS_SERVICE_SECRET", "")
LICENSE_KEY = os.getenv("RMS_LICENSE_KEY", "")
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", "30"))

if not SERVICE_SECRET or not LICENSE_KEY:
    raise SystemExit("ERROR: .env に RMS_SERVICE_SECRET と RMS_LICENSE_KEY を設定してください")

API_BASE = "https://api.rms.rakuten.co.jp/es/2.0/order"
JST = timezone(timedelta(hours=9))

# ドライランフラグ: True=APIを呼ばずペイロードを表示、False=実際に更新
DRY_RUN = False

# ──────────────────────────────────────────────
# 判定ロジック定数（Tampermonkeyスクリプトと同一ルール）
# ──────────────────────────────────────────────

# EMS非対象国：これらが city に含まれていたらスキップ
EXCLUDED_KEYWORDS = [
    "日本", "Japan",
    "United States", "アメリカ",
    "United Kingdom", "英国", "イギリス",
    "Australia", "オーストラリア",
    "Singapore", "シンガポール",
    "New Zealand", "ニュージーランド",
]

# ゾーン定義：送料（円）とマッチキーワード
ZONES = [
    {
        "region": "アジア", "fee": 1500,
        "keywords": [
            "China", "中国", "Korea", "韓国",
            "Taiwan", "台湾", "台灣", "臺灣",
            "CN", "TW",
        ],
    },
    {
        "region": "アジア", "fee": 1900,
        "keywords": [
            "Hong Kong", "香港", "Macau", "マカオ",
            "Thailand", "タイ",
            "Malaysia", "マレーシア",
            "Philippines", "フィリピン",
            "Viet Nam", "ベトナム",
            "Indonesia", "インドネシア",
            "India", "インド",
            "Brunei", "ブルネイ",
            "HK", "MY",
        ],
    },
    {
        "region": "オセアニア", "fee": 3150,
        "keywords": ["Fiji", "フィジー", "Papua", "パプア"],
    },
    {
        "region": "北米", "fee": 3150,
        "keywords": ["Canada", "カナダ"],
    },
    {
        "region": "ヨーロッパ", "fee": 3150,
        "keywords": [
            "Europe", "ヨーロッパ",
            "Italy", "イタリア", "Germany", "ドイツ",
            "Czech", "チェコ", "Iceland", "アイスランド",
            "Estonia", "エストニア", "Austria", "オーストリア",
            "Netherlands", "オランダ", "Cyprus", "キプロス",
            "Greece", "ギリシャ", "Croatia", "クロアチア",
            "San Marino", "サンマリノ", "Switzerland", "スイス",
            "Sweden", "スウェーデン", "Spain", "スペイン",
            "Slovakia", "スロバキア", "Slovenia", "スロベニア",
            "Denmark", "デンマーク", "Hungary", "ハンガリー",
            "Finland", "フィンランド", "Bulgaria", "ブルガリア",
            "Belgium", "ベルギー", "Poland", "ポーランド",
            "Portugal", "ポルトガル", "Malta", "マルタ",
            "Monaco", "モナコ", "Latvia", "ラトビア",
            "Lithuania", "リトアニア", "Liechtenstein", "リヒテンシュタイン",
            "Romania", "ルーマニア", "Luxembourg", "ルクセンブルク",
            "Ireland", "アイルランド", "France", "フランス",
            "Norway", "ノルウェー",
        ],
    },
    {
        "region": "中南米", "fee": 3600,
        "keywords": [
            "Mexico", "メキシコ", "Panama", "パナマ", "Costa Rica", "コスタリカ",
        ],
    },
    {
        "region": "中東", "fee": 3150,
        "keywords": [
            "Middle East", "中東",
            "United Arab Emirates", "アラブ首長国連邦", "UAE",
            "Israel", "イスラエル",
            "Turkey", "トルコ",
            "Saudi Arabia", "サウジアラビア",
        ],
    },
    {
        "region": "アフリカ", "fee": 3600,
        "keywords": [
            "Africa", "アフリカ",
            "Egypt", "エジプト",
            "South Africa", "南アフリカ",
            "Morocco", "モロッコ",
        ],
    },
]

# 配送不可の可能性がある地域（現在EMSが停止中の可能性）
WARNING_REGIONS = ["ヨーロッパ", "中東", "アフリカ"]


# ──────────────────────────────────────────────
# 判定結果データクラス
# ──────────────────────────────────────────────

@dataclass
class OrderVerdict:
    order_number: str
    is_overseas: bool
    postage_already_set: bool
    skip_reason: Optional[str]   # None = 処理が必要
    country_keyword: str         # マッチしたキーワード
    region: str                  # ゾーン名
    fee: Optional[int]           # 適用する送料（円）
    tax_exempt_needed: bool      # 税率を0%にする必要があるか
    is_warning_region: bool      # 配送不可の可能性
    city_text: str               # SenderModel.city の生テキスト


# ──────────────────────────────────────────────
# 判定ロジック
# ──────────────────────────────────────────────

def _match_keyword(text: str, keyword: str) -> bool:
    """キーワードマッチ。2文字大文字英字は単語境界、それ以外は部分一致（大文字小文字無視）"""
    if len(keyword) == 2 and keyword.isupper() and keyword.isalpha():
        return bool(re.search(rf'\b{re.escape(keyword)}\b', text, re.IGNORECASE))
    return keyword.lower() in text.lower()


def classify_order(order: dict) -> OrderVerdict:
    """注文を分析し、処理要否・送料・免税の判定結果を返す（更新は行わない）"""
    order_number = order.get("orderNumber", "不明")
    packages = order.get("PackageModelList", [])
    sender = packages[0].get("SenderModel", {}) if packages else {}

    # 海外注文判定
    prefecture = sender.get("prefecture", "")
    zip1 = sender.get("zipCode1", "")
    zip2 = sender.get("zipCode2", "")
    is_overseas = (prefecture == "国外" and zip1 == "000" and zip2 == "0000")

    if not is_overseas:
        return OrderVerdict(
            order_number=order_number, is_overseas=False,
            postage_already_set=False, skip_reason="国内注文",
            country_keyword="", region="", fee=None,
            tax_exempt_needed=False, is_warning_region=False,
            city_text=prefecture,
        )

    # 送料設定済みチェック（-9999が未設定の初期値）
    postage = packages[0].get("postagePrice") if packages else None
    if postage is not None and postage != -9999:
        return OrderVerdict(
            order_number=order_number, is_overseas=True,
            postage_already_set=True, skip_reason="送料設定済み",
            country_keyword="", region="", fee=None,
            tax_exempt_needed=False, is_warning_region=False,
            city_text=sender.get("city", ""),
        )

    city_text = sender.get("city", "")

    # 除外キーワードチェック（EMS非対象国）
    for word in EXCLUDED_KEYWORDS:
        if _match_keyword(city_text, word):
            return OrderVerdict(
                order_number=order_number, is_overseas=True,
                postage_already_set=False, skip_reason=f"対象外国: {word}",
                country_keyword=word, region="", fee=None,
                tax_exempt_needed=False, is_warning_region=False,
                city_text=city_text,
            )

    # ゾーン判定（送料決定）
    for zone in ZONES:
        for kw in zone["keywords"]:
            if _match_keyword(city_text, kw):
                region = zone["region"]
                return OrderVerdict(
                    order_number=order_number, is_overseas=True,
                    postage_already_set=False, skip_reason=None,
                    country_keyword=kw, region=region, fee=zone["fee"],
                    tax_exempt_needed=True,
                    is_warning_region=region in WARNING_REGIONS,
                    city_text=city_text,
                )

    # 未登録国
    return OrderVerdict(
        order_number=order_number, is_overseas=True,
        postage_already_set=False, skip_reason="未登録の国/地域",
        country_keyword="", region="", fee=None,
        tax_exempt_needed=False, is_warning_region=False,
        city_text=city_text,
    )


# ──────────────────────────────────────────────
# API呼び出し
# ──────────────────────────────────────────────

def _auth_header() -> dict:
    token = base64.b64encode(f"{SERVICE_SECRET}:{LICENSE_KEY}".encode()).decode()
    return {
        "Authorization": f"ESA {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def search_orders(start_dt: datetime, end_dt: datetime) -> list[str]:
    """注文確認待ち（ステータス100）の注文番号リストを返す"""
    payload = {
        "dateType": 1,
        "startDatetime": start_dt.strftime("%Y-%m-%dT%H:%M:%S+0900"),
        "endDatetime": end_dt.strftime("%Y-%m-%dT%H:%M:%S+0900"),
        "orderProgressList": [100],  # 注文確認待ちのみ
        "PaginationRequestModel": {
            "requestRecordsAmount": 100,
            "requestPage": 1,
            "SortModelList": [
                {"sortColumn": 1, "sortDirection": 1}
            ],
        },
    }
    resp = requests.post(
        f"{API_BASE}/searchOrder",
        headers=_auth_header(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("MessageModelList"):
        for msg in data["MessageModelList"]:
            print(f"  [API MSG] {msg.get('messageType')} {msg.get('message')}")

    order_numbers = data.get("orderNumberList") or []
    total = (data.get("PaginationResponseModel") or {}).get("totalRecordsAmount") or 0
    print(f"  searchOrder: {total}件ヒット / 今回取得: {len(order_numbers)}件")
    return order_numbers


def get_order(order_number: str) -> dict | None:
    """注文番号から注文詳細を取得する"""
    payload = {"orderNumberList": [order_number], "version": 10}
    resp = requests.post(
        f"{API_BASE}/getOrder",
        headers=_auth_header(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("MessageModelList"):
        for msg in data["MessageModelList"]:
            print(f"  [API MSG] {msg.get('messageType')} {msg.get('message')}")

    order_list = data.get("OrderModelList", [])
    if not order_list:
        print(f"  WARNING: {order_number} の詳細が取得できませんでした")
        return None
    return order_list[0]


# ──────────────────────────────────────────────
# 表示ヘルパー
# ──────────────────────────────────────────────

def display_verdict(verdict: OrderVerdict):
    """判定結果をターミナルに表示する"""
    num = verdict.order_number

    if not verdict.is_overseas:
        print(f"  [{num}] → 国内注文スキップ")
        return

    if verdict.skip_reason == "送料設定済み":
        print(f"  [{num}] → 送料設定済みスキップ（city: {verdict.city_text}）")
        return

    if verdict.skip_reason and verdict.skip_reason.startswith("対象外国:"):
        print(f"  [{num}] → {verdict.skip_reason}（EMS非対象）")
        return

    if verdict.skip_reason == "未登録の国/地域":
        print(f"  [{num}] ⚠ 未登録の国/地域: '{verdict.city_text}'")
        print(f"           → 手動で確認してください")
        return

    # 処理対象
    warning_str = "  ⚠ 配送不可の可能性あり" if verdict.is_warning_region else ""
    print(f"""
  ┌─ 判定結果: {num} ─────────────────────────
  │ 国/地域   : {verdict.city_text}
  │             → キーワード: {verdict.country_keyword}（{verdict.region}）{warning_str}
  │ 送料      : {verdict.fee:,} 円（現在: -9999 未設定）
  │ 税率      : 10% → 0%（免税）
  │ ※ フェーズ2: 判定のみ（更新は行いません）
  └─────────────────────────────────────────────""")


# ──────────────────────────────────────────────
# 更新ペイロード構築・実行
# ──────────────────────────────────────────────

def build_update_payload(order: dict, verdict: OrderVerdict) -> dict:
    """updateOrder に送るペイロードを組み立てる"""
    packages = order.get("PackageModelList", [])
    pkg = packages[0] if packages else {}

    # 商品ごとの taxRate を 0 にする
    items = []
    for item in pkg.get("ItemModelList", []):
        item_detail_id = item.get("itemDetailId")
        if item_detail_id is not None:
            items.append({
                "itemDetailId": item_detail_id,
                "taxRate": 0,
            })

    payload = {
        "orderNumber": verdict.order_number,
        "PackageModelList": [
            {
                "postagePrice": verdict.fee,       # 送料設定
                "postageTaxRate": 0,               # 送料の税率 → 0%
                "deliveryTaxRate": 0,              # 配送料の税率 → 0%
                "deliveryName": "国際配送",        # 配送方法
                "ItemModelList": items,            # 商品税率 → 0%
            }
        ],
    }
    return payload


def call_update_order(payload: dict) -> bool:
    """
    updateOrder を呼ぶ。DRY_RUN=True のときはペイロードを表示するだけ。
    成功したら True を返す。
    """
    if DRY_RUN:
        print("\n  [DRY RUN] updateOrder に送信予定のペイロード:")
        print(json.dumps(payload, ensure_ascii=False, indent=4))
        print("  [DRY RUN] ※ 実際の更新は行いません（DRY_RUN=True）")
        return True

    resp = requests.post(
        f"{API_BASE}/updateOrder",
        headers=_auth_header(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    messages = data.get("MessageModelList", [])
    for msg in messages:
        msg_type = msg.get("messageType", "")
        msg_text = msg.get("message", "")
        print(f"  [API MSG] {msg_type} {msg_text}")

    # エラーメッセージがあれば失敗とみなす
    has_error = any(m.get("messageType") == "ERROR" for m in messages)
    if has_error:
        return False

    print(f"  ✓ updateOrder 成功: {payload['orderNumber']}")
    return True


def process_order(order: dict, verdict: OrderVerdict) -> bool:
    """判定済み注文を実際に処理する（ドライランまたは本番）"""
    payload = build_update_payload(order, verdict)
    return call_update_order(payload)


def _extract_sender(order: dict) -> dict:
    packages = order.get("PackageModelList", [])
    if not packages:
        return {}
    return packages[0].get("SenderModel", {}) or {}


def _extract_postage(order: dict) -> int | None:
    packages = order.get("PackageModelList", [])
    if not packages:
        return None
    return packages[0].get("postagePrice")


# ──────────────────────────────────────────────
# メインループ
# ──────────────────────────────────────────────

def run_once():
    """1回分のポーリング処理（読み取り＋判定のみ）"""
    now = datetime.now(JST)
    start = now - timedelta(minutes=35)  # 本番: 直近35分

    print(f"\n{'='*60}")
    print(f"  ポーリング開始: {now.strftime('%Y-%m-%d %H:%M:%S JST')}")
    print(f"  検索期間: {start.strftime('%H:%M')} 〜 {now.strftime('%H:%M')}（35分）")
    print(f"{'='*60}")

    order_numbers = search_orders(start, now)

    if not order_numbers:
        print("  → 対象注文なし")
        return

    needs_action = 0
    skipped = 0
    unknown = 0

    for order_number in order_numbers:
        print(f"\n  処理中: {order_number}")
        order = get_order(order_number)
        if not order:
            continue

        verdict = classify_order(order)
        display_verdict(verdict)

        if verdict.skip_reason is None:
            process_order(order, verdict)
            needs_action += 1
        elif verdict.skip_reason == "未登録の国/地域":
            unknown += 1
        else:
            skipped += 1

    dry_label = "【ドライラン】" if DRY_RUN else "【本番】"
    print(f"\n{'='*60}")
    print(f"  ポーリング完了 {dry_label}")
    print(f"  処理={needs_action}件 / スキップ={skipped}件 / 要確認={unknown}件")
    print(f"{'='*60}")


def fetch_single(order_number: str):
    """指定した注文番号1件だけ判定して表示する（デバッグ用）"""
    print("=" * 60)
    print(f"  注文番号指定モード: {order_number}")
    print("  ※ updateOrder系APIは一切呼び出しません")
    print("=" * 60)
    order = get_order(order_number)
    if order:
        verdict = classify_order(order)
        display_verdict(verdict)

        if verdict.skip_reason is None:
            process_order(order, verdict)

        # デバッグ用：判定に関係するフィールドのみ出力
        packages = order.get("PackageModelList", [])
        sender = packages[0].get("SenderModel", {}) if packages else {}
        print("\n  [判定に使用したフィールド]")
        print(f"    SenderModel.prefecture : {sender.get('prefecture')}")
        print(f"    SenderModel.zipCode1   : {sender.get('zipCode1')}")
        print(f"    SenderModel.zipCode2   : {sender.get('zipCode2')}")
        print(f"    SenderModel.city       : {sender.get('city')}")
        print(f"    postagePrice           : {packages[0].get('postagePrice') if packages else 'N/A'}")
        print()

        # 税率フィールドを探す（フェーズ3確認用）
        print("  [税率関連フィールド候補（フェーズ3確認用）]")
        _find_tax_fields(order)
    else:
        print("  取得できませんでした")


def _find_tax_fields(order: dict, prefix: str = "", depth: int = 0):
    """注文データから taxRate / tax 関連フィールドを再帰的に探す"""
    if depth > 5:
        return
    if isinstance(order, dict):
        for key, val in order.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if any(kw in key.lower() for kw in ["tax", "消費税", "zei"]):
                print(f"    {full_key}: {val}")
            _find_tax_fields(val, full_key, depth + 1)
    elif isinstance(order, list):
        for i, item in enumerate(order[:3]):
            _find_tax_fields(item, f"{prefix}[{i}]", depth + 1)


def main():
    import sys
    if len(sys.argv) > 1:
        fetch_single(sys.argv[1])
        return

    dry_label = "フェーズ3: ドライラン（DRY_RUN=True）" if DRY_RUN else "フェーズ4以降: 本番モード（DRY_RUN=False）"
    print("=" * 60)
    print("  RMS EMS注文自動処理スクリプト")
    print(f"  {dry_label}")
    if DRY_RUN:
        print("  ※ updateOrder は呼ばずペイロードを表示します")
    print("=" * 60)
    print(f"  ポーリング間隔: {POLLING_INTERVAL}秒")
    print("  Ctrl+C で停止")
    print()

    try:
        while True:
            try:
                run_once()
            except requests.exceptions.HTTPError as e:
                print(f"  [HTTP ERROR] {e}")
                print(f"  レスポンス: {e.response.text[:500]}")
            except requests.exceptions.RequestException as e:
                print(f"  [NETWORK ERROR] {e}")
            except Exception as e:
                print(f"  [ERROR] {type(e).__name__}: {e}")

            print(f"\n  次回ポーリングまで {POLLING_INTERVAL}秒待機...")
            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n  停止しました（Ctrl+C）")


if __name__ == "__main__":
    main()
