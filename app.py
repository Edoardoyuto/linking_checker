import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Dict, List, Optional

import streamlit as st

if sys.platform == "win32":
    import winreg
else:
    winreg = None


STATUS_UNCONFIRMED = "unconfirmed"
STATUS_CONFIRMED = "confirmed"
STATUS_MODIFIED = "modified"
STATUS_NEEDS_REVIEW = "needs_review"
WORKFLOW_KEYS = {"status", "admin_check_required", "admin_check_reason"}


st.set_page_config(
    page_title="JSON/PDF Checker",
    page_icon="LC",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.markdown(
    """
    <style>
    div.stButton > button {
        min-height: 2.25rem;
        white-space: nowrap;
    }
    div.stButton > button[kind="primary"] {
        background: #dc2626;
        border-color: #dc2626;
        color: #ffffff;
    }
    div.stButton > button[kind="primary"]:hover {
        background: #b91c1c;
        border-color: #b91c1c;
        color: #ffffff;
    }
    div[data-testid="stVerticalBlock"] {
        gap: 0.45rem;
    }
    div[data-testid="stExpander"] details {
        padding-bottom: 0.35rem;
    }
    div[data-testid="stExpander"] div[data-testid="stVerticalBlock"] {
        gap: 0.35rem;
    }
    div[data-testid="stMetric"] {
        padding: 0;
    }
    hr {
        margin: 0.45rem 0;
    }
    .block-container {
        padding-top: 1.5rem;
    }
    section[data-testid="stSidebar"] {
        max-height: 100vh;
        overflow-y: auto;
    }
    div[data-testid="column"]:has(.json-pane-marker) {
        height: calc(100vh - 7.5rem);
        min-height: 0;
    }
    div[data-testid="column"]:has(.json-pane-marker) > div[data-testid="stVerticalBlock"] {
        max-height: calc(100vh - 7.5rem);
        overflow-y: auto;
        padding-right: 0.5rem;
    }
    div[data-testid="column"]:has(.json-pane-marker) > div[data-testid="stVerticalBlock"]::-webkit-scrollbar {
        width: 10px;
    }
    div[data-testid="column"]:has(.json-pane-marker) > div[data-testid="stVerticalBlock"]::-webkit-scrollbar-thumb {
        background: #cbd5e1;
        border-radius: 999px;
        border: 2px solid #ffffff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def fetch_pdf_bytes(pdf_url: str) -> bytes:
    request = Request(
        pdf_url,
        headers={
            "User-Agent": "Mozilla/5.0 JSON-PDF-Checker/1.0",
            "Accept": "application/pdf,*/*",
        },
    )
    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read()

    if not data:
        raise ValueError("PDF response is empty.")
    if "pdf" not in content_type.lower() and not data.startswith(b"%PDF"):
        raise ValueError(f"PDFではない応答を受け取りました: {content_type or 'unknown'}")
    return data


def load_json_records(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        records: List[Dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL line {line_no}: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"JSONL line {line_no}: object expected")
            records.append(item)
        return records

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    raise ValueError("JSON must be an object, an array of objects, or JSONL objects.")


def dump_jsonl(records: List[Dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps(prepare_record_for_output(record), ensure_ascii=False, separators=(",", ":"))
        for record in records
    )


def reviewed_output_filename(original_filename: str) -> str:
    filename = original_filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename:
        filename = "review_result.jsonl"
    return f"reviewd_{filename}"


def prepare_record_for_output(record: dict[str, Any]) -> dict[str, Any]:
    output_record = strip_position_fields(record)
    if isinstance(output_record, dict):
        output_record.setdefault("status", STATUS_UNCONFIRMED)
        output_record.setdefault("admin_check_required", False)
        output_record.setdefault("admin_check_reason", "")
    return output_record


def strip_position_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_position_fields(item)
            for key, item in value.items()
            if key not in {"line_no", "column_no"}
        }
    if isinstance(value, list):
        return [strip_position_fields(item) for item in value]
    return value


def strip_workflow_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_workflow_fields(item)
            for key, item in value.items()
            if key not in WORKFLOW_KEYS
        }
    if isinstance(value, list):
        return [strip_workflow_fields(item) for item in value]
    return value


def ensure_review_metadata(record: dict[str, Any]) -> None:
    record.setdefault("status", STATUS_UNCONFIRMED)
    record.setdefault("admin_check_required", False)
    record.setdefault("admin_check_reason", "")


def record_content_changed(record: dict[str, Any], original_record: dict[str, Any]) -> bool:
    current = strip_workflow_fields(strip_position_fields(record))
    original = strip_workflow_fields(strip_position_fields(original_record))
    return current != original


def update_record_status(record: dict[str, Any], original_record: dict[str, Any]) -> None:
    ensure_review_metadata(record)
    if bool(record.get("admin_check_required")):
        record["status"] = STATUS_NEEDS_REVIEW
    elif record_content_changed(record, original_record):
        record["status"] = STATUS_MODIFIED
    else:
        record["status"] = STATUS_CONFIRMED


def get_arxiv_pdf_url(record: dict[str, Any]) -> str:
    arxiv_id = str(record.get("arxiv_id", "")).strip()
    return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else ""


def request_pdf_open() -> None:
    st.session_state.pdf_open_request_id = st.session_state.get("pdf_open_request_id", 0) + 1


def set_selected_record(index: int, record_count: int) -> None:
    if record_count <= 0:
        return
    if index is None:
        return
    selected_index = min(max(int(index), 0), record_count - 1)
    st.session_state.selected_record_index = selected_index
    request_pdf_open()


def close_current_pdf_window() -> None:
    process = st.session_state.get("pdf_window_process")
    pid = st.session_state.get("pdf_window_pid")
    if not process and not pid:
        return

    try:
        if pid and sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif process and process.poll() is None:
            process.terminate()
    except Exception:
        pass
    finally:
        st.session_state.pdf_window_process = None
        st.session_state.pdf_window_pid = None


def find_chrome_path() -> str | None:
    if winreg is not None:
        registry_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ]
        for root_key, sub_key in registry_paths:
            try:
                with winreg.OpenKey(root_key, sub_key) as key:
                    chrome_path = winreg.QueryValue(key, None)
                    if chrome_path and os.path.exists(chrome_path):
                        return chrome_path
            except OSError:
                pass

    chrome_candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]
    return next((path for path in chrome_candidates if path and os.path.exists(path)), None)


def can_launch_local_browser_window() -> bool:
    return sys.platform == "win32"


def open_pdf_in_chrome_once(pdf_url: str) -> None:
    request_id = st.session_state.get("pdf_open_request_id", 0)
    if not pdf_url or request_id == st.session_state.get("pdf_opened_request_id"):
        return

    close_current_pdf_window()

    chrome_path = find_chrome_path()
    if chrome_path:
        profile_dir = os.path.join(tempfile.gettempdir(), "linking-checker-pdf-chrome-profile")
        os.makedirs(profile_dir, exist_ok=True)
        try:
            process = subprocess.Popen(
                [
                    chrome_path,
                    "--new-window",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--disable-extensions",
                    pdf_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            st.session_state.pdf_window_process = process
            st.session_state.pdf_window_pid = process.pid
        except OSError as exc:
            st.sidebar.warning(f"Chromeの起動に失敗しました: {exc}")
            webbrowser.open_new_tab(pdf_url)
    else:
        st.sidebar.warning("Chromeが見つからないため、既定ブラウザで開きます。前のPDFは自動で閉じられません。")
        webbrowser.open_new_tab(pdf_url)

    st.session_state.pdf_opened_request_id = request_id


def render_pdf_bytes(pdf_bytes: bytes, height: int) -> None:
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    component_height = max(height, 520)
    pdf_data = json.dumps(encoded)
    st.components.v1.html(
        f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8" />
          <style>
            html, body {{
              margin: 0;
              padding: 0;
              height: 100%;
              background: #f3f4f6;
              color: #111827;
              font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }}
            .toolbar {{
              position: sticky;
              top: 0;
              z-index: 2;
              display: flex;
              gap: 8px;
              align-items: center;
              padding: 8px;
              background: #ffffff;
              border-bottom: 1px solid #d1d5db;
            }}
            button {{
              min-width: 40px;
              min-height: 34px;
              border: 1px solid #d1d5db;
              border-radius: 6px;
              background: #ffffff;
              color: #111827;
              cursor: pointer;
            }}
            button:disabled {{
              color: #9ca3af;
              cursor: default;
            }}
            #status {{
              flex: 1;
              min-width: 120px;
              font-size: 14px;
              text-align: center;
            }}
            #viewer {{
              height: {component_height - 52}px;
              overflow: auto;
              padding: 14px;
              box-sizing: border-box;
            }}
            canvas {{
              display: block;
              max-width: 100%;
              margin: 0 auto;
              background: #ffffff;
              box-shadow: 0 1px 4px rgba(17, 24, 39, 0.18);
            }}
            #message {{
              padding: 24px;
              text-align: center;
              color: #4b5563;
            }}
          </style>
        </head>
        <body>
          <div class="toolbar">
            <button id="prev" title="前のページ">‹</button>
            <button id="next" title="次のページ">›</button>
            <span id="status">Loading PDF...</span>
            <button id="zoomOut" title="縮小">−</button>
            <button id="zoomIn" title="拡大">＋</button>
          </div>
          <div id="viewer">
            <canvas id="canvas"></canvas>
            <div id="message"></div>
          </div>
          <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.mjs" type="module"></script>
          <script type="module">
            import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.mjs";
            pdfjsLib.GlobalWorkerOptions.workerSrc =
              "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.worker.min.mjs";

            const base64 = {pdf_data};
            const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
            const canvas = document.getElementById("canvas");
            const context = canvas.getContext("2d");
            const status = document.getElementById("status");
            const message = document.getElementById("message");
            const prev = document.getElementById("prev");
            const next = document.getElementById("next");
            const zoomIn = document.getElementById("zoomIn");
            const zoomOut = document.getElementById("zoomOut");

            let pdf = null;
            let pageNumber = 1;
            let scale = 1.25;
            let rendering = false;
            let pendingPage = null;

            function updateControls() {{
              prev.disabled = !pdf || pageNumber <= 1;
              next.disabled = !pdf || pageNumber >= pdf.numPages;
              status.textContent = pdf
                ? `Page ${{pageNumber}} / ${{pdf.numPages}}  ${{Math.round(scale * 100)}}%`
                : "Loading PDF...";
            }}

            async function renderPage(number) {{
              rendering = true;
              updateControls();
              const page = await pdf.getPage(number);
              const viewport = page.getViewport({{ scale }});
              canvas.width = viewport.width;
              canvas.height = viewport.height;
              await page.render({{ canvasContext: context, viewport }}).promise;
              rendering = false;
              updateControls();
              if (pendingPage !== null) {{
                const nextPage = pendingPage;
                pendingPage = null;
                await renderPage(nextPage);
              }}
            }}

            function queueRender(number) {{
              if (rendering) {{
                pendingPage = number;
              }} else {{
                renderPage(number);
              }}
            }}

            prev.addEventListener("click", () => {{
              if (pageNumber <= 1) return;
              pageNumber -= 1;
              queueRender(pageNumber);
            }});

            next.addEventListener("click", () => {{
              if (!pdf || pageNumber >= pdf.numPages) return;
              pageNumber += 1;
              queueRender(pageNumber);
            }});

            zoomOut.addEventListener("click", () => {{
              scale = Math.max(0.5, scale - 0.15);
              queueRender(pageNumber);
            }});

            zoomIn.addEventListener("click", () => {{
              scale = Math.min(2.8, scale + 0.15);
              queueRender(pageNumber);
            }});

            try {{
              pdf = await pdfjsLib.getDocument({{ data: bytes }}).promise;
              message.textContent = "";
              await renderPage(pageNumber);
            }} catch (error) {{
              canvas.style.display = "none";
              status.textContent = "PDF表示エラー";
              message.textContent = error?.message || String(error);
            }}
          </script>
        </body>
        </html>
        """,
        height=component_height,
        scrolling=False,
    )


def render_pdf_viewer(pdf_bytes: Optional[bytes], pdf_url: str, height: int) -> None:
    if pdf_bytes:
        render_pdf_bytes(pdf_bytes, height)
        return

    if pdf_url:
        st.link_button("PDFを新しいタブで開く", pdf_url, use_container_width=True)
        try:
            with st.spinner("PDFを取得しています..."):
                render_pdf_bytes(fetch_pdf_bytes(pdf_url), height)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            st.error(f"PDFを埋め込み表示できませんでした: {exc}")
        return

    st.info("PDFをアップロードするか、JSONに arxiv_id を入れてください。")


def ensure_author_shape(author: Dict[str, Any]) -> Dict[str, Any]:
    author.setdefault("name", "")
    author.setdefault("email", "")
    author.setdefault("orcid", "")
    other = author.get("other")
    if other is None or other == "":
        author["other"] = {}
    elif not isinstance(other, dict):
        author["other"] = {"note": str(other)}
    affiliations = author.get("affiliations")
    if not isinstance(affiliations, list):
        author["affiliations"] = []
    return author


def ensure_affiliation_shape(affiliation: Dict[str, Any]) -> Dict[str, Any]:
    affiliation.setdefault("institution", "")
    affiliation.setdefault("address", "")
    return affiliation


def other_editor(author: Dict[str, Any], record_index: int, author_index: int) -> None:
    other = author.setdefault("other", {})
    if not isinstance(other, dict):
        other = {"note": str(other)}
        author["other"] = other

    st.caption("その他")
    if st.button(
        "その他項目を追加",
        key=f"add_other_{record_index}_{author_index}",
        use_container_width=True,
    ):
        base_key = "key"
        next_index = 1
        new_key = base_key
        while new_key in other:
            next_index += 1
            new_key = f"{base_key}_{next_index}"
        other[new_key] = ""
        st.rerun()

    for other_index, (other_key, other_value) in enumerate(list(other.items())):
        cols = st.columns([1, 2, 0.7])
        with cols[0]:
            new_key = st.text_input(
                "キー",
                value=str(other_key),
                key=f"other_key_{record_index}_{author_index}_{other_index}",
            ).strip()
        with cols[1]:
            new_value = st.text_area(
                "値",
                value=str(other_value),
                key=f"other_value_{record_index}_{author_index}_{other_index}",
                height=68,
            )
        with cols[2]:
            delete_other = st.button(
                "削除",
                key=f"del_other_{record_index}_{author_index}_{other_index}",
                use_container_width=True,
            )

        if delete_other:
            other.pop(other_key, None)
            st.rerun()

        if not new_key:
            st.warning("その他項目のキーは空にできません。")
            continue
        if new_key != other_key and new_key in other:
            st.warning(f"その他項目のキー `{new_key}` は既に使われています。")
            continue
        if new_key != other_key:
            other.pop(other_key, None)
        other[new_key] = new_value


def author_editor(record: Dict[str, Any], record_index: int) -> None:
    authors = record.setdefault("authors", [])
    if not isinstance(authors, list):
        st.warning("authors が配列ではないため、空の配列に置き換えました。")
        record["authors"] = []
        authors = record["authors"]

    left, right = st.columns([1, 1])
    with left:
        if st.button("著者を追加", use_container_width=True):
            authors.append(
                {"name": "", "email": "", "orcid": "", "other": {}, "affiliations": []}
            )
            st.rerun()
    with right:
        st.metric("著者数", len(authors))

    for author_index, author in enumerate(authors):
        if not isinstance(author, dict):
            authors[author_index] = {
                "name": str(author),
                "email": "",
                "orcid": "",
                "other": {},
                "affiliations": [],
            }
            author = authors[author_index]

        ensure_author_shape(author)
        title = author.get("name") or f"Author {author_index + 1}"
        with st.expander(title, expanded=True):
            author["name"] = st.text_input(
                "Name",
                value=str(author.get("name", "")),
                key=f"name_{record_index}_{author_index}",
            )
            author_cols = st.columns(2)
            with author_cols[0]:
                author["email"] = st.text_input(
                    "Email",
                    value=str(author.get("email", "")),
                    key=f"email_{record_index}_{author_index}",
                )
            with author_cols[1]:
                author["orcid"] = st.text_input(
                    "ORCID",
                    value=str(author.get("orcid", "")),
                    key=f"orcid_{record_index}_{author_index}",
                )
            other_editor(author, record_index, author_index)

            button_cols = st.columns(2)
            with button_cols[0]:
                if st.button(
                    "所属を追加",
                    key=f"add_aff_{record_index}_{author_index}",
                    use_container_width=True,
                ):
                    author["affiliations"].append({"institution": "", "address": ""})
                    st.rerun()
            with button_cols[1]:
                if st.button(
                    "著者を削除",
                    key=f"del_author_{record_index}_{author_index}",
                    use_container_width=True,
                ):
                    authors.pop(author_index)
                    st.rerun()

            for affiliation_index, affiliation in enumerate(author["affiliations"]):
                if not isinstance(affiliation, dict):
                    author["affiliations"][affiliation_index] = {
                        "institution": str(affiliation),
                        "address": "",
                    }
                    affiliation = author["affiliations"][affiliation_index]
                ensure_affiliation_shape(affiliation)

                st.divider()
                st.caption(f"Affiliation {affiliation_index + 1}")
                affiliation_cols = st.columns(2)
                with affiliation_cols[0]:
                    affiliation["institution"] = st.text_area(
                        "Institution",
                        value=str(affiliation.get("institution", "")),
                        key=f"inst_{record_index}_{author_index}_{affiliation_index}",
                        height=68,
                    )
                with affiliation_cols[1]:
                    affiliation["address"] = st.text_area(
                        "Address",
                        value=str(affiliation.get("address", "")),
                        key=f"addr_{record_index}_{author_index}_{affiliation_index}",
                        height=68,
                    )
                if st.button(
                    "所属を削除",
                    key=f"del_aff_{record_index}_{author_index}_{affiliation_index}",
                ):
                    author["affiliations"].pop(affiliation_index)
                    st.rerun()


def record_summary(record: Dict[str, Any]) -> str:
    arxiv_id = record.get("arxiv_id", "")
    authors = record.get("authors", [])
    names = []
    if isinstance(authors, list):
        names = [
            str(author.get("name", ""))
            for author in authors
            if isinstance(author, dict) and author.get("name")
        ]
    prefix = str(arxiv_id) if arxiv_id else "no arxiv_id"
    return f"{prefix} | {', '.join(names[:3])}" if names else prefix


def initialize_state() -> None:
    if "records" not in st.session_state:
        st.session_state.records = []
    if "original_records" not in st.session_state:
        st.session_state.original_records = copy.deepcopy(st.session_state.records)
    if "uploaded_dataset_filename" not in st.session_state:
        st.session_state.uploaded_dataset_filename = "review_result.jsonl"


def sync_selected_record_from_selectbox() -> None:
    index = st.session_state.get("record_selector")
    if not isinstance(index, int):
        return
    set_selected_record(index, len(st.session_state.records))


@st.dialog("編集内容を確認")
def confirm_next_dialog(selected_index: int) -> None:
    preview_record = copy.deepcopy(st.session_state.records[selected_index])
    update_record_status(preview_record, st.session_state.original_records[selected_index])
    current_record_json = json.dumps(
        prepare_record_for_output(preview_record),
        ensure_ascii=False,
        indent=2,
    )
    st.caption("この編集内容で次の論文に進みますか？")
    st.text_area(
        "現在のJSON",
        value=current_record_json,
        height=360,
        disabled=True,
    )

    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button("確認して次へ", type="primary", use_container_width=True):
            update_record_status(
                st.session_state.records[selected_index],
                st.session_state.original_records[selected_index],
            )
            if selected_index < len(st.session_state.records) - 1:
                st.session_state.pending_selected_record_index = selected_index + 1
                request_pdf_open()
            st.rerun()
    with cancel_col:
        if st.button("キャンセル", use_container_width=True):
            st.rerun()


initialize_state()

st.title("JSON/PDF Checker")

with st.sidebar:
    st.header("Files")
    json_file = st.file_uploader("JSON / JSONL", type=["json", "jsonl"])
    if json_file is not None:
        file_bytes = json_file.getvalue()
        upload_signature = (
            json_file.name,
            len(file_bytes),
            hashlib.sha256(file_bytes).hexdigest(),
        )
        if st.session_state.get("uploaded_dataset_signature") != upload_signature:
            try:
                text = file_bytes.decode("utf-8-sig")
                st.session_state.records = [
                    strip_position_fields(record) for record in load_json_records(text)
                ]
                for record in st.session_state.records:
                    ensure_review_metadata(record)
                st.session_state.original_records = copy.deepcopy(st.session_state.records)
                st.session_state.uploaded_dataset_signature = upload_signature
                st.session_state.uploaded_dataset_filename = json_file.name
                close_current_pdf_window()
                st.session_state.pdf_opened_request_id = None
                set_selected_record(0, len(st.session_state.records))
                st.success(f"{len(st.session_state.records)}件を読み込みました。")
            except (UnicodeDecodeError, ValueError) as exc:
                st.error(f"読み込みに失敗しました: {exc}")

    st.divider()
    st.download_button(
        "修正済みJSONLをダウンロード",
        data=dump_jsonl(st.session_state.records),
        file_name=reviewed_output_filename(st.session_state.uploaded_dataset_filename),
        mime="application/x-jsonlines",
        use_container_width=True,
        disabled=not st.session_state.records,
    )

records = st.session_state.records
if not records:
    st.info("JSON/JSONLファイルをアップロードしてください。")
    st.stop()

record_labels = [record_summary(record) for record in records]
if "selected_record_index" not in st.session_state:
    st.session_state.selected_record_index = 0
st.session_state.selected_record_index = min(
    max(int(st.session_state.selected_record_index), 0),
    len(records) - 1,
)
if "pending_selected_record_index" in st.session_state:
    set_selected_record(st.session_state.pending_selected_record_index, len(records))
    del st.session_state.pending_selected_record_index
if "record_selector" not in st.session_state:
    st.session_state.record_selector = st.session_state.selected_record_index
if st.session_state.record_selector != st.session_state.selected_record_index:
    st.session_state.record_selector = st.session_state.selected_record_index

with st.sidebar:
    st.divider()
    st.subheader("Navigation")
    st.caption(f"{st.session_state.selected_record_index + 1} / {len(records)}")

    prev_col, next_col = st.columns(2)
    with prev_col:
        st.button(
            "前の論文",
            use_container_width=True,
            disabled=st.session_state.selected_record_index <= 0,
            on_click=set_selected_record,
            args=(st.session_state.selected_record_index - 1, len(records)),
        )
    with next_col:
        st.button(
            "次の論文",
            use_container_width=True,
            disabled=st.session_state.selected_record_index >= len(records) - 1,
            on_click=set_selected_record,
            args=(st.session_state.selected_record_index + 1, len(records)),
        )

    target_record_number = st.number_input(
        "n番目の論文へ",
        min_value=1,
        max_value=len(records),
        value=st.session_state.selected_record_index + 1,
        step=1,
    )
    st.button(
        "指定番号へ移動",
        use_container_width=True,
        on_click=set_selected_record,
        args=(int(target_record_number) - 1, len(records)),
    )
selected = st.sidebar.selectbox(
    "確認するレコード",
    options=list(range(len(records))),
    key="record_selector",
    on_change=sync_selected_record_from_selectbox,
    format_func=lambda index: f"{index + 1}. {record_labels[index]}",
)

record = records[selected]
records[selected] = strip_position_fields(record)
record = records[selected]
ensure_review_metadata(record)
pdf_url = st.sidebar.text_input(
    "PDF URL",
    value=get_arxiv_pdf_url(record),
    key=f"pdf_url_{selected}",
)
if can_launch_local_browser_window():
    open_pdf_in_chrome_once(pdf_url)
    st.sidebar.caption("PDFはChromeの別ウィンドウで開きます。")
elif pdf_url:
    st.sidebar.link_button("PDFを新しいタブで開く", pdf_url, use_container_width=True)
    st.sidebar.caption("デプロイ環境では、ボタンからPC側のブラウザで開きます。")

st.markdown('<div class="json-pane-marker"></div>', unsafe_allow_html=True)
st.subheader("JSON")
tabs = st.tabs(["フォーム編集", "Raw JSON", "差分"])

with tabs[0]:
    top_cols = st.columns(2)
    with top_cols[0]:
        st.text_input("arxiv_id", value=str(record.get("arxiv_id", "")), disabled=True)
    with top_cols[1]:
        st.text_input("doc_class", value=str(record.get("doc_class", "")), disabled=True)

    author_editor(record, selected)

with tabs[1]:
    edited = st.text_area(
        "選択中レコード",
        value=json.dumps(strip_position_fields(record), ensure_ascii=False, indent=2),
        height=700,
        key=f"raw_json_{selected}",
    )
    if st.button("Raw JSONを反映", use_container_width=True):
        try:
            parsed = json.loads(edited)
            if not isinstance(parsed, dict):
                st.error("選択中レコードはJSON objectにしてください。")
            else:
                ensure_review_metadata(parsed)
                records[selected] = strip_position_fields(parsed)
                st.session_state.pdf_opened_request_id = st.session_state.get("pdf_open_request_id", 0)
                st.rerun()
        except json.JSONDecodeError as exc:
            st.error(f"JSON parse error: {exc}")

with tabs[2]:
    original = strip_position_fields(st.session_state.original_records[selected])
    before = json.dumps(original, ensure_ascii=False, indent=2).splitlines()
    after = json.dumps(strip_position_fields(record), ensure_ascii=False, indent=2).splitlines()
    if before == after:
        st.success("このレコードはまだ変更されていません。")
    else:
        st.caption("左が読み込み時、右が現在の内容です。")
        diff_cols = st.columns(2)
        with diff_cols[0]:
            st.code("\n".join(before), language="json")
        with diff_cols[1]:
            st.code("\n".join(after), language="json")

st.divider()
st.subheader("確認ステータス")
review_cols = st.columns([1, 1])
with review_cols[0]:
    needs_admin_check = st.checkbox(
        "管理者によるチェックが必要",
        value=bool(record.get("admin_check_required", False)),
        key=f"admin_check_required_{selected}",
    )
record["admin_check_required"] = needs_admin_check
if needs_admin_check:
    record["status"] = STATUS_NEEDS_REVIEW
    record["admin_check_reason"] = st.text_area(
        "チェックが必要な理由",
        value=str(record.get("admin_check_reason", "")),
        key=f"admin_check_reason_{selected}",
        height=90,
    )
else:
    record["admin_check_reason"] = ""
    if record.get("status") == STATUS_NEEDS_REVIEW:
        record["status"] = STATUS_UNCONFIRMED
with review_cols[1]:
    st.text_input("status", value=str(record.get("status", STATUS_UNCONFIRMED)), disabled=True)

if st.button("確認して次へ", type="primary", use_container_width=True):
    confirm_next_dialog(selected)
