import os
import re
import cv2
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from PIL import Image
from pyzbar.pyzbar import decode
from streamlit_webrtc import webrtc_streamer, WebRtcMode, VideoProcessorBase
from supabase import create_client, Client
import queue

# ==============================================================================
# 0. 環境設定・安全な初期化
# ==============================================================================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("【環境変数エラー】.envファイル、またはデプロイ環境の設定に 'SUPABASE_URL' と 'SUPABASE_KEY' を指定してください。")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

st.set_page_config(
    page_title="蔵書管理システム 📚",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

if "current_series" not in st.session_state:
    st.session_state.current_series = None
if "last_scanned_isbn" not in st.session_state:
    st.session_state.last_scanned_isbn = None
if "scan_history" not in st.session_state:
    st.session_state.scan_history = []
if "user_session" not in st.session_state:
    st.session_state.user_session = {
        "id": "00000000-0000-0000-0000-000000000000", 
        "email": "demo-user@example.com"
    }

USER_ID = st.session_state.user_session["id"]

# ==============================================================================
# 1. フロントエンド連携：JavaScript注入ユーティリティ
# ==============================================================================
def trigger_device_vibration(pattern_type: str):
    patterns = {
        "success": "100",
        "duplicate": "300",
        "failed": "[100, 50, 100]"
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
# 2. データアクセス・外部API連携層
# ==============================================================================
def fetch_book_metadata_from_openbd(isbn: str) -> dict:
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
                    "description": description,
                    "status": "未読",
                    "location": "",
                    "memo": ""
                }
    except Exception as e:
        print(f"API通信エラー: {str(e)}")
    return None

def upsert_book_to_supabase(book_payload: dict) -> tuple:
    book_payload["user_id"] = USER_ID
    # 全てのフィールドを確実に含める
    defaults = {
        "isbn": "", "title": "", "subtitle": "", "author": "", 
        "publisher": "", "volume": 1, "series": "", "pubdate": "", 
        "cover": "", "description": "", "status": "未読", 
        "location": "", "memo": ""
    }
    final_payload = {**defaults, **book_payload}
    
    try:
        res = supabase.table("books").insert(final_payload).execute()
        if res.data:
            return "success", res.data[0]
    except Exception as e:
        err_msg = str(e)
        if "unique_user_isbn" in err_msg or "duplicate key" in err_msg:
            return "duplicate", None
        return "error", err_msg
    return "error", "未知のデータベース割り込みレスポンス"

# ==============================================================================
# 3. 画面プレゼンテーション層
# ==============================================================================

is_mobile_width = st.sidebar.checkbox("📱 スマホ表示モード (最大3列) でシミュレート", value=False)
layout_columns_limit = 3 if is_mobile_width else 6

app_tabs = st.tabs(["📚 蔵書一覧・検索", "📷 バーコード高速登録", "⚙️ システムデータ管理"])

# ------------------------------------------------------------------------------
# 3.1 【画面1】書籍一覧画面
# ------------------------------------------------------------------------------
with app_tabs[0]:
    if st.session_state.current_series is not None:
        target_series = st.session_state.current_series
        if st.button("← 蔵書一覧へ戻る", key="btn_drilldown_back", type="secondary"):
            st.session_state.current_series = None
            st.rerun()
            
        st.markdown(f"## 📁 シリーズ階層: **{target_series}**")
        series_res = supabase.table("books").select("*").eq("user_id", USER_ID).eq("series", target_series).order("volume").execute()
        series_books = series_res.data if series_res.data else []
        
        if not series_books:
            st.info("このシリーズ内には現在書籍が登録されていません。")
        else:
            for i in range(0, len(series_books), layout_columns_limit):
                grid_cols = st.columns(layout_columns_limit)
                for j in range(layout_columns_limit):
                    idx = i + j
                    if idx < len(series_books):
                        b_item = series_books[idx]
                        with grid_cols[j]:
                            thumb_url = b_item.get("cover") if b_item.get("cover") else "https://via.placeholder.com/150x210?text=No+Image"
                            st.image(thumb_url, use_container_width=True)
                            st.caption(f"**第 {b_item.get('volume', 1)} 巻**")
                            if st.button("詳細・編集", key=f"btn_s_detail_{b_item['id']}", use_container_width=True):
                                render_book_dialog_form(b_item)
                                
    else:
        st.title("マイ本棚 蔵書一覧")
        search_term = st.text_input("🔎 キーワード検索 (タイトル、著者、出版社、ISBNコード)", value="")
        with st.expander("▽ 詳細な絞り込み・検索条件フィルター"):
            f_status = st.selectbox("読書ステータス", ["すべて", "未読", "読書中", "読了"])
            f_location = st.text_input("保管場所・本棚名でのフィルタリング", value="")
            
        db_query = supabase.table("books").select("*").eq("user_id", USER_ID)
        raw_data = db_query.execute().data if db_query.execute().data else []
        
        if not raw_data:
            st.info("蔵書データがありません。「バーコード高速登録」から本を追加してください。")
        else:
            df_books = pd.DataFrame(raw_data)
            
            # 全ての検索対象カラムを文字列型に変換して検索漏れを防ぐ
            search_columns = ["title", "author", "publisher", "isbn", "subtitle", "series", "location"]
            for col in search_columns:
                if col in df_books.columns:
                    df_books[col] = df_books[col].fillna("").astype(str)
            
            # 検索フィルタリング
            if search_term:
                search_pattern = f".*{re.escape(search_term)}.*"
                mask = df_books[search_columns].apply(lambda x: x.str.contains(search_pattern, case=False, na=False)).any(axis=1)
                df_books = df_books[mask]
                
            if f_status != "すべて":
                df_books = df_books[df_books["status"] == f_status]
            if f_location:
                df_books = df_books[df_books["location"].str.contains(re.escape(f_location), case=False, na=False)]
                
            arranged_items = []
            registered_series_set = set()
            
            # シリーズ物のグループ化
            df_books["series"] = df_books["series"].fillna("")
            grouped_df_series = df_books[df_books["series"] != ""].groupby("series")
            
            # 検索結果を表示リストに変換
            for _, b_row in df_books.iterrows():
                s_name = b_row["series"]
                if s_name:
                    if s_name not in registered_series_set:
                        s_elements = grouped_df_series.get_group(s_name).sort_values(by="volume")
                        lead_book = s_elements.iloc[0]
                        arranged_items.append({
                            "type": "folder",
                            "key_name": s_name,
                            "count": len(s_elements),
                            "cover": lead_book.get("cover", "")
                        })
                        registered_series_set.add(s_name)
                else:
                    arranged_items.append({
                        "type": "single",
                        "key_name": b_row["title"],
                        "book_payload": b_row.to_dict(),
                        "cover": b_row.get("cover", "")
                    })
                    
            if not arranged_items:
                st.warning("条件に合致する書籍データが見つかりませんでした。")
            else:
                items_per_page = 50
                total_items_count = len(arranged_items)
                max_page_num = ((total_items_count - 1) // items_per_page) + 1
                selected_page = st.number_input(f"ページ切り替え (1 - {max_page_num}ページ / 全{total_items_count}件)", min_value=1, max_value=max_page_num, value=1, step=1)
                
                slice_start = (selected_page - 1) * items_per_page
                slice_end = slice_start + items_per_page
                paged_items = arranged_items[slice_start:slice_end]
                
                for row_idx in range(0, len(paged_items), layout_columns_limit):
                    cols_list = st.columns(layout_columns_limit)
                    for col_idx in range(layout_columns_limit):
                        current_item_idx = row_idx + col_idx
                        if current_item_idx < len(paged_items):
                            render_target = paged_items[current_item_idx]
                            with cols_list[col_idx]:
                                card_img = render_target["cover"] if render_target["cover"] else "https://via.placeholder.com/150x210?text=No+Image"
                                st.image(card_img, use_container_width=True)
                                display_label = render_target["key_name"]
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
# 3.2 【ダイアログ】書籍詳細・編集・削除ポップアップ
# ------------------------------------------------------------------------------
@st.dialog("書籍詳細・データ編集")
def render_book_dialog_form(book_record: dict):
    head_col1, head_col2 = st.columns([0.75, 0.25])
    with head_col1:
        st.write("#### 登録台帳データ")
    with head_col2:
        if st.button("🗑️ データを削除", key=f"dlg_del_{book_record['id']}", help="注意：この本を本棚から完全に抹消します"):
            st.error("【最終警告】本当に削除しますか？この操作は取り消せません。")
            if st.button("はい、本当に削除します", key=f"dlg_confirm_del_{book_record['id']}", type="primary"):
                supabase.table("books").delete().eq("id", book_record["id"]).execute()
                st.success("削除処理が正常完了しました。")
                st.session_state.current_series = None
                st.rerun()

    st.write("---")
    if not is_mobile_width:
        body_col1, body_col2 = st.columns([0.4, 0.6])
    else:
        body_col1, body_col2 = st.container(), st.container()
        
    with body_col1:
        img_src = book_record.get("cover") if book_record.get("cover") else "https://via.placeholder.com/150x210?text=No+Image"
        st.image(img_src, use_container_width=True)
        st.caption(f"ISBNコード: {book_record.get('isbn', '未登録')}")
        
    with body_col2:
        edit_title = st.text_input("書籍タイトル (必須)", value=book_record.get("title", ""))
        edit_subtitle = st.text_input("サブタイトル (任意)", value=book_record.get("subtitle", ""))
        edit_author = st.text_input("著者名 (必須)", value=book_record.get("author", ""))
        edit_publisher = st.text_input("出版社 (必須)", value=book_record.get("publisher", ""))
        edit_volume = st.number_input("巻数", min_value=1, value=int(book_record.get("volume", 1)))
        edit_series = st.text_input("シリーズ・フォルダ紐づけ名", value=book_record.get("series", ""))
        
        status_list = ["未読", "読書中", "読了"]
        current_status = book_record.get("status", "未読")
        status_index = status_list.index(current_status) if current_status in status_list else 0
        edit_status = st.selectbox("読書ステータス", status_list, index=status_index)
        
        edit_location = st.text_input("保管棚・保管場所番号", value=book_record.get("location", ""))
        edit_memo = st.text_area("ユーザー個別メモ欄", value=book_record.get("memo", ""))
        
        st.write("**openBD提供の作品紹介あらすじ:**")
        if is_mobile_width:
            with st.expander("あらすじ詳細を展開表示"):
                st.caption(book_record.get("description", ""))
        else:
            st.info(book_record.get("description", "データなし"))
            
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
# 3.3 【画面2】書籍登録画面
# ------------------------------------------------------------------------------
with app_tabs[1]:
    st.title("新しい本を登録する")
    register_mode = st.radio("入力アプローチの選択", ["📷 カメラからバーコードを連続読み取り", "⌨️ ISBN数値を手動でタイピング", "📝 白紙から完全手動フォーム入力"], horizontal=True)
    
    if register_mode == "📷 カメラからバーコードを連続読み取り":
        if not is_mobile_width:
            cam_layout, history_layout = st.columns([0.5, 0.5])
        else:
            cam_layout, history_layout = st.container(), st.container()
            
        with cam_layout:
            st.markdown("#### カメラフレーム内中央の「赤枠ターゲット」に本のバーコード(上側)を合わせてください。")
            scan_message_spot = st.empty() 
            
            class OpenCVIsbnProcessor(VideoProcessorBase):
                def __init__(self):
                    self.result_queue = queue.Queue()
                    self.last_detected = None

                def recv(self, frame):
                    img = frame.to_ndarray(format="bgr24")
                    frame_h, frame_w, _ = img.shape
                    box_x1, box_y1 = int(frame_w * 0.15), int(frame_h * 0.35)
                    box_x2, box_y2 = int(frame_w * 0.85), int(frame_h * 0.65)
                    roi = img[box_y1:box_y2, box_x1:box_x2]
                    
                    if roi.size > 0:
                        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                        _, threshed = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                        detected_barcodes = decode(threshed)
                        if not detected_barcodes:
                            detected_barcodes = decode(roi)
                            
                        for bc in detected_barcodes:
                            raw_code = bc.data.decode("utf-8")
                            if len(raw_code) == 13 and (raw_code.startswith("978") or raw_code.startswith("979")):
                                if raw_code != self.last_detected:
                                    self.last_detected = raw_code
                                    self.result_queue.put(raw_code)
                    
                    cv2.rectangle(img, (box_x1, box_y1), (box_x2, box_y2), (0, 0, 255), 3)
                    return frame.from_ndarray(img, format="bgr24")

            webrtc_ctx = webrtc_streamer(
                key="webrtc_isbn_scanner",
                mode=WebRtcMode.SENDRECV,
                video_processor_factory=OpenCVIsbnProcessor,
                media_stream_constraints={
                    "video": {
                        "width": {"ideal": 1280, "min": 640},
                        "height": {"ideal": 720, "min": 480},
                        "facingMode": "environment",
                    },
                    "audio": False
                },
                async_processing=True
            )
            
        with history_layout:
            history_display_spot = st.container()
            if webrtc_ctx.video_processor:
                try:
                    scanned_isbn_code = webrtc_ctx.video_processor.result_queue.get_nowait()
                except queue.Empty:
                    scanned_isbn_code = None

                if scanned_isbn_code:
                    fetched_meta = fetch_book_metadata_from_openbd(scanned_isbn_code)
                    if fetched_meta:
                        db_status, created_rec = upsert_book_to_supabase(fetched_meta)
                        if db_status == "success":
                            scan_message_spot.success(f"✅ 登録成功: {fetched_meta['title']}")
                            trigger_device_vibration("success")
                            st.session_state.scan_history.insert(0, fetched_meta)
                        elif db_status == "duplicate":
                            scan_message_spot.warning(f"ℹ️ 既に登録されています: {fetched_meta['title']}")
                            trigger_device_vibration("duplicate")
                    else:
                        scan_message_spot.error(f"🚨 openBDに該当書籍がありません。 (ISBN: {scanned_isbn_code})")
                        trigger_device_vibration("failed")
                    st.rerun()

            with history_display_spot:
                st.markdown(f"#### 直近のスキャン登録履歴 (最大 {layout_columns_limit} 件を表示)")
                if st.session_state.scan_history:
                    active_history = st.session_state.scan_history[:layout_columns_limit]
                    hist_cols = st.columns(layout_columns_limit)
                    for h_idx, h_book in enumerate(active_history):
                        with hist_cols[h_idx]:
                            h_img = h_book.get("cover") if h_book.get("cover") else "https://via.placeholder.com/150x210?text=No+Image"
                            st.image(h_img, use_container_width=True)
                            st.caption(f"**{h_book.get('title', '')[:8]}...**")
                else:
                    st.write("履歴がここに並びます。")

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

    elif register_mode == "📝 白紙から完全手動フォーム入力":
        with st.form("form_manual_add"):
            st.write("手動で書籍情報を入力してください。")
            m_title = st.text_input("書籍名・タイトル（必須項目）")
            m_author = st.text_input("著者名（必須項目）")
            m_publisher = st.text_input("出版社・レーベル（必須項目）")
            m_volume = st.number_input("巻数", min_value=1, value=1)
            m_series = st.text_input("シリーズ名")
            m_location = st.text_input("本棚の配置場所メモ")
            if st.form_submit_button("手動データを本棚に登録"):
                if m_title and m_author and m_publisher:
                    manual_payload = {
                        "isbn": "MANUAL-" + os.urandom(4).hex(),
                        "title": m_title,
                        "author": m_author,
                        "publisher": m_publisher,
                        "volume": int(m_volume),
                        "series": m_series,
                        "location": m_location,
                        "status": "未読",
                        "cover": "",
                        "description": "手動登録された書籍です。"
                    }
                    db_status, _ = upsert_book_to_supabase(manual_payload)
                    if db_status == "success":
                        st.success("手動登録が完了しました。")
                    else:
                        st.error(f"登録エラー: {db_status}")
                else:
                    st.error("必須項目を入力してください。")

# ------------------------------------------------------------------------------
# 3.4 【画面3】システムデータ管理
# ------------------------------------------------------------------------------
with app_tabs[2]:
    st.title("システム設定・データ管理")
    st.write(f"ログインユーザー: {st.session_state.user_session['email']}")
    
    if st.button("全蔵書データをCSVでエクスポート"):
        all_res = supabase.table("books").select("*").eq("user_id", USER_ID).execute()
        if all_res.data:
            df_export = pd.DataFrame(all_res.data)
            csv = df_export.to_csv(index=False).encode('utf-8')
            st.download_button("CSVファイルをダウンロード", csv, "my_library_data.csv", "text/csv")
        else:
            st.warning("エクスポートするデータがありません。")
