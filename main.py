import streamlit as st
import pandas as pd
import datetime
import random
import ast
import json
import re
import time
from io import BytesIO
from gtts import gTTS
from streamlit_gsheets import GSheetsConnection

# =========================================================
# 0) Config
# =========================================================
SHEET_MAIN = "Sheet1"
QC_SHEET = "QC_Log"

QC_COLUMNS = [
    "ts", "session_id", "word_id", "word", "qtype", "question_text", "example_blank",
    "options", "correct_answers", "llm_selected", "llm_is_correct", "flag", "reasons"
]

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"  # 공식 문서 예시 모델 :contentReference[oaicite:2]{index=2}

# =========================================================
# 1) Google Sheet 연결 + 데이터 로드
# =========================================================
conn = st.connection("gsheets", type=GSheetsConnection)


def load_data():
    try:
        df = conn.read(worksheet=SHEET_MAIN, ttl=0)
        df.columns = df.columns.str.lower()

        # 중복 단어 제거
        if 'word' in df.columns:
            df = df.drop_duplicates(subset=['word'], keep='first')

        needs_initial_save = False

        # 기본 SRS 컬럼 보장
        for col in ['mistake_count', 'box', 'next_review']:
            if col not in df.columns:
                if col in ['mistake_count', 'box']:
                    df[col] = 0
                else:
                    df[col] = '0000-00-00'
                needs_initial_save = True

        # MCQ용 컬럼 보장
        for col in ['example_blank', 'collocations', 'confusables']:
            if col not in df.columns:
                df[col] = ''
                needs_initial_save = True

        # 타입 정리
        df['mistake_count'] = df['mistake_count'].fillna(0).astype(int)
        df['box'] = df['box'].fillna(0).astype(int)
        df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

        # id 필수
        if 'id' not in df.columns:
            st.error("ERROR: 'id' column not found in Sheet1.")
            st.stop()

        if needs_initial_save:
            conn.update(worksheet=SHEET_MAIN, data=df)
            st.toast("Updated Google Sheet structure (added columns).")

        if df.empty:
            st.warning("Google Sheet is empty.")
            st.stop()

        return df

    except Exception as e:
        st.error(f"Google Sheet Connection Error: {e}")
        st.stop()


def ensure_qc_sheet_and_header():
    """
    QC_Log 워크시트가 있고, 헤더가 맞도록 보장.
    seed row(ts="__seed__")를 맨 위에 둬서
    Streamlit-GSheets 환경에서 헤더 꼬임을 방어.
    """
    try:
        df_old = conn.read(worksheet=QC_SHEET, ttl=0)

        if df_old is None or df_old.empty or len(getattr(df_old, "columns", [])) == 0:
            seed = {c: "" for c in QC_COLUMNS}
            seed["ts"] = "__seed__"
            df_init = pd.DataFrame([seed], columns=QC_COLUMNS)
            conn.update(worksheet=QC_SHEET, data=df_init)
            return True

        df_old.columns = df_old.columns.str.lower()

        # 컬럼 동기화
        missing = [c for c in QC_COLUMNS if c not in df_old.columns]
        for c in missing:
            df_old[c] = ""

        df_old = df_old[QC_COLUMNS]

        # seed row 보장
        if not (df_old["ts"].astype(str) == "__seed__").any():
            seed = {c: "" for c in QC_COLUMNS}
            seed["ts"] = "__seed__"
            df_old = pd.concat([pd.DataFrame([seed]), df_old], ignore_index=True)

        conn.update(worksheet=QC_SHEET, data=df_old)
        return True

    except Exception:
        st.warning(f"Worksheet '{QC_SHEET}' not found. Please create it in Google Sheet.")
        st.info("Google Sheet에 QC_Log 탭(worksheet)을 만든 뒤 rerun 하세요. 헤더는 자동 생성됩니다.")
        return False


def append_qc_log(rows):
    """
    rows: list[dict]
    - seed row 제거 후 append, 맨 위에 seed 재삽입
    - llm_selected/llm_is_correct는 절대 빈값 방지
    """
    if not rows:
        return
    if not ensure_qc_sheet_and_header():
        return

    try:
        df_old = conn.read(worksheet=QC_SHEET, ttl=0)
        if df_old is None or len(getattr(df_old, "columns", [])) == 0:
            df_old = pd.DataFrame(columns=QC_COLUMNS)

        df_old.columns = df_old.columns.str.lower()

        df_new = pd.DataFrame(rows)
        if df_new is None or df_new.empty:
            return
        df_new.columns = df_new.columns.str.lower()

        # 컬럼 보정
        for c in QC_COLUMNS:
            if c not in df_old.columns:
                df_old[c] = ""
            if c not in df_new.columns:
                df_new[c] = ""

        df_old = df_old[QC_COLUMNS]
        df_new = df_new[QC_COLUMNS]

        # seed row 제거
        df_old = df_old[df_old["ts"].astype(str) != "__seed__"]

        # llm 컬럼 비면 강제 채움
        def _fill_llm(row_dict):
            opt_list = []
            try:
                opt_list = json.loads(row_dict.get("options", "[]"))
            except Exception:
                opt_list = []

            if not str(row_dict.get("llm_selected", "")).strip():
                row_dict["llm_selected"] = opt_list[0] if opt_list else ""

            if not str(row_dict.get("llm_is_correct", "")).strip():
                try:
                    ca = set(json.loads(row_dict.get("correct_answers", "[]")))
                except Exception:
                    ca = set()
                row_dict["llm_is_correct"] = "TRUE" if (row_dict["llm_selected"] in ca) else "FALSE"

            return row_dict

        df_new = df_new.apply(lambda r: pd.Series(_fill_llm(r.to_dict())), axis=1)

        merged = pd.concat([df_old, df_new], ignore_index=True)
        merged = merged.fillna("")

        # seed row 다시 삽입
        seed = {c: "" for c in QC_COLUMNS}
        seed["ts"] = "__seed__"
        merged = pd.concat([pd.DataFrame([seed]), merged], ignore_index=True)

        conn.update(worksheet=QC_SHEET, data=merged)

    except Exception as e:
        st.error(f"QC_Log append failed: {e}")


