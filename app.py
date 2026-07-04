import os
import re
import cv2
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from PIL import Image
from pyzbar.pyzbar import decode
from streamlit_webrtc import webrtc_streamer, WebRtcMode, VideoTransformerBase
from supabase import create_client, Client

# ==============================================================================
# 0. 環境設定・安全な初期化
# ==============================================================================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 本番公開時およびGitHub共有時のオープンソース規約に準拠（シークレットのハードコード完全排除）
if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("【環境変数エラー】.envファイル、またはデプロイ環境の設定に 'SUPABASE_URL' と 'SUPABASE_KEY' を指定してください。")
    st.stop()

# Supabaseクライアントの初期化
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ページ基本設定（レスポンシブWeb対応のワイドレイアウト強制）
st.set_page_config(
    page_title="蔵書管理システム 📚",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# セッション状態（State）の厳格な初期化管理
if "current_series" not in st.session_state:
    st.session_state.current_series = None         # 現在ドリルダウン展開しているシリーズフォルダ名
if "last_scanned_isbn" not in st.session_state:
    st.session_state.last_scanned_isbn = None     # 直近にカメラが検知したISBN一時バッファ
if "scan_history" not in st.session_state:
    st.session_state.scan_history = []             # バーコード連続登録の成功履歴リスト
if "user_session" not in st.session_state:
    # 認証要件に基づくモックセッション（本番実装時はSupabase Auth経由のUUIDを動的に割当）
    st.session_state.user_session = {
        "id": "00000000-0000-0000-0000-000000000000", 
        "email": "demo-user@example.com"
    }

USER_ID = st.session_state.user_session["id"]

# ==============================================================================
# 1. フロントエンド連携：JavaScript注入ユーティリティ（触覚フィードバック）
# ==============================================================================
def trigger_device_vibration(pattern_type: str):
    """
    ブラウザのVibration APIを直接制御するJavaScriptをインジェクションする。
    画面を見ずとも手触りで登録成否を識別可能にするためのUX要件の実装。
    """
    patterns = {
        "success": "100",                 # 正常登録：短く1回「ブルッ」
        "duplicate": "300",               # 重複警告：少し長めに「ブーーー」
        "failed": "[100, 50, 100]"        # エラー/該当なし：短く2回「ブッ、ブッ」
    }
    pattern = patterns.get(pattern_type, "100")
    js_script = f"""
    <script>
    if (navigator.vibrate) {{
        navigator.vibrate({pattern});
    }}
    </script>
    """
    st.components.v1.html(js_script, height=0, width=0, scroller=False)

# ==============================================================================
# 2. データアクセス・外部API連携層（ビジネスロジック）
# ==============================================================================
def fetch_book_metadata_from_openbd(isbn: str) -> dict:
    """
    openBD APIにリクエストを送り、JLA/ONIX準拠の書誌データをパースして取得する。
    """
    # ISBNの簡易バリデーション（数字以外を除去）
    clean_isbn = re.sub(r"\D", "", isbn)
    if not clean_isbn:
        return None
        
    url = f"https://api.openbd.jp/v1/get?isbn={clean_isbn}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            res_json = response.json()
            if res_json and res_json[0] is not None:
                summary = res_json[0].get("summary", {})
                onix = res_json[0].get("onix", {})
                
                # あらすじ・詳細テキスト（TextContent）の安全な多階層パース
                description = "詳細情報（あらすじ）は提供されていません。"
                try:
                    text_content_list = onix.get("CollateralDetail", {}).get("TextContent", [])
                    if text_content_list:
                        description = text_content_list[0].get("Text", description)
                except Exception:
                    pass
                
                return {
                    "isbn": clean_isbn,
                    "title": summary.get("title", "名称未設定の書籍"),
                    "subtitle": summary.get("subtitle", ""),
                    "author": summary.get("author", "著者不明"),
                    "publisher": summary.get("publisher", "出版社不明"),
                    "volume": int(summary.get("volume")) if summary.get("volume") and str(summary.get("volume")).isdigit() else 1,
                    "series": summary.get("series", ""),
                    "pubdate": summary.get("pubdate", ""),
                    "cover": summary.get("cover", ""),
                    "description": description
                }
    except Exception as e:
        st.log(f"API通信エラー: {str(e)}")
    return None

def upsert_book_to_supabase(book_payload: dict) -> tuple:
    """
    Supabaseのbooksテーブルへレコードの登録を試みる。
    unique_user_isbnユニーク制約に基づく重複ハンドリングを行う。
    """
    book_payload["user_id"] = USER_ID
    try:
        res = supabase.table("books").insert(book_payload).execute()
        if res.data:
            return "success", res.data[0]
    except Exception as e:
        err_msg = str(e)
        if "unique_user_isbn" in err_msg or "duplicate key" in err_msg:
            return "duplicate", None
        return "error", err_msg
    return "error", "未知のデータベース割り込みレスポンス"

# ==============================================================================
# 3. 画面プレゼンテーション層（Streamlit UI設計）
# ==============================================================================

# PC最大6列 / スマホ最大3列ルールをエミュレートするためのレスポンシブ幅判定トグル
is_mobile_width = st.sidebar.checkbox("📱 スマホ表示モード (最大3列) でシミュレート", value=False)
layout_columns_limit = 3 if is_mobile_width else 6

# 画面最上部の固定タブメニュー（画面仕様書に基づくシームレスなメニュー切り替え）
app_tabs = st.tabs(["📚 蔵書一覧・検索", "📷 バーコード高速登録", "⚙️ システムデータ管理"])

# ------------------------------------------------------------------------------
# 3.1 【画面1】書籍一覧画面（メイン・ドリルダウン対応）
# ------------------------------------------------------------------------------
with app_tabs[0]:
    
    # CASE A: 特定のシリーズフォルダのドリルダウン（単巻化）展開モード
    if st.session_state.current_series is not None:
        target_series = st.session_state.current_series
        
        # 画面最上部に大きく「一覧に戻る」を配置
        if st.button("← 蔵書一覧へ戻る", key="btn_drilldown_back", type="secondary"):
            st.session_state.current_series = None
            st.rerun()
            
        st.markdown(f"## 📁 シリーズ階層: **{target_series}**")
        
        # Supabaseより該当シリーズのみを巻数（volume）の昇順でクエリ抽出
        series_res = supabase.table("books").select("*").eq("user_id", USER_ID).eq("series", target_series).order("volume").execute()
        series_books = series_res.data if series_res.data else []
        
        if not series_books:
            st.info("このシリーズ内には現在書籍が登録されていません。")
        else:
            # 指定されたレスポンシブ列数（PC:6/スマホ:3）でグリッド展開
            for i in range(0, len(series_books), layout_columns_limit):
                grid_cols = st.columns(layout_columns_limit)
                for j in range(layout_columns_limit):
                    idx = i + j
                    if idx < len(series_books):
                        b_item = series_books[idx]
                        with grid_cols[j]:
                            thumb_url = b_item["cover"] if b_item["cover"] else "https://via.placeholder.com/150x210?text=No+Image"
                            st.image(thumb_url, use_container_width=True)
                            st.caption(f"**第 {b_item['volume']} 巻**")
                            # ダイアログ展開用トリガー
                            if st.button("詳細・編集", key=f"btn_s_detail_{b_item['id']}", use_container_width=True):
                                # 後述のダイアログマクロを起動
                                render_book_dialog_form(b_item)
                                
    # CASE B: 通常の全体一覧表示モード（フォルダ混在型）
    else:
        st.title("マイ本棚 蔵書一覧")
        
        # 検索窓（部分一致）
        search_term = st.text_input("🔎 キーワード検索 (タイトル、著者、出版社、ISBNコード)", value="")
        
        # スマホ画面を圧迫しないための折りたたみアコーディオン
        with st.expander("▽ 詳細な絞り込み・検索条件フィルター"):
            f_status = st.selectbox("読書ステータス", ["すべて", "未読", "読書中", "読了"])
            f_location = st.text_input("保管場所・本棚名でのフィルタリング", value="")
            
        # Supabaseからベースクエリ実行（実運用上は3万冊制約対応のため、集計用件を担保した上でフィルタを適用）
        db_query = supabase.table("books").select("*").eq("user_id", USER_ID)
        raw_data = db_query.execute().data if db_query.execute().data else []
        
        if not raw_data:
            st.info("蔵書データがありません。「バーコード高速登録」から本を追加してください。")
        else:
            # クレンジングおよびPandasデータフレーム化によるシリーズフォルダ集計
            df_books = pd.DataFrame(raw_data)
            
            # クライアントサイドでの動的フィルタリング適用
            if search_term:
                df_books = df_books[
                    df_books["title"].str.contains(search_term, case=False, na=False) |
                    df_books["author"].str.contains(search_term, case=False, na=False) |
                    df_books["publisher"].str.contains(search_term, case=False, na=False) |
                    df_books["isbn"].str.contains(search_term, case=False, na=False)
                ]
            if f_status != "all" and f_status != "すべて":
                df_books = df_books[df_books["status"] == f_status]
            if f_location:
                df_books = df_books[df_books["location"].str.contains(f_location, case=False, na=False)]
                
            # シリーズフォルダグループの生成
            arranged_items = []
            registered_series_set = set()
            
            # seriesフィールド値が登録されている行のグループ化準備
            grouped_df_series = df_books[df_books["series"] != ""].groupby("series")
            
            for _, b_row in df_books.iterrows():
                s_name = b_row["series"]
                if s_name:
                    # シリーズ物がまだ統合リストに追加されていない場合、代表を抽出してフォルダカード化
                    if s_name not in registered_series_set:
                        s_elements = grouped_df_series.get_group(s_name).sort_values(by="volume")
                        lead_book = s_elements.iloc[0]  # 要件：シリーズ物であれば一巻または上巻の表紙を代表表示
                        arranged_items.append({
                            "type": "folder",
                            "key_name": s_name,
                            "count": len(s_elements),
                            "cover": lead_book["cover"]
                        })
                        registered_series_set.add(s_name)
                else:
                    # 単巻本はそのまま独立カードとして展開
                    arranged_items.append({
                        "type": "single",
                        "key_name": b_row["title"],
                        "book_payload": b_row.to_dict(),
                        "cover": b_row["cover"]
                    })
                    
            if not arranged_items:
                st.warning("条件に合致する書籍データが見つかりませんでした。")
            else:
                # 3万冊想定に伴う「最大50件（フォルダ単位）」の厳密なページネーション管理
                items_per_page = 50
                total_items_count = len(arranged_items)
                max_page_num = ((total_items_count - 1) // items_per_page) + 1
                
                # 下部ではなくカードの上部でページ移動をコントロールできるUIの提供
                selected_page = st.number_input(f"ページ切り替え (1 - {max_page_num}ページ / 全{total_items_count}件)", min_value=1, max_value=max_page_num, value=1, step=1)
                
                slice_start = (selected_page - 1) * items_per_page
                slice_end = slice_start + items_per_page
                paged_items = arranged_items[slice_start:slice_end]
                
                # 指定レスポンシブ列数によるカード描画マトリクスループ
                for row_idx in range(0, len(paged_items), layout_columns_limit):
                    cols_list = st.columns(layout_columns_limit)
                    for col_idx in range(layout_columns_limit):
                        current_item_idx = row_idx + col_idx
                        if current_item_idx < len(paged_items):
                            render_target = paged_items[current_item_idx]
                            with cols_list[col_idx]:
                                card_img = render_target["cover"] if render_target["cover"] else "https://via.placeholder.com/150x210?text=No+Image"
                                st.image(card_img, use_container_width=True)
                                
                                # スマホ版表示における文字溢れ対策・タイトルトリミングの適用
                                display_label = render_target["key_name"]
                                if is_mobile_width and len(display_label) > 10:
                                    display_label = display_label[:10] + "..."
                                    
                                if render_target["type"] == "folder":
                                    st.markdown(f"📁 **{display_label}**")
                                    st.caption(f"(全 {render_target['count']} 冊)")
                                    if st.button("フォルダを開く", key=f"f_btn_{render_target['key_name']}", use_container_width=True):
                                        st.session_state.current_series = render_target["key_name"]
                                        st.rerun()
                                else:
                                    st.markdown(f"📘 **{display_label}**")
                                    st.caption(render_target["book_payload"].get("author", ""))
                                    if st.button("詳細確認", key=f"s_btn_{render_target['book_payload']['id']}", use_container_width=True):
                                        render_book_dialog_form(render_target["book_payload"])

# ------------------------------------------------------------------------------
# 3.2 【ダイアログ】書籍詳細・編集・削除ポップアップ（st.dialogマクロ）
# ------------------------------------------------------------------------------
@st.dialog("書籍詳細・データ編集")
def render_book_dialog_form(book_record: dict):
    """
    一覧のアイテムクリックによりモーダル浮上するポップアップ。
    画面仕様に準拠し、削除ボタンを右上に逃がし、誤操作を完全防御する。
    """
    # 2カラムのトップヘッダー構成による削除ボタンの右上隔離配置
    head_col1, head_col2 = st.columns([0.75, 0.25])
    with head_col1:
        st.write("#### 登録台帳データ")
    with head_col2:
        # 右上のデッドスペースに小さく赤文字風のプライマリー型を配置
        if st.button("🗑️ データを削除", key=f"dlg_del_{book_record['id']}", help="注意：この本を本棚から完全に抹消します"):
            st.error("【最終警告】本当に削除しますか？この操作は取り消せません。")
            if st.button("はい、本当に削除します", key=f"dlg_confirm_del_{book_record['id']}", type="primary"):
                supabase.table("books").delete().eq("id", book_record["id"]).execute()
                st.success("削除処理が正常完了しました。")
                st.session_state.current_series = None # 整合性維持のためドリルダウンを初期化
                st.rerun()

    st.write("---")
    
    # 画面幅に応じたフォームのレスポンシブ配置（スマホは縦1列並びへ自動最適化）
    if not is_mobile_width:
        body_col1, body_col2 = st.columns([0.4, 0.6])
    else:
        body_col1, body_col2 = st.container(), st.container()
        
    with body_col1:
        img_src = book_record["cover"] if book_record["cover"] else "https://via.placeholder.com/150x210?text=No+Image"
        st.image(img_src, use_container_width=True)
        st.caption(f"ISBNコード: {book_record.get('isbn', '未登録')}")
        
    with body_col2:
        # 入力項目フォームのレンダリング
        edit_title = st.text_input("書籍タイトル (必須)", value=book_record["title"])
        edit_subtitle = st.text_input("サブタイトル (任意)", value=book_record.get("subtitle", ""))
        edit_author = st.text_input("著者名 (必須)", value=book_record["author"])
        edit_publisher = st.text_input("出版社 (必須)", value=book_record["publisher"])
        edit_volume = st.number_input("巻数", min_value=1, value=int(book_record["volume"]))
        edit_series = st.text_input("シリーズ・フォルダ紐づけ名", value=book_record.get("series", ""))
        edit_status = st.selectbox("読書ステータス", ["未読", "読書中", "読了"], index=["未読", "読書中", "読了"].index(book_record["status"]))
        edit_location = st.text_input("保管棚・保管場所番号", value=book_record.get("location", ""))
        edit_memo = st.text_area("ユーザー個別メモ欄", value=book_record.get("memo", ""))
        
        # あらすじ紹介枠の仕様（PCは固定高インフォ枠、スマホはエクスパンダーで縦伸び防止）
        st.write("**openBD提供の作品紹介あらすじ:**")
        if is_mobile_width:
            with st.expander("あらすじ詳細を展開表示"):
                st.caption(book_record.get("description", ""))
        else:
            st.info(book_record.get("description", "データなし"))
            
        # ポジティブアクション（変更保存）ボタンは最下部中央寄りに配置
        if st.button("更新内容を本棚に保存する", type="primary", use_container_width=True):
            if not edit_title or not edit_author or not edit_publisher:
                st.error("必須項目（タイトル・著者・出版社）が空欄です。")
            else:
                supabase.table("books").update({
                    "title": edit_title,
                    "subtitle": edit_subtitle,
                    "author": edit_author,
                    "publisher": edit_publisher,
                    "volume": int(edit_volume),
                    "series": edit_series,
                    "status": edit_status,
                    "location": edit_location,
                    "memo": edit_memo
                }).eq("id", book_record["id"]).execute()
                st.success("書籍台帳データを正常に書き換えました。")
                st.rerun()

# ------------------------------------------------------------------------------
# 3.3 【画面2】書籍登録画面（WebRTC連動リアルタイムスキャン機能）
# ------------------------------------------------------------------------------
with app_tabs[1]:
    st.title("新しい本を登録する")
    
    register_mode = st.radio("入力アプローチの選択", ["📷 カメラからバーコードを連続読み取り", "⌨️ ISBN数値を手動でタイピング", "📝 白紙から完全手動フォーム入力"], horizontal=True)
    
    # CASE A: カメラを用いたリアルタイムバーコードスキャンモード
    if register_mode == "📷 カメラからバーコードを連続読み取り":
        if not is_mobile_width:
            cam_layout, history_layout = st.columns([0.5, 0.5])
        else:
            cam_layout, history_layout = st.container(), st.container()
            
        with cam_layout:
            st.markdown("#### カメラフレーム内中央の「赤枠ターゲット」に本のバーコード(上側)を合わせてください。")
            
            # streamlit-webrtc 映像フレーム割り込み解析内部クラス
            class OpenCVIsbnDecoder(VideoTransformerBase):
                def transform(self, frame):
                    img_array = frame.to_ndarray(format="bgr24")
                    frame_h, frame_w, _ = img_array.shape
                    
                    # 1. 照準枠（赤枠）の座標計算
                    box_x1, box_y1 = int(frame_w * 0.15), int(frame_h * 0.35)
                    box_x2, box_y2 = int(frame_w * 0.85), int(frame_h * 0.65)
                    
                    # 2. 【超重要】照準枠の中（バーコードがあるエリア）だけを切り抜いて画質対策を集中
                    roi = img_array[box_y1:box_y2, box_x1:box_x2]
                    
                    if roi.size > 0:
                        # 補正A: 映像を少し拡大してバーコードの線を太く見せる（解像度不足の補正）
                        roi_resized = cv2.resize(roi, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
                        
                        # 補正B: 白黒（グレースケール）に変換して色ノイズをカット
                        gray = cv2.cvtColor(roi_resized, cv2.COLOR_BGR2GRAY)
                        
                        # 補正C: 輪郭をハッキリさせる「大津の二値化」を適用（ぼやけた影をクッキリさせる）
                        _, threshed = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                        
                        # 3. 補正済みのクッキリした画像でバーコードを解析
                        detected_barcodes = decode(threshed)
                        
                        # もし補正画像でダメなら、念のためオリジナルの切り抜き領域でも解析を試みる（2段構え）
                        if not detected_barcodes:
                            detected_barcodes = decode(roi)
                            
                        for bc in detected_barcodes:
                            raw_code = bc.data.decode("utf-8")
                            if len(raw_code) == 13 and (raw_code.startswith("978") or raw_code.startswith("979")):
                                st.session_state.last_scanned_isbn = raw_code
                    
                    # 画面に表示する用の赤い案内枠を合成（ユーザー向け）
                    cv2.rectangle(img_array, (box_x1, box_y1), (box_x2, box_y2), (0, 0, 255), 3)
                    return img_array

            # WebRTCピア接続ストリーマーの起動（ブラウザのカメラパーミッション直接奪取対応）
            webrtc_streamer(
                key="webrtc_isbn_scanner",
                mode=WebRtcMode.SENDRECV,
                video_transformer_factory=OpenCVIsbnDecoder,
                # カメラデバイスへ高解像度かつオートフォーカスなどを要求する詳細設定
                media_stream_constraints={
                    "video": {
                        "width": {"ideal": 1280, "min": 640},
                        "height": {"ideal": 720, "min": 480},
                        "facingMode": "environment", # スマホの場合は背面カメラを優先
                        "focusMode": "continuous"     # 可能なら継続的なオートフォーカスを要求
                    },
                    "audio": False
                },
                async_processing=True
            )
            
            # 解析スレッドからセッションステートへISBNが渡された際のイベントハンドラ割り込み
            if st.session_state.last_scanned_isbn:
                scanned_isbn_code = st.session_state.last_scanned_isbn
                st.session_state.last_scanned_isbn = None # 連続多重検知の抑制クリア
                
                # openBD APIによるメタデータ自動補完
                fetched_meta = fetch_book_metadata_from_openbd(scanned_isbn_code)
                if fetched_meta:
                    db_status, created_rec = upsert_book_to_supabase(fetched_meta)
                    
                    if db_status == "success":
                        st.toast(f"🎉 登録成功: {fetched_meta['title']} ({fetched_meta['volume']}巻)", icon="✅")
                        trigger_device_vibration("success") # 画面仕様書：成功時は短く1回振動
                        st.session_state.scan_history.insert(0, fetched_meta) # 連続スキャンの履歴先頭に追加
                    elif db_status == "duplicate":
                        st.toast(f"⚠️ 既に登録されています: {fetched_meta['title']}", icon="ℹ️")
                        trigger_device_vibration("duplicate") # 画面仕様書：重複時は長めに1回振動
                else:
                    st.toast("❌ openBDに該当書籍がありません。完全手動登録を使用してください。", icon="🚨")
                    trigger_device_vibration("failed") # 画面仕様書：該当なし時は短く2回振動
                    
        with history_layout:
            st.markdown(f"#### 直近のスキャン登録履歴 (最大 {layout_columns_limit} 件を表示)")
            if st.session_state.scan_history:
                active_history = st.session_state.scan_history[:layout_columns_limit]
                hist_cols = st.columns(layout_columns_limit)
                for h_idx, h_book in enumerate(active_history):
                    with hist_cols[h_idx]:
                        h_img = h_book["cover"] if h_book["cover"] else "https://via.placeholder.com/150x210?text=No+Image"
                        st.image(h_img, use_container_width=True)
                        st.caption(f"**{h_book['title'][:8]}...**")
            else:
                st.write("カメラをかざすとここに直近の成功履歴が並びます（連続登録対応UI）。")

    # CASE B: ISBN数値の手動タイピング登録
    elif register_mode == "⌨️ ISBN数値を手動でタイピング":
        typed_isbn = st.text_input("13桁のISBNコード（978...）を入力してください")
        if st.button("このISBNで検索・登録を実行", type="primary"):
            if typed_isbn:
                with st.spinner("APIからデータを探索中..."):
                    manual_meta = fetch_book_metadata_from_openbd(typed_isbn)
                    if manual_meta:
                        db_status, _ = upsert_book_to_supabase(manual_meta)
                        if db_status == "success":
                            st.success(f"登録に成功しました: {manual_meta['title']}")
                        elif db_status == "duplicate":
                            st.warning("この本は既にマイ本棚に登録済みです。")
                    else:
                        st.error("外部の書誌データベースに該当コードが見つかりません。完全手動登録へ切り替えてください。")

    # CASE C: API失敗時・ISBNを持たない同人誌等の完全手動フォーム入力
    elif register_mode == "📝 白紙から完全手動フォーム入力":
        with st.form("form_manual_add"):
            st.write("仮想キーボード立ち上げ時に隠れないよう、間隔を広く設計したフォームです。")
            m_title = st.text_input("書籍名・タイトル（必須項目）")
            m_author = st.text_input("著者名（必須項目）")
            m_publisher = st.text_input("出版社・レーベル（必須項目）")
            m_volume = st.number_input("巻数（単巻本の場合は 1）", min_value=1, value=1)
            m_series = st.text_input("シリーズ名（同じ名前を入れると一覧画面でフォルダ化されます）")
            m_location = st.text_input("本棚の配置場所メモ（例：リビング本棚A-3）")
            
            if st.form_submit_button("手動データを本棚に追加", type="primary"):
                if not m_title or not m_author or not m_publisher:
                    st.error("必須項目（タイトル・著者・出版社）が入力されていません。")
                else:
                    manual_payload = {
                        "isbn": None,
                        "title": m_title,
                        "author": m_author,
                        "publisher": m_publisher,
                        "volume": int(m_volume),
                        "series": m_series,
                        "status": "未読",
                        "location": m_location,
                        "description": "手動入力による登録書籍データです。",
                        "cover": ""
                    }
                    db_status, _ = upsert_book_to_supabase(manual_payload)
                    if db_status == "success":
                        st.success("手動登録書籍をマイ本棚へ格納しました。")

# ------------------------------------------------------------------------------
# 3.4 【画面3】データ管理画面（CSVファイルの一括インポート・エクスポート）
# ------------------------------------------------------------------------------
with app_tabs[2]:
    st.title("システム一括移行・バックアップセンター")
    
    # 機能A: CSVファイルインポート（一括登録対応）
    st.markdown("### 📥 既存データの一括移行インポート")
    st.write("他社サービス（ブクログ等）からの移行用CSVデータを読み込みます。")
    
    # 画面仕様書に基づく、サンプルフォーマットダウンロード誘導
    sample_df = pd.DataFrame(columns=["title", "author", "publisher", "isbn", "series", "volume", "location", "status"])
    st.download_button(
        label="📄 インポート用CSVテンプレートのダウンロード",
        data=sample_df.to_csv(index=False, encoding="utf-8"),
        file_name="books_import_template.csv",
        mime="text/csv"
    )
    
    uploaded_csv_file = st.file_uploader("移行用CSVファイルを選択、またはドロップしてください", type=["csv"])
    if uploaded_csv_file is not None:
        try:
            df_parsed = pd.read_csv(uploaded_csv_file)
            
            # フォーマット検証（厳密な必須列エラーハンドリング）
            essential_fields = ["title", "author", "publisher"]
            omitted_fields = [f for f in essential_fields if f not in df_parsed.columns]
            
            if omitted_fields:
                st.error(f"❌ インポートが中断されました。CSV内に必須列 '{omitted_fields}' が存在しません。列名をご確認ください。")
            else:
                progress_indicator = st.progress(0)
                imported_rows_count = len(df_parsed)
                successful_inserts = 0
                
                for idx, row in df_parsed.iterrows():
                    # 各列値のPandas欠損値（NaN）安全保護クレンジング
                    clean_isbn_val = str(row.get("isbn", "")).split(".")[0] if pd.notna(row.get("isbn")) else None
                    clean_vol_val = int(row.get("volume")) if pd.notna(row.get("volume")) and str(row.get("volume")).isdigit() else 1
                    
                    csv_payload = {
                        "isbn": clean_isbn_val if clean_isbn_val and clean_isbn_val != "nan" else None,
                        "title": str(row["title"]),
                        "author": str(row["author"]),
                        "publisher": str(row["publisher"]),
                        "volume": clean_vol_val,
                        "series": str(row.get("series", "")) if pd.notna(row.get("series")) else "",
                        "location": str(row.get("location", "")) if pd.notna(row.get("location")) else "",
                        "status": str(row.get("status", "未読")) if str(row.get("status")) in ["未読", "読書中", "読了"] else "未読",
                        "description": "他社システムからのCSVインポートデータ"
                    }
                    
                    status, _ = upsert_book_to_supabase(csv_payload)
                    if status == "success":
                        successful_inserts += 1
                        
                    # 進捗プログレスバーの動的書き換え
                    progress_indicator.progress((idx + 1) / imported_rows_count)
                    
                st.success(f"🎉 一括インポート処理完了: 総行数 {imported_rows_count} 件のうち、{successful_inserts} 冊を新しく本棚へ取り込みました！")
        except Exception as csv_err:
            st.error(f"【CSVパース失敗】アップロードされたファイルの構造解析に失敗しました。構造に異常があります: {str(csv_err)}")

    st.write("---")
    
    # 機能B: CSVファイルエクスポート（一括出力バックアップ対応）
    st.markdown("### 📤 蔵書データの一括エクスポート")
    st.write("登録されているすべてのデータをCSVとして一括バックアップ出力します（マルチデバイス共有対応用）。")
    
    if st.button("クラウド上の全蔵書データを抽出し、エクスポートを準備"):
        with st.spinner("データベースからパッキング中..."):
            export_res = supabase.table("books").select("*").eq("user_id", USER_ID).execute()
            
            if export_res.data:
                df_export = pd.DataFrame(export_res.data)
                # ユーザー個人の機密システムカラム（UUID、ID等）の安全な除外処理
                drop_targets = ["user_id", "id", "created_at"]
                df_export = df_export.drop(columns=[col for col in drop_targets if col in df_export.columns])
                
                # Excel等の文字化けを防止するBOM付きUTF-8エンコード
                csv_bytes = df_export.to_csv(index=False, encoding="utf-8-sig")
                
                st.download_button(
                    label="💾 CSVファイルをローカルへ保存",
                    data=csv_bytes,
                    file_name="my_bookshelf_backup.csv",
                    mime="text/csv",
                    type="primary"
                )
                st.success("エクスポート用ダウンロードリンクが生成されました。上のボタンから保存してください。")
            else:
                st.warning("本棚に書籍が1冊も登録されていないため、エクスポートを実行できません。")