# =========================================================
# 2) Session State
# =========================================================
if 'vocab_db' not in st.session_state:
    st.session_state.vocab_db = load_data()

if 'app_mode' not in st.session_state:
    st.session_state.app_mode = 'setup'
if 'session_config' not in st.session_state:
    st.session_state.session_config = {}
if 'session_stats' not in st.session_state:
    st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
if 'current_word_id' not in st.session_state:
    st.session_state.current_word_id = None
if 'quiz_options' not in st.session_state:
    st.session_state.quiz_options = []
if 'quiz_answered' not in st.session_state:
    st.session_state.quiz_answered = False
if 'selected_option' not in st.session_state:
    st.session_state.selected_option = None

if 'question_type' not in st.session_state:
    st.session_state.question_type = None
if 'correct_answers' not in st.session_state:
    st.session_state.correct_answers = set()
if 'question_text' not in st.session_state:
    st.session_state.question_text = ""
if 'example_blank_to_show' not in st.session_state:
    st.session_state.example_blank_to_show = ""


# =========================================================
# 3) Core Logic
# =========================================================
def get_next_word():
    df = st.session_state.vocab_db
    config = st.session_state.session_config

    difficulty = config.get('difficulty', (1, 3))
    mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])

    topic = config.get('topic', 'All')
    if topic != "All":
        mask = mask & (df['topic'] == topic)

    mode = config.get('mode', 'Standard Study (SRS)')
    today_str = str(datetime.date.today())

    if mode == 'Review Mistakes Only':
        logic_mask = (df['box'] == 0) & (df['mistake_count'] > 0)
        if df[mask & logic_mask].empty:
            st.toast("No historical mistakes found! (Box 0 & Count > 0)")
    else:
        logic_mask = df['next_review'] <= today_str

    candidates = df[mask & logic_mask]
    if len(candidates) == 0:
        return None

    selected = candidates.sample(1).iloc[0]
    return selected['id']


def update_srs(word_id, is_correct):
    df = st.session_state.vocab_db
    idx_list = df[df['id'] == word_id].index.tolist()
    if not idx_list:
        return
    idx = idx_list[0]

    current_box = int(df.at[idx, 'box'])
    current_mistakes = int(df.at[idx, 'mistake_count'])

    if is_correct:
        st.session_state.session_stats['correct'] += 1
        new_box = min(current_box + 1, 5)
        days_to_add = int(2 ** new_box)
        new_mistakes = current_mistakes
    else:
        st.session_state.session_stats['wrong'] += 1
        new_box = 0
        days_to_add = 0
        new_mistakes = current_mistakes + 1

    st.session_state.session_stats['total'] += 1
    next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)

    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    st.session_state.vocab_db.at[idx, 'mistake_count'] = new_mistakes

    try:
        conn.update(worksheet=SHEET_MAIN, data=st.session_state.vocab_db)
    except Exception as e:
        st.error(f"Save failed: {e}")


def parse_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x.strip() != "":
        try:
            v = ast.literal_eval(x)
            if isinstance(v, list):
                return v
            return [str(v)]
        except Exception:
            return [x]
    return []


def build_question_for_word(word_row, df_all):
    # word_row: dict(시뮬) 또는 Series(퀴즈)
    def _get(key, default=""):
        if isinstance(word_row, dict):
            return word_row.get(key, default)
        return word_row.get(key, default)

    new_id = int(_get('id'))
    word_text = str(_get('word', '')).strip()
    target_pos = str(_get('pos', '')).strip().lower()
    target_topic = str(_get('topic', '')).strip()

    example_blank = str(_get('example_blank', '')).strip()
    can_blank = (example_blank != "" and example_blank.lower() not in ['nan', 'none'])

    qtype = random.choice(['synonym', 'blank'])
    if qtype == 'blank' and not can_blank:
        qtype = 'synonym'

    df_pool = df_all.copy()
    df_pool['pos_norm'] = df_pool['pos'].fillna('').astype(str).str.strip().str.lower()

    # [A] Synonym
    if qtype == 'synonym':
        synonyms = parse_list(_get('synonyms', ''))
        synonyms = [s for s in synonyms if isinstance(s, str) and s.strip() != ""]
        if not synonyms:
            if can_blank:
                qtype = 'blank'
            else:
                synonyms = [word_text]

        if qtype == 'synonym':
            question_text = f"### What is a synonym for: **{word_text}**?"
            correct_set = set(synonyms)

            correct_option = random.choice(list(correct_set))
            options = [correct_option]

            if target_pos and target_pos != 'nan':
                candidate_df = df_pool[(df_pool['pos_norm'] == target_pos) & (df_pool['id'] != new_id)]
                if candidate_df.empty:
                    candidate_df = df_pool[df_pool['id'] != new_id]
            else:
                candidate_df = df_pool[df_pool['id'] != new_id]

            wrong_pool = []
            for syn_list in candidate_df['synonyms']:
                for w in parse_list(syn_list):
                    if isinstance(w, str) and w.strip():
                        wrong_pool.append(w)

            wrong_pool = list(set([w for w in wrong_pool if w and w not in correct_set]))
            needed = 3
            if len(wrong_pool) >= needed:
                wrong_options = random.sample(wrong_pool, needed)
            else:
                defaults = ["Option A", "Option B", "Option C"]
                wrong_options = wrong_pool + defaults[:needed - len(wrong_pool)]

            options += wrong_options
            random.shuffle(options)

            return qtype, question_text, options, correct_set, {'example_blank': ''}

    # [B] Blank
    question_text = "### Fill in the blank with the best word:"
    correct_set = {word_text}

    confusables = parse_list(_get('confusables', ''))
    confusables = [c for c in confusables if isinstance(c, str) and c.strip() and c != word_text]

    options = [word_text]
    for c in confusables:
        if len(options) >= 4:
            break
        if c not in options:
            options.append(c)

    if len(options) < 4:
        cand = df_pool[df_pool['id'] != new_id]
        if target_topic:
            cand = cand[cand['topic'] == target_topic]
        if target_pos and target_pos != 'nan':
            cand_pos = cand[cand['pos_norm'] == target_pos]
            if not cand_pos.empty:
                cand = cand_pos

        filler = cand['word'].dropna().astype(str).tolist()
        filler = [w for w in list(set(filler)) if w != word_text and w.strip()]
        random.shuffle(filler)

        for w in filler:
            if len(options) >= 4:
                break
            if w not in options:
                options.append(w)

    while len(options) < 4:
        options.append(f"Option {len(options)}")

    random.shuffle(options)
    return 'blank', question_text, options, correct_set, {'example_blank': example_blank}


# =========================================================
# 4) Gemini QC (REAL) - google-genai SDK
# =========================================================
def list_gemini_models(api_key: str):
    """
    generateContent 가능한 모델만 반환.
    공식 문서의 models.list 패턴 기반 :contentReference[oaicite:3]{index=3}
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    names = []
    for m in client.models.list():
        # m.supported_actions 또는 m.supported_actions 유사 필드가 올 수 있음
        supported = getattr(m, "supported_actions", None) or getattr(m, "supportedActions", None) or []
        if any(a == "generateContent" for a in supported):
            # m.name 예: "models/gemini-2.0-flash"
            names.append(m.name)
    return names


# def gemini_pick_option(api_key: str, model_name: str, question_text: str, example_blank: str, options: list[str]):
#     """
#     Gemini가 보기 중 하나를 선택하도록 하고,
#     selected(옵션 문자열) + rationale(짧게) 반환.
#     실패하면 (None, reason)
#     """
#     from google import genai

#     client = genai.Client(api_key=api_key)

#     prompt = f"""
# You are taking a multiple-choice TOEFL vocabulary quiz.

# QUESTION:
# {question_text}

# SENTENCE (if any):
# {example_blank if example_blank else ""}

# OPTIONS (choose exactly one):
# {json.dumps(options, ensure_ascii=False)}

# Return ONLY valid JSON:
# {{
#   "selected": "<must be exactly one of the OPTIONS strings>",
#   "rationale": "<one short sentence>"
# }}
# """

#     try:
#         resp = client.models.generate_content(
#             model=model_name,
#             contents=prompt
#         )

#         text = ""
#         # google-genai 응답 구조 방어적으로 처리
#         if hasattr(resp, "text") and resp.text:
#             text = resp.text.strip()
#         else:
#             text = str(resp).strip()

#         # 모델이 주변 텍스트 붙일 수도 있어서 JSON만 추출
#         m = re.search(r"\{.*\}", text, flags=re.DOTALL)
#         if not m:
#             return None, f"JSON not found in response: {text[:120]}"

#         data = json.loads(m.group(0))
#         selected = str(data.get("selected", "")).strip()
#         rationale = str(data.get("rationale", "")).strip()
#         return {"selected": selected, "rationale": rationale}, None

#     except Exception as e:
#         return None, f"{type(e).__name__}: {e}"

def gemini_pick_option(api_key: str, model_name: str, question_text: str, example_blank: str, options: list[str],
                       max_retries: int = 5):
    """
    Gemini 호출 (429/일시 오류는 재시도)
    """
    from google import genai
    client = genai.Client(api_key=api_key)

    prompt = f"""
You are taking a multiple-choice TOEFL vocabulary quiz.

QUESTION:
{question_text}

SENTENCE (if any):
{example_blank if example_blank else ""}

OPTIONS (choose exactly one):
{json.dumps(options, ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "selected": "<must be exactly one of the OPTIONS strings>",
  "rationale": "<one short sentence>"
}}
"""

    last_err = None

    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )

            text = getattr(resp, "text", "") or str(resp)
            text = text.strip()

            m = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not m:
                return None, f"JSON not found in response: {text[:120]}"

            data = json.loads(m.group(0))
            selected = str(data.get("selected", "")).strip()
            rationale = str(data.get("rationale", "")).strip()
            return {"selected": selected, "rationale": rationale}, None

        except Exception as e:
            last_err = e

            msg = str(e)
            is_rate_limited = ("429" in msg) or ("RESOURCE_EXHAUSTED" in msg)
            is_transient = is_rate_limited or ("503" in msg) or ("UNAVAILABLE" in msg) or ("DEADLINE" in msg)

            if not is_transient:
                break

            # 지수 백오프 + 지터
            sleep_s = (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(sleep_s)

    return None, f"{type(last_err).__name__}: {last_err}"


def qc_with_gemini_or_fallback(question_text, example_blank, options, correct_answers, use_gemini, api_key, model_name):
    """
    반환: dict {flag, reasons, llm_selected, llm_is_correct}
    - llm_selected / llm_is_correct는 절대 빈값 방지
    - flag는 '구조 오류' 위주로 1로 둠 (원하면 LLM 오답도 flag로 올릴 수 있음)
    """
    reasons = []
    flag = 0

    correct_answers = set(correct_answers)

    # 구조 체크
    if not any(opt in correct_answers for opt in options):
        flag = 1
        reasons.append("No correct answer included in options.")

    if "Fill in the blank" in question_text and (not example_blank or not str(example_blank).strip()):
        flag = 1
        reasons.append("Blank question has empty example_blank.")

    # fallback 항상 준비
    # fallback_selected = options[0] if options else ""
    fallback_selected = random.choice(options) if options else ""
    fallback_is_correct = "TRUE" if (fallback_selected in correct_answers) else "FALSE"

    if not use_gemini:
        # Gemini 안 쓰더라도 선택값은 채움
        return {
            "flag": int(flag),
            "reasons": reasons + ["Used fallback (Gemini disabled)."],
            "llm_selected": fallback_selected,
            "llm_is_correct": fallback_is_correct
        }

    # Gemini 사용
    data, err = gemini_pick_option(api_key, model_name, question_text, example_blank, options)
    if err or not data:
        # 실패하면 fallback
        reasons.append(f"Gemini call failed; used fallback selection. ({err})")
        # (선택) Gemini 실패 자체를 flag로 보고 싶으면 여기서 flag=1
        flag = 1
        return {
            "flag": int(flag),
            "reasons": reasons,
            "llm_selected": fallback_selected,
            "llm_is_correct": fallback_is_correct
        }

    selected = str(data.get("selected", "")).strip()
    rationale = str(data.get("rationale", "")).strip()

    if selected not in options:
        # 규칙 위반이면 fallback
        flag = 1
        reasons.append("Gemini selected an option not in the provided options; used fallback.")
        selected = fallback_selected

    is_correct = "TRUE" if (selected in correct_answers) else "FALSE"
    if rationale:
        reasons.append(f"Gemini rationale: {rationale}")

    # (옵션) Gemini가 틀리면 flag 올리고 싶으면 아래 주석 해제
    # if is_correct == "FALSE":
    #     flag = 1
    #     reasons.append("Gemini answered incorrectly (may indicate confusing item).")

    return {
        "flag": int(flag),
        "reasons": reasons,
        "llm_selected": selected,
        "llm_is_correct": is_correct
    }


# =========================================================
# 5) UI
# =========================================================
st.title("🎓 NicholaSOOBIN TOEFL Voca")

with st.sidebar:
    st.header("Data Management")
    if st.button("Reset All Progress"):
        df_reset = st.session_state.vocab_db.copy()
        df_reset['box'] = 0
        df_reset['next_review'] = '0000-00-00'
        df_reset['mistake_count'] = 0
        conn.update(worksheet=SHEET_MAIN, data=df_reset)
        st.toast("All progress has been reset.")
        st.session_state.clear()
        st.rerun()

    st.divider()
    st.header("QC (Gemini)")

    # ---- secrets 디버그: 지금 앱이 키를 읽는지 바로 확인 ----
    with st.expander("🔎 Secrets debug (배포 후 한 번만 확인)"):
        has_key = "GEMINI_API_KEY" in st.secrets
        st.write("Has GEMINI_API_KEY in st.secrets:", has_key)
        if has_key:
            st.write("Key length:", len(str(st.secrets.get("GEMINI_API_KEY", ""))))
        st.caption("키 값 자체는 노출하지 않습니다.")

    use_gemini = st.toggle(
        "Use Gemini API",
        value=True,
        help="ON이면 Gemini가 실제로 문제를 풀어 선택/정오답을 기록합니다."
    )

    sim_n = st.number_input("Simulate N questions", min_value=1, max_value=2000, value=100, step=50)

    # 모델 목록 로딩 (키가 있을 때만)
    api_key = st.secrets.get("GEMINI_API_KEY", "")
    model_candidates = []
    model_name = DEFAULT_GEMINI_MODEL

    if api_key:
        try:
            model_candidates = list_gemini_models(api_key)
        except Exception as e:
            model_candidates = []
            st.caption(f"Model list failed (will use default): {type(e).__name__}")

    if model_candidates:
        # 모델명은 "models/..." 형태로 올 수 있음. generate_content에는 그걸 그대로 써도 됨.
        model_name = st.selectbox(
            "Gemini model (generateContent 지원)",
            options=model_candidates,
            index=min(0, len(model_candidates) - 1)
        )
    else:
        st.caption(f"Using default model: {DEFAULT_GEMINI_MODEL}")

    if st.button("Run QC Simulation"):
        ok = ensure_qc_sheet_and_header()
        if not ok:
            st.stop()

        if use_gemini and not api_key:
            st.error("❌ GEMINI_API_KEY not found in st.secrets")
            st.stop()

        df_all = st.session_state.vocab_db
        session_id = random.randint(10, 10000)

        logs = []
        flagged = 0

        sampled = df_all.sample(min(int(sim_n), len(df_all))).to_dict("records")

        for row in sampled:
            qtype, qtext, options, correct_set, extra = build_question_for_word(row, df_all)
            ex_blank = extra.get("example_blank", "")

            qc = qc_with_gemini_or_fallback(
                question_text=qtext,
                example_blank=ex_blank,
                options=options,
                correct_answers=correct_set,
                use_gemini=use_gemini,
                api_key=api_key,
                model_name=model_name if model_candidates else DEFAULT_GEMINI_MODEL
            )

            if int(qc.get("flag", 0)) == 1:
                flagged += 1

            llm_selected = str(qc.get("llm_selected", "")).strip()
            llm_is_correct = str(qc.get("llm_is_correct", "")).strip()

            # 절대 empty 방지
            if not llm_selected:
                llm_selected = options[0] if options else ""
            if not llm_is_correct:
                llm_is_correct = "TRUE" if (llm_selected in correct_set) else "FALSE"

            logs.append({
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "session_id": int(session_id),
                "word_id": int(row.get("id")),
                "word": str(row.get("word", "")),
                "qtype": qtype,
                "question_text": qtext,
                "example_blank": ex_blank,
                "options": json.dumps(options, ensure_ascii=False),
                "correct_answers": json.dumps(sorted(list(correct_set)), ensure_ascii=False),
                "llm_selected": llm_selected,
                "llm_is_correct": llm_is_correct,  # TRUE/FALSE
                "flag": int(qc.get("flag", 0)),
                "reasons": json.dumps(qc.get("reasons", []), ensure_ascii=False),
            })

        append_qc_log(logs)
        st.success(f"QC done. Flagged: {flagged} / {len(logs)} (session_id={session_id})")
        st.caption("Google Sheet → QC_Log 탭에서 확인하세요.")

# ---------------------------
# Main App: Setup / Quiz / Summary
# ---------------------------
if st.session_state.app_mode == 'setup':
    st.markdown("### ⚙️ Study Setup")

    with st.form("setup_form"):
        c1, c2 = st.columns(2)
        with c1:
            topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
            sel_topic = st.selectbox("Topic", topic_list)
            sel_mode = st.radio(
                "Mode",
                ["Standard Study (SRS)", "Review Mistakes Only"],
                help="Standard: New & Due words | Mistakes: Words you got wrong before"
            )
        with c2:
            sel_goal = st.selectbox("Daily Goal", [5, 10, 15, 20, 30])
            sel_diff = st.slider("Difficulty", 1, 3, (1, 3))

        submitted = st.form_submit_button("🚀 Start Session", use_container_width=True)

        if submitted:
            st.session_state.session_config = {
                'topic': sel_topic,
                'goal': sel_goal,
                'difficulty': sel_diff,
                'mode': sel_mode
            }
            st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
            st.session_state.app_mode = 'quiz'
            st.rerun()

elif st.session_state.app_mode == 'quiz':
    config = st.session_state.session_config
    stats = st.session_state.session_stats

    goal = config['goal']
    current = stats['total']
    st.progress(min(current / goal, 1.0))
    st.caption(f"Progress: {current} / {goal} (Topic: {config['topic']})")

    if current >= goal:
        st.session_state.app_mode = 'summary'
        st.rerun()

    df_all = st.session_state.vocab_db

    if st.session_state.current_word_id is None:
        new_id = get_next_word()
        if new_id is not None:
            st.session_state.current_word_id = new_id
            current_word = df_all[df_all['id'] == new_id].iloc[0]

            qtype, qtext, options, correct_set, extra = build_question_for_word(current_word, df_all)

            st.session_state.question_type = qtype
            st.session_state.question_text = qtext
            st.session_state.quiz_options = options
            st.session_state.correct_answers = correct_set
            st.session_state.example_blank_to_show = extra.get('example_blank', '')

            st.session_state.quiz_answered = False
            st.session_state.selected_option = None
        else:
            st.warning("No words matching your criteria!")
            if config['mode'] == 'Review Mistakes Only':
                st.info("💡 You have no recorded mistakes yet! Try 'Standard Study (SRS)'.")
            if st.button("Back to Setup"):
                st.session_state.app_mode = 'setup'
                st.rerun()
            st.stop()

    current_id = st.session_state.current_word_id
    current_word_row = df_all[df_all['id'] == current_id].iloc[0]
    word_text = str(current_word_row.get('word', '')).strip()

    st.markdown(st.session_state.question_text)

    if st.session_state.question_type == 'blank':
        blank_sentence = st.session_state.example_blank_to_show
        if blank_sentence:
            st.info(blank_sentence)

    try:
        sound_file = BytesIO()
        tts = gTTS(text=word_text, lang='en')
        tts.write_to_fp(sound_file)
        sound_file.seek(0)
        st.audio(sound_file, format='audio/mpeg')
    except Exception:
        pass

    st.caption(f"Part of Speech: *{current_word_row.get('pos', '')}*")

    if not st.session_state.quiz_answered:
        cols = st.columns(2)
        for i, option in enumerate(st.session_state.quiz_options):
            if cols[i % 2].button(option, key=f"btn_{i}", use_container_width=True):
                st.session_state.quiz_answered = True
                st.session_state.selected_option = option

                is_correct = option in st.session_state.correct_answers
                update_srs(current_id, is_correct)
                st.rerun()

    else:
        selected = st.session_state.selected_option
        is_correct = selected in st.session_state.correct_answers
        final_answer_text = list(st.session_state.correct_answers)[0] if st.session_state.correct_answers else word_text

        if is_correct:
            st.success(f"✅ Correct! **'{selected}'**")
        else:
            st.error(f"❌ Incorrect. The answer is **'{final_answer_text}'**.")

        st.markdown("---")
        st.markdown(f"#### 📖 Study: **{word_text}**")

        st.info(
            f"**Definition:** {current_word_row.get('definition','')}\n\n"
            f"**Example:** *{current_word_row.get('example','')}*"
        )

        if st.session_state.question_type == 'blank':
            colls = parse_list(current_word_row.get('collocations', ''))
            if colls:
                st.caption("Collocations: " + ", ".join(colls))

        if st.button("Next Question ➡️", type="primary"):
            st.session_state.current_word_id = None
            st.session_state.quiz_answered = False
            st.session_state.selected_option = None
            st.session_state.correct_answers = set()
            st.session_state.question_type = None
            st.session_state.question_text = ""
            st.session_state.quiz_options = []
            st.session_state.example_blank_to_show = ""
            st.rerun()

elif st.session_state.app_mode == 'summary':
    st.balloons()
    st.markdown("## 🏆 Session Complete!")

    stats = st.session_state.session_stats
    score = int((stats['correct'] / stats['total']) * 100) if stats['total'] > 0 else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("Total", stats['total'])
    col2.metric("Correct 🟢", stats['correct'])
    col3.metric("Wrong 🔴", stats['wrong'])

    st.progress(score / 100)
    st.caption(f"Final Score: {score}%")

    st.divider()

    if st.button("🏠 Back to Home", use_container_width=True):
        st.session_state.app_mode = 'setup'
        st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
        st.rerun()





# ###########################################
# ###########################################
# # Synonym/MCQ quiz 모드로 잘 작동하고 있는 버전
# ###########################################
# ###########################################

# import streamlit as st
# import pandas as pd
# import datetime
# import random
# import ast
# from io import BytesIO
# from gtts import gTTS
# from streamlit_gsheets import GSheetsConnection

# # ---------------------------------------------------------
# # 1. 데이터 및 세션 초기화
# # ---------------------------------------------------------
# conn = st.connection("gsheets", type=GSheetsConnection)

# def load_data():
#     try:
#         # 캐시 없이 매번 최신 데이터 로드
#         df = conn.read(worksheet="Sheet1", ttl=0)

#         # 1. 컬럼명 소문자 통일
#         df.columns = df.columns.str.lower()

#         # 2. 중복 단어 제거
#         df = df.drop_duplicates(subset=['word'], keep='first')

#         # 3. 컬럼 구조 동기화 체크
#         needs_initial_save = False

#         # mistake_count 없으면 생성
#         if 'mistake_count' not in df.columns:
#             df['mistake_count'] = 0
#             needs_initial_save = True

#         # box 없으면 생성
#         if 'box' not in df.columns:
#             df['box'] = 0
#             needs_initial_save = True

#         # next_review 없으면 생성
#         if 'next_review' not in df.columns:
#             df['next_review'] = '0000-00-00'
#             needs_initial_save = True

#         # ---- [NEW] MCQ용 컬럼이 없으면 생성 ----
#         for col in ['example_blank', 'collocations', 'confusables']:
#             if col not in df.columns:
#                 df[col] = ''
#                 needs_initial_save = True

#         # 데이터 타입 정리 (NaN 방지)
#         df['mistake_count'] = df['mistake_count'].fillna(0).astype(int)
#         df['box'] = df['box'].fillna(0).astype(int)
#         df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

#         # [중요] 컬럼을 새로 만들었으면 시트에도 즉시 반영하여 헤더를 생성함
#         if needs_initial_save:
#             conn.update(worksheet="Sheet1", data=df)
#             st.toast("Updated Google Sheet structure (added columns).")

#         if df.empty:
#             st.warning("Google Sheet is empty.")
#             st.stop()

#         return df
#     except Exception as e:
#         st.error(f"Google Sheet Connection Error: {e}")
#         st.stop()

# if 'vocab_db' not in st.session_state:
#     st.session_state.vocab_db = load_data()

# # 데이터 전처리 (세션용)
# df = st.session_state.vocab_db

# # --- [앱 상태 관리 변수들] ---
# if 'app_mode' not in st.session_state:
#     st.session_state.app_mode = 'setup'
# if 'session_config' not in st.session_state:
#     st.session_state.session_config = {}
# if 'session_stats' not in st.session_state:
#     st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
# if 'current_word_id' not in st.session_state:
#     st.session_state.current_word_id = None
# if 'quiz_options' not in st.session_state:
#     st.session_state.quiz_options = []
# if 'quiz_answered' not in st.session_state:
#     st.session_state.quiz_answered = False
# if 'selected_option' not in st.session_state:
#     st.session_state.selected_option = None

# # ---- [NEW] 문제 타입/정답/문항 텍스트 상태 ----
# if 'question_type' not in st.session_state:
#     st.session_state.question_type = None  # 'synonym' or 'blank'
# if 'correct_answers' not in st.session_state:
#     st.session_state.correct_answers = set()
# if 'question_text' not in st.session_state:
#     st.session_state.question_text = ""

# # ---------------------------------------------------------
# # 2. 로직 함수
# # ---------------------------------------------------------
# def get_next_word():
#     df = st.session_state.vocab_db
#     config = st.session_state.session_config

#     # 1. 난이도 필터
#     difficulty = config.get('difficulty', (1, 3))
#     mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])

#     # 2. 주제 필터
#     topic = config.get('topic', 'All')
#     if topic != "All":
#         mask = mask & (df['topic'] == topic)

#     # 3. 모드별 필터
#     mode = config.get('mode', 'Standard Study (SRS)')
#     today_str = str(datetime.date.today())

#     if mode == 'Review Mistakes Only':
#         # 오답 노트: Box가 0이면서 AND 오답 횟수가 1 이상인 것
#         logic_mask = (df['box'] == 0) & (df['mistake_count'] > 0)

#         # 틀린 단어가 없으면 안내 후 일반 모드로 전환 고려 (여기선 토스트만)
#         if df[mask & logic_mask].empty:
#             st.toast("No historical mistakes found! (Box 0 & Count > 0)")

#     else:
#         # 일반 모드: 오늘 복습해야 할 단어 OR 아직 안 본 단어
#         logic_mask = df['next_review'] <= today_str

#     candidates = df[mask & logic_mask]

#     if len(candidates) == 0:
#         return None

#     selected = candidates.sample(1).iloc[0]
#     return selected['id']

# def update_srs(word_id, is_correct):
#     df = st.session_state.vocab_db
#     idx_list = df[df['id'] == word_id].index.tolist()
#     if not idx_list:
#         return
#     idx = idx_list[0]

#     current_box = int(df.at[idx, 'box'])
#     current_mistakes = int(df.at[idx, 'mistake_count'])

#     if is_correct:
#         st.session_state.session_stats['correct'] += 1
#         new_box = min(current_box + 1, 5)
#         days_to_add = int(2 ** new_box)
#         new_mistakes = current_mistakes
#     else:
#         st.session_state.session_stats['wrong'] += 1
#         new_box = 0
#         days_to_add = 0
#         new_mistakes = current_mistakes + 1

#     st.session_state.session_stats['total'] += 1

#     next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)

#     st.session_state.vocab_db.at[idx, 'box'] = new_box
#     st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
#     st.session_state.vocab_db.at[idx, 'mistake_count'] = new_mistakes

#     try:
#         conn.update(worksheet="Sheet1", data=st.session_state.vocab_db)
#     except Exception as e:
#         st.error(f"Save failed: {e}")

# # ---- [NEW] 유틸: 리스트 파서 ----
# def parse_list(x):
#     if isinstance(x, list):
#         return x
#     if isinstance(x, str) and x.strip() != "":
#         try:
#             v = ast.literal_eval(x)
#             if isinstance(v, list):
#                 return v
#             return [str(v)]
#         except:
#             return [x]
#     return []

# # ---- [NEW] 문제 생성 함수: synonym + blank 섞기 ----
# def build_question_for_word(word_row, df_all):
#     """
#     Returns:
#       question_type: 'synonym' or 'blank'
#       question_text: markdown
#       options: list[str]
#       correct_answers: set[str]  (synonym은 여러 정답 가능)
#       extra_display: dict (blank 문장 등)
#     """
#     new_id = int(word_row['id'])
#     word_text = str(word_row.get('word', '')).strip()
#     target_pos = str(word_row.get('pos', '')).strip().lower()
#     target_topic = str(word_row.get('topic', '')).strip()

#     example_blank = str(word_row.get('example_blank', '')).strip()
#     can_blank = (example_blank != "" and example_blank.lower() not in ['nan', 'none'])

#     # 50:50 섞기
#     qtype = random.choice(['synonym', 'blank'])
#     if qtype == 'blank' and not can_blank:
#         qtype = 'synonym'

#     df_pool = df_all.copy()
#     df_pool['pos_norm'] = df_pool['pos'].fillna('').astype(str).str.strip().str.lower()

#     # ---------------------------
#     # [A] Synonym 문제
#     # ---------------------------
#     if qtype == 'synonym':
#         synonyms = parse_list(word_row.get('synonyms', ''))
#         synonyms = [s for s in synonyms if isinstance(s, str) and s.strip() != ""]
#         if not synonyms:
#             # synonyms가 비어있으면 blank로 대체 시도
#             if can_blank:
#                 qtype = 'blank'
#             else:
#                 synonyms = [word_text]

#         if qtype == 'synonym':
#             question_text = f"### What is a synonym for: **{word_text}**?"
#             correct_set = set(synonyms)

#             # 보기: 정답 1 + 오답 3
#             correct_option = random.choice(list(correct_set))
#             options = [correct_option]

#             # 오답 풀: 같은 POS(가능하면)에서 synonyms 모으기
#             if target_pos and target_pos != 'nan':
#                 candidate_df = df_pool[(df_pool['pos_norm'] == target_pos) & (df_pool['id'] != new_id)]
#                 if candidate_df.empty:
#                     candidate_df = df_pool[df_pool['id'] != new_id]
#             else:
#                 candidate_df = df_pool[df_pool['id'] != new_id]

#             wrong_pool = []
#             for syn_list in candidate_df['synonyms']:
#                 for w in parse_list(syn_list):
#                     if isinstance(w, str) and w.strip() != "":
#                         wrong_pool.append(w)

#             wrong_pool = list(set([w for w in wrong_pool if w and w not in correct_set]))
#             needed = 3
#             if len(wrong_pool) >= needed:
#                 wrong_options = random.sample(wrong_pool, needed)
#             else:
#                 defaults = ["Option A", "Option B", "Option C"]
#                 wrong_options = wrong_pool + defaults[:needed - len(wrong_pool)]

#             options += wrong_options
#             random.shuffle(options)

#             return qtype, question_text, options, correct_set, {'example_blank': ''}

#     # ---------------------------
#     # [B] Blank MCQ 문제
#     # ---------------------------
#     question_text = "### Fill in the blank with the best word:"
#     correct_set = set([word_text])

#     # 보기: 정답 + confusables 우선 + 부족하면 같은 topic+pos 단어로 채움
#     confusables = parse_list(word_row.get('confusables', ''))
#     confusables = [c for c in confusables if isinstance(c, str) and c.strip() != "" and c != word_text]

#     options = [word_text]

#     for c in confusables:
#         if len(options) >= 4:
#             break
#         if c not in options:
#             options.append(c)

#     if len(options) < 4:
#         cand = df_pool[df_pool['id'] != new_id]
#         if target_topic:
#             cand = cand[cand['topic'] == target_topic]
#         if target_pos and target_pos != 'nan':
#             cand_pos = cand[cand['pos_norm'] == target_pos]
#             if not cand_pos.empty:
#                 cand = cand_pos

#         filler = cand['word'].dropna().astype(str).tolist()
#         filler = [w for w in list(set(filler)) if w != word_text and w.strip() != ""]
#         random.shuffle(filler)

#         for w in filler:
#             if len(options) >= 4:
#                 break
#             if w not in options:
#                 options.append(w)

#     while len(options) < 4:
#         options.append(f"Option {len(options)}")

#     random.shuffle(options)
#     return 'blank', question_text, options, correct_set, {'example_blank': example_blank}

# # ---------------------------------------------------------
# # 3. UI 구성
# # ---------------------------------------------------------
# st.title("🎓 NicholaSOOBIN TOEFL Voca")

# # 사이드바 데이터 관리
# with st.sidebar:
#     st.header("Data Management")
#     if st.button("Reset All Progress"):
#         df_reset = st.session_state.vocab_db.copy()
#         df_reset['box'] = 0
#         df_reset['next_review'] = '0000-00-00'
#         df_reset['mistake_count'] = 0
#         conn.update(worksheet="Sheet1", data=df_reset)
#         st.toast("All progress has been reset.")
#         st.session_state.clear()
#         st.rerun()

# # --- 화면 1: 설정 (Setup) ---
# if st.session_state.app_mode == 'setup':
#     st.markdown("### ⚙️ Study Setup")

#     with st.form("setup_form"):
#         c1, c2 = st.columns(2)
#         with c1:
#             topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
#             sel_topic = st.selectbox("Topic", topic_list)
#             sel_mode = st.radio(
#                 "Mode",
#                 ["Standard Study (SRS)", "Review Mistakes Only"],
#                 help="Standard: New & Due words | Mistakes: Words you got wrong before"
#             )
#         with c2:
#             sel_goal = st.selectbox("Daily Goal", [5, 10, 15, 20, 30])
#             sel_diff = st.slider("Difficulty", 1, 3, (1, 3))

#         submitted = st.form_submit_button("🚀 Start Session", use_container_width=True)

#         if submitted:
#             st.session_state.session_config = {
#                 'topic': sel_topic,
#                 'goal': sel_goal,
#                 'difficulty': sel_diff,
#                 'mode': sel_mode
#             }
#             st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
#             st.session_state.app_mode = 'quiz'
#             st.rerun()

# # --- 화면 2: 퀴즈 (Quiz) ---
# elif st.session_state.app_mode == 'quiz':
#     config = st.session_state.session_config
#     stats = st.session_state.session_stats

#     goal = config['goal']
#     current = stats['total']
#     st.progress(min(current / goal, 1.0))
#     st.caption(f"Progress: {current} / {goal} (Topic: {config['topic']})")

#     if current >= goal:
#         st.session_state.app_mode = 'summary'
#         st.rerun()

#     df = st.session_state.vocab_db

#     # -------------------------------------------------------
#     # 문제 로딩 로직
#     # -------------------------------------------------------
#     if st.session_state.current_word_id is None:
#         new_id = get_next_word()
#         if new_id is not None:
#             st.session_state.current_word_id = new_id
#             current_word = df[df['id'] == new_id].iloc[0]

#             qtype, qtext, options, correct_set, extra = build_question_for_word(current_word, df)

#             st.session_state.question_type = qtype
#             st.session_state.question_text = qtext
#             st.session_state.quiz_options = options
#             st.session_state.correct_answers = correct_set

#             # extra
#             st.session_state.example_blank_to_show = extra.get('example_blank', '')

#             # 상태 초기화
#             st.session_state.quiz_answered = False
#             st.session_state.selected_option = None

#         else:
#             st.warning("No words matching your criteria!")
#             if config['mode'] == 'Review Mistakes Only':
#                 st.info("💡 You have no recorded mistakes yet! Try 'Standard Study (SRS)'.")
#             if st.button("Back to Setup"):
#                 st.session_state.app_mode = 'setup'
#                 st.rerun()
#             st.stop()

#     # -------------------------------------------------------
#     # UI 구성
#     # -------------------------------------------------------
#     current_id = st.session_state.current_word_id
#     current_word_row = df[df['id'] == current_id].iloc[0]
#     word_text = str(current_word_row.get('word', '')).strip()

#     # 문제 텍스트
#     st.markdown(st.session_state.question_text)

#     # blank 문제면 문장 표시
#     if st.session_state.question_type == 'blank':
#         blank_sentence = getattr(st.session_state, 'example_blank_to_show', '')
#         if blank_sentence:
#             st.info(blank_sentence)

#     # 발음 듣기 (단어)
#     try:
#         sound_file = BytesIO()
#         tts = gTTS(text=word_text, lang='en')
#         tts.write_to_fp(sound_file)
#         sound_file.seek(0)
#         st.audio(sound_file, format='audio/mpeg')
#     except:
#         pass

#     st.caption(f"Part of Speech: *{current_word_row.get('pos', '')}*")

#     # [A] 답변 전
#     if not st.session_state.quiz_answered:
#         cols = st.columns(2)
#         for i, option in enumerate(st.session_state.quiz_options):
#             if cols[i % 2].button(option, key=f"btn_{i}", use_container_width=True):
#                 st.session_state.quiz_answered = True
#                 st.session_state.selected_option = option

#                 is_correct = option in st.session_state.correct_answers
#                 update_srs(current_id, is_correct)
#                 st.rerun()

#     # [B] 답변 후
#     else:
#         selected = st.session_state.selected_option
#         is_correct = selected in st.session_state.correct_answers
#         final_answer_text = list(st.session_state.correct_answers)[0] if st.session_state.correct_answers else word_text

#         if is_correct:
#             st.success(f"✅ Correct! **'{selected}'**")
#         else:
#             st.error(f"❌ Incorrect. The answer is **'{final_answer_text}'**.")

#         st.markdown("---")
#         st.markdown(f"#### 📖 Study: **{word_text}**")

#         st.info(
#             f"**Definition:** {current_word_row.get('definition','')}\n\n"
#             f"**Example:** *{current_word_row.get('example','')}*"
#         )

#         # blank 문제면 collocations도 함께 보여주면 학습효율↑
#         if st.session_state.question_type == 'blank':
#             colls = parse_list(current_word_row.get('collocations', ''))
#             if colls:
#                 st.caption("Collocations: " + ", ".join(colls))

#         if st.button("Next Question ➡️", type="primary"):
#             st.session_state.current_word_id = None
#             st.session_state.quiz_answered = False
#             st.session_state.selected_option = None
#             st.session_state.correct_answers = set()
#             st.session_state.question_type = None
#             st.session_state.question_text = ""
#             st.session_state.quiz_options = []
#             st.rerun()

# # --- 화면 3: 결과 요약 (Summary) ---
# elif st.session_state.app_mode == 'summary':
#     st.balloons()
#     st.markdown("## 🏆 Session Complete!")

#     stats = st.session_state.session_stats
#     score = int((stats['correct'] / stats['total']) * 100) if stats['total'] > 0 else 0

#     col1, col2, col3 = st.columns(3)
#     col1.metric("Total", stats['total'])
#     col2.metric("Correct 🟢", stats['correct'])
#     col3.metric("Wrong 🔴", stats['wrong'])

#     st.progress(score / 100)
#     st.caption(f"Final Score: {score}%")

#     st.divider()

#     if st.button("🏠 Back to Home", use_container_width=True):
#         st.session_state.app_mode = 'setup'
#         st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
#         st.rerun()


