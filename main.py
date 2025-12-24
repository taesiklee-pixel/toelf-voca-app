import streamlit as st
import pandas as pd
import datetime
import random
import ast
import re
from io import BytesIO
from gtts import gTTS
from streamlit_gsheets import GSheetsConnection

# =========================================================
# 0. App Config
# =========================================================
st.set_page_config(page_title="NicholaSOOBIN TOEFL Voca", page_icon="🎓", layout="centered")

# ---------------------------------------------------------
# 1. Google Sheet Connection + Data Load
# ---------------------------------------------------------
conn = st.connection("gsheets", type=GSheetsConnection)

MAIN_SHEET = "Sheet1"
QC_SHEET = "QC"   # QC 결과 저장 워크시트 (없으면 생성 시도)

REQUIRED_COLS = [
    "id", "word", "definition", "example", "synonyms", "topic", "level", "box",
    "next_review", "pos", "mistake_count",
    "example_blank", "collocations", "confusables"
]

def ensure_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """Ensure required columns exist. Returns (df, changed_flag)."""
    changed = False
    for col in REQUIRED_COLS:
        if col not in df.columns:
            df[col] = "" if col in ["example_blank", "collocations", "confusables"] else 0
            changed = True

    # 타입 정리
    # NOTE: id는 시트에서 숫자/문자 섞일 수 있어 안전하게 숫자화 시도 후 실패는 그대로 둠.
    for col in ["mistake_count", "box"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    df["next_review"] = df["next_review"].astype(str).replace(["nan", "None"], "0000-00-00")

    # level도 숫자화
    df["level"] = pd.to_numeric(df["level"], errors="coerce").fillna(1).astype(int)

    return df, changed

def load_data():
    try:
        df = conn.read(worksheet=MAIN_SHEET, ttl=0)
        df.columns = df.columns.str.lower()

        # 중복 단어 제거 (유지: 첫 등장만)
        if "word" in df.columns:
            df = df.drop_duplicates(subset=["word"], keep="first")

        df, changed = ensure_columns(df)

        if changed:
            conn.update(worksheet=MAIN_SHEET, data=df)
            st.toast("Updated Google Sheet structure (added missing columns).")

        if df.empty:
            st.warning("Google Sheet is empty.")
            st.stop()

        return df

    except Exception as e:
        st.error(f"Google Sheet Connection Error: {e}")
        st.stop()

if "vocab_db" not in st.session_state:
    st.session_state.vocab_db = load_data()

df = st.session_state.vocab_db

# =========================================================
# 2. Session State
# =========================================================
if "app_mode" not in st.session_state:
    st.session_state.app_mode = "setup"  # setup | quiz | summary
if "session_config" not in st.session_state:
    st.session_state.session_config = {}
if "session_stats" not in st.session_state:
    st.session_state.session_stats = {"correct": 0, "wrong": 0, "total": 0}

if "current_word_id" not in st.session_state:
    st.session_state.current_word_id = None
if "quiz_options" not in st.session_state:
    st.session_state.quiz_options = []
if "quiz_answered" not in st.session_state:
    st.session_state.quiz_answered = False
if "selected_option" not in st.session_state:
    st.session_state.selected_option = None

if "question_type" not in st.session_state:
    st.session_state.question_type = None  # synonym | blank
if "correct_answers" not in st.session_state:
    st.session_state.correct_answers = set()
if "question_text" not in st.session_state:
    st.session_state.question_text = ""
if "example_blank_to_show" not in st.session_state:
    st.session_state.example_blank_to_show = ""

# =========================================================
# 3. Utility
# =========================================================
def parse_list(x):
    """Parse list-like strings safely into list[str]."""
    if isinstance(x, list):
        return [str(v).strip() for v in x if str(v).strip() != ""]
    if isinstance(x, str) and x.strip() != "":
        s = x.strip()
        try:
            v = ast.literal_eval(s)
            if isinstance(v, list):
                return [str(i).strip() for i in v if str(i).strip() != ""]
            return [str(v).strip()]
        except:
            # 그냥 문자열이면 단일 항목
            return [s]
    return []

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def safe_int(x, default=0):
    try:
        return int(float(x))
    except:
        return default

def today_str():
    return str(datetime.date.today())

# =========================================================
# 4. Core Engine (A 방식 핵심): generate / grade
# =========================================================
def get_candidate_pool(df_all: pd.DataFrame, target_id: int) -> pd.DataFrame:
    """Return df without the target row; add pos_norm."""
    pool = df_all.copy()
    pool["pos_norm"] = pool["pos"].fillna("").astype(str).str.strip().str.lower()
    return pool[pool["id"].apply(lambda v: safe_int(v, -1)) != target_id]

def generate_question(word_row: pd.Series, df_all: pd.DataFrame, rng: random.Random) -> dict:
    """
    Pure-ish generator (depends only on inputs + rng).
    Returns a dict:
      {
        "question_type": "synonym"|"blank",
        "word_id": int,
        "word": str,
        "prompt": str (markdown-ready),
        "stem": str (blank sentence or "")
        "options": list[str],
        "correct_answers": set[str],
        "meta": {...}
      }
    """
    wid = safe_int(word_row.get("id"), -1)
    word = str(word_row.get("word", "")).strip()
    pos = str(word_row.get("pos", "")).strip()
    topic = str(word_row.get("topic", "")).strip()

    example_blank = str(word_row.get("example_blank", "")).strip()
    can_blank = (example_blank != "" and norm(example_blank) not in ["nan", "none"])

    # 50:50 섞기 (가능하면 blank도 출제)
    qtype = rng.choice(["synonym", "blank"])
    if qtype == "blank" and not can_blank:
        qtype = "synonym"

    pool = get_candidate_pool(df_all, wid)

    # ---------------------------
    # Synonym MCQ
    # ---------------------------
    if qtype == "synonym":
        synonyms = parse_list(word_row.get("synonyms", ""))
        synonyms = [s for s in synonyms if s and norm(s) not in ["nan", "none"]]

        # 방어: synonyms 없으면 blank로 대체, 그것도 안 되면 단어 자체를 정답 처리
        if not synonyms and can_blank:
            qtype = "blank"
        elif not synonyms:
            synonyms = [word]

        if qtype == "synonym":
            correct_set = set(synonyms)
            correct_option = rng.choice(list(correct_set))
            options = [correct_option]

            target_pos = norm(pos)
            pool2 = pool
            if target_pos:
                pool_pos = pool2[pool2["pos_norm"] == target_pos]
                if not pool_pos.empty:
                    pool2 = pool_pos

            wrong_pool = []
            for syn_list in pool2["synonyms"]:
                for w in parse_list(syn_list):
                    if w and w not in correct_set:
                        wrong_pool.append(w)

            wrong_pool = list(set([w for w in wrong_pool if w and w not in correct_set]))
            rng.shuffle(wrong_pool)

            needed = 3
            wrong_options = wrong_pool[:needed]
            while len(wrong_options) < needed:
                wrong_options.append(f"Option {chr(ord('A') + len(wrong_options))}")

            options += wrong_options
            rng.shuffle(options)

            return {
                "question_type": "synonym",
                "word_id": wid,
                "word": word,
                "prompt": f"### What is a synonym for: **{word}**?",
                "stem": "",
                "options": options,
                "correct_answers": correct_set,
                "meta": {
                    "pos": pos,
                    "topic": topic,
                    "synonyms": synonyms,
                    "example_blank": example_blank,
                }
            }

    # ---------------------------
    # Blank MCQ
    # ---------------------------
    correct_set = {word}
    prompt = "### Fill in the blank with the best word:"
    stem = example_blank

    confusables = parse_list(word_row.get("confusables", ""))
    confusables = [c for c in confusables if c and c != word]

    options = [word]
    for c in confusables:
        if len(options) >= 4:
            break
        if c not in options:
            options.append(c)

    # 부족하면 같은 topic/pos 단어로 채우기
    if len(options) < 4:
        pool2 = pool
        if topic:
            pool_topic = pool2[pool2["topic"] == topic]
            if not pool_topic.empty:
                pool2 = pool_topic

        target_pos = norm(pos)
        if target_pos:
            pool_pos = pool2[pool2["pos_norm"] == target_pos]
            if not pool_pos.empty:
                pool2 = pool_pos

        filler = pool2["word"].dropna().astype(str).tolist()
        filler = [w for w in list(set(filler)) if w and w != word]
        rng.shuffle(filler)
        for w in filler:
            if len(options) >= 4:
                break
            if w not in options:
                options.append(w)

    while len(options) < 4:
        options.append(f"Option {len(options)}")

    rng.shuffle(options)

    return {
        "question_type": "blank",
        "word_id": wid,
        "word": word,
        "prompt": prompt,
        "stem": stem,
        "options": options,
        "correct_answers": correct_set,
        "meta": {
            "pos": pos,
            "topic": topic,
            "confusables": confusables,
            "example_blank": example_blank,
            "collocations": parse_list(word_row.get("collocations", "")),
        }
    }

def grade_question(q: dict, choice: str) -> bool:
    return choice in q.get("correct_answers", set())

# =========================================================
# 5. SRS (unchanged logic)
# =========================================================
def get_next_word_id():
    df0 = st.session_state.vocab_db
    config = st.session_state.session_config

    difficulty = config.get("difficulty", (1, 3))
    mask = (df0["level"] >= difficulty[0]) & (df0["level"] <= difficulty[1])

    topic = config.get("topic", "All")
    if topic != "All":
        mask = mask & (df0["topic"] == topic)

    mode = config.get("mode", "Standard Study (SRS)")
    ts = today_str()

    if mode == "Review Mistakes Only":
        logic_mask = (df0["box"] == 0) & (df0["mistake_count"] > 0)
        if df0[mask & logic_mask].empty:
            st.toast("No historical mistakes found! (Box 0 & Count > 0)")
    else:
        logic_mask = df0["next_review"] <= ts

    candidates = df0[mask & logic_mask]
    if candidates.empty:
        return None

    # 랜덤 추출
    pick = candidates.sample(1).iloc[0]
    return safe_int(pick["id"], None)

def update_srs(word_id: int, is_correct: bool):
    df0 = st.session_state.vocab_db
    idx_list = df0[df0["id"].apply(lambda v: safe_int(v, -1)) == word_id].index.tolist()
    if not idx_list:
        return
    idx = idx_list[0]

    current_box = int(df0.at[idx, "box"])
    current_mistakes = int(df0.at[idx, "mistake_count"])

    if is_correct:
        st.session_state.session_stats["correct"] += 1
        new_box = min(current_box + 1, 5)
        days_to_add = int(2 ** new_box)
        new_mistakes = current_mistakes
    else:
        st.session_state.session_stats["wrong"] += 1
        new_box = 0
        days_to_add = 0
        new_mistakes = current_mistakes + 1

    st.session_state.session_stats["total"] += 1
    next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)

    st.session_state.vocab_db.at[idx, "box"] = new_box
    st.session_state.vocab_db.at[idx, "next_review"] = str(next_date)
    st.session_state.vocab_db.at[idx, "mistake_count"] = new_mistakes

    try:
        conn.update(worksheet=MAIN_SHEET, data=st.session_state.vocab_db)
    except Exception as e:
        st.error(f"Save failed: {e}")

# =========================================================
# 6. QC: rule-based 검사 + (선택) LLM 훅
# =========================================================
def qc_rules(q: dict, word_row: pd.Series, df_all: pd.DataFrame) -> list[str]:
    """
    Return list of issues (strings).
    규칙 기반으로 '이상한 문제/답'을 최대한 자동 검출.
    """
    issues = []

    qtype = q["question_type"]
    word = q["word"]
    options = q["options"]
    correct = q["correct_answers"]

    # 공통: 보기 중복
    if len(options) != len(set(options)):
        issues.append("Duplicate options in MCQ.")

    # 공통: 정답이 보기 안에 없는 경우
    if not any(c in options for c in correct):
        issues.append("Correct answer not present in options.")

    # 공통: placeholder 옵션(Option A/B 등)이 섞인 경우 (데이터 부족 신호)
    if any(norm(o).startswith("option ") for o in options):
        issues.append("Placeholder options used (insufficient distractor pool).")

    # 공통: 보기 길이가 과도하게 길거나(예: 문장), 공백만 있는 경우
    for o in options:
        if len(o.strip()) == 0:
            issues.append("Empty option found.")
        if len(o) > 40:
            issues.append("Option seems too long (maybe not a word/phrase).")

    # synonym 전용 규칙
    if qtype == "synonym":
        synonyms = parse_list(word_row.get("synonyms", ""))
        if len(synonyms) == 0:
            issues.append("No synonyms in DB but synonym question generated.")
        # 정답 후보가 너무 많으면(예: 6개 이상) 애매해질 가능성 -> 경고
        if len(set(synonyms)) >= 6:
            issues.append("Many synonyms listed; synonym MCQ may be ambiguous.")

        # 오답 중 정답과 완전히 동일/부분 포함 관계(간단 휴리스틱)
        for o in options:
            if o in correct:
                continue
            # 예: "economic system" vs "system" 같은 형태
            if any(norm(o) in norm(c) or norm(c) in norm(o) for c in correct):
                issues.append("Distractor may overlap heavily with a correct synonym (possible ambiguity).")
                break

    # blank 전용 규칙
    if qtype == "blank":
        stem = q.get("stem", "")
        if norm(stem) in ["", "nan", "none"]:
            issues.append("Blank question generated but example_blank is missing.")
        # blank 문장에 빈칸 표식이 없으면 경고
        if "____" not in stem:
            issues.append("example_blank has no '____' placeholder.")
        # confusable이 정답과 너무 가까운 경우(동일/부분포함)
        conf = parse_list(word_row.get("confusables", ""))
        if len(conf) == 0:
            issues.append("Blank MCQ has no confusables (distractors may be weak).")
        for c in conf:
            if norm(c) == norm(word):
                issues.append("Confusables contains the target word itself.")
                break

    return list(dict.fromkeys(issues))  # 중복 제거, 순서 유지

def choose_as_user(q: dict, rng: random.Random) -> str:
    """
    '가상 유저' 선택 정책(LLM 없이도 돌릴 수 있는 baseline).
    - synonym: 정답 중 하나가 options에 있으면 그걸 고르는 '치팅' 대신,
              랜덤 유저/휴리스틱 유저 2종이 있는데, 여기서는 기본 랜덤.
    - blank: stem에 맞춰 고르는 건 어려우니 기본 랜덤.
    -> 실제 품질검사는 정답률보다 '이상 탐지'가 목적이므로 랜덤도 충분히 의미 있음.
    """
    return rng.choice(q["options"])

# ---- (선택) LLM QC 훅 ----
def llm_qc_review_stub(q: dict) -> dict:
    """
    여기에 OpenAI/Gemini API를 붙이면, LLM이 아래를 반환하도록 만들면 됩니다:
      {
        "is_weird": bool,
        "reasons": [str, ...],
        "suggested_fix": str
      }

    현재는 "stub"이라 항상 정상으로 반환.
    """
    return {"is_weird": False, "reasons": [], "suggested_fix": ""}

def ensure_qc_sheet_exists():
    """
    streamlit_gsheets는 워크시트 생성 기능이 환경마다 다를 수 있어,
    실패하면 사용자에게 안내만 하고, 가능하면 업데이트로 생성되도록 시도.
    """
    try:
        # 읽어보기 시도
        _ = conn.read(worksheet=QC_SHEET, ttl=0)
        return True
    except Exception:
        # 생성 시도: 빈 DF를 update하면 생성되는 환경이 많음
        try:
            empty = pd.DataFrame(columns=[
                "timestamp", "seed", "word_id", "word", "qtype", "prompt", "stem",
                "options", "correct_answers",
                "rule_issues", "llm_is_weird", "llm_reasons", "llm_suggested_fix"
            ])
            conn.update(worksheet=QC_SHEET, data=empty)
            return True
        except Exception:
            return False

def append_qc_rows(rows: list[dict]):
    """Append rows to QC worksheet (simple 방식: read -> concat -> update)."""
    ok = ensure_qc_sheet_exists()
    if not ok:
        st.error(
            "QC 워크시트 생성/접근에 실패했습니다. "
            "Google Sheet에 'QC' 시트를 수동으로 만들고 다시 시도해 주세요."
        )
        return

    try:
        existing = conn.read(worksheet=QC_SHEET, ttl=0)
        existing.columns = existing.columns.str.lower()
        new_df = pd.DataFrame(rows)
        new_df.columns = new_df.columns.str.lower()

        combined = pd.concat([existing, new_df], ignore_index=True)
        conn.update(worksheet=QC_SHEET, data=combined)
    except Exception as e:
        st.error(f"Failed to write QC results: {e}")

def run_qc_simulation(
    df_all: pd.DataFrame,
    n_questions: int = 200,
    seed: int = 42,
    topic: str = "All",
    difficulty: tuple[int, int] = (1, 3),
    include_llm: bool = False,
):
    """
    Generate N questions, run rule checks (+ optional LLM checks),
    save issues to QC sheet.
    """
    rng = random.Random(seed)

    # 필터
    df0 = df_all.copy()
    mask = (df0["level"] >= difficulty[0]) & (df0["level"] <= difficulty[1])
    if topic != "All":
        mask = mask & (df0["topic"] == topic)
    df0 = df0[mask].copy()

    if df0.empty:
        st.warning("No data matches QC filters.")
        return

    rows = []
    weird_count = 0

    # 샘플링: 데이터 수보다 N이 크면 반복 샘플링
    for i in range(n_questions):
        row = df0.sample(1, random_state=rng.randint(0, 10**9)).iloc[0]
        q = generate_question(row, df_all, rng)

        # baseline user choice (랜덤)
        user_choice = choose_as_user(q, rng)
        _ = grade_question(q, user_choice)  # 점수 자체는 지금은 사용하지 않음(원하면 기록 가능)

        # rule-based issues
        issues = qc_rules(q, row, df_all)

        # optional LLM review
        llm = {"is_weird": False, "reasons": [], "suggested_fix": ""}
        if include_llm:
            llm = llm_qc_review_stub(q)  # <- 여기를 실제 API 호출로 교체

        is_weird = (len(issues) > 0) or llm.get("is_weird", False)
        if is_weird:
            weird_count += 1

        rows.append({
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "seed": seed,
            "word_id": q["word_id"],
            "word": q["word"],
            "qtype": q["question_type"],
            "prompt": q["prompt"],
            "stem": q["stem"],
            "options": str(q["options"]),
            "correct_answers": str(sorted(list(q["correct_answers"]))),
            "rule_issues": "; ".join(issues),
            "llm_is_weird": bool(llm.get("is_weird", False)),
            "llm_reasons": "; ".join(llm.get("reasons", [])),
            "llm_suggested_fix": llm.get("suggested_fix", ""),
        })

    append_qc_rows(rows)
    st.success(f"QC simulation complete: {n_questions} questions, flagged {weird_count} as potentially problematic.")

# =========================================================
# 7. UI
# =========================================================
st.title("🎓 NicholaSOOBIN TOEFL Voca")

with st.sidebar:
    st.header("Data Management")

    if st.button("🔄 Reload Sheet (no cache)"):
        st.session_state.vocab_db = load_data()
        st.toast("Reloaded latest data.")
        st.rerun()

    if st.button("Reset All Progress"):
        df_reset = st.session_state.vocab_db.copy()
        df_reset["box"] = 0
        df_reset["next_review"] = "0000-00-00"
        df_reset["mistake_count"] = 0
        conn.update(worksheet=MAIN_SHEET, data=df_reset)
        st.toast("All progress has been reset.")
        st.session_state.clear()
        st.rerun()

    st.divider()
    st.header("QC (A-mode)")

    qc_topic = st.selectbox("QC Topic", ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"], index=0)
    qc_diff = st.slider("QC Difficulty", 1, 3, (1, 3))
    qc_n = st.number_input("Number of QC questions", min_value=20, max_value=2000, value=200, step=20)
    qc_seed = st.number_input("Seed", min_value=0, max_value=10**9, value=42, step=1)

    include_llm = st.checkbox("Include LLM QC (requires API hookup)", value=False)

    if st.button("🧪 Run QC Simulation", use_container_width=True):
        run_qc_simulation(
            df_all=st.session_state.vocab_db,
            n_questions=int(qc_n),
            seed=int(qc_seed),
            topic=qc_topic,
            difficulty=tuple(qc_diff),
            include_llm=include_llm
        )

# ---------------------------------------------------------
# Setup Screen
# ---------------------------------------------------------
if st.session_state.app_mode == "setup":
    st.markdown("### ⚙️ Study Setup")

    with st.form("setup_form"):
        c1, c2 = st.columns(2)
        with c1:
            topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
            sel_topic = st.selectbox("Topic", topic_list)
            sel_mode = st.radio(
                "Mode",
                ["Standard Study (SRS)", "Review Mistakes Only"],
                help="Standard: New & Due words | Mistakes: Words you got wrong before",
            )
        with c2:
            sel_goal = st.selectbox("Daily Goal", [5, 10, 15, 20, 30])
            sel_diff = st.slider("Difficulty", 1, 3, (1, 3))

        submitted = st.form_submit_button("🚀 Start Session", use_container_width=True)

        if submitted:
            st.session_state.session_config = {
                "topic": sel_topic,
                "goal": sel_goal,
                "difficulty": sel_diff,
                "mode": sel_mode,
            }
            st.session_state.session_stats = {"correct": 0, "wrong": 0, "total": 0}
            st.session_state.app_mode = "quiz"
            st.rerun()

# ---------------------------------------------------------
# Quiz Screen
# ---------------------------------------------------------
elif st.session_state.app_mode == "quiz":
    config = st.session_state.session_config
    stats = st.session_state.session_stats

    goal = config["goal"]
    current = stats["total"]
    st.progress(min(current / goal, 1.0))
    st.caption(f"Progress: {current} / {goal} (Topic: {config['topic']})")

    if current >= goal:
        st.session_state.app_mode = "summary"
        st.rerun()

    df0 = st.session_state.vocab_db

    # 문제 로딩
    if st.session_state.current_word_id is None:
        new_id = get_next_word_id()
        if new_id is not None:
            st.session_state.current_word_id = new_id
            row = df0[df0["id"].apply(lambda v: safe_int(v, -1)) == new_id].iloc[0]

            # 학습용은 여기서도 엔진 함수 사용(= A 방식으로 분리 완료)
            rng = random.Random()  # 학습 모드는 시드 고정 안 함
            q = generate_question(row, df0, rng)

            st.session_state.question_type = q["question_type"]
            st.session_state.question_text = q["prompt"]
            st.session_state.quiz_options = q["options"]
            st.session_state.correct_answers = q["correct_answers"]
            st.session_state.example_blank_to_show = q.get("stem", "")

            st.session_state.quiz_answered = False
            st.session_state.selected_option = None
        else:
            st.warning("No words matching your criteria!")
            if config["mode"] == "Review Mistakes Only":
                st.info("💡 You have no recorded mistakes yet! Try 'Standard Study (SRS)'.")
            if st.button("Back to Setup"):
                st.session_state.app_mode = "setup"
                st.rerun()
            st.stop()

    # UI 출력
    current_id = st.session_state.current_word_id
    current_row = df0[df0["id"].apply(lambda v: safe_int(v, -1)) == current_id].iloc[0]
    word_text = str(current_row.get("word", "")).strip()

    st.markdown(st.session_state.question_text)

    if st.session_state.question_type == "blank":
        if st.session_state.example_blank_to_show:
            st.info(st.session_state.example_blank_to_show)

    # 발음
    try:
        sound_file = BytesIO()
        tts = gTTS(text=word_text, lang="en")
        tts.write_to_fp(sound_file)
        sound_file.seek(0)
        st.audio(sound_file, format="audio/mpeg")
    except:
        pass

    st.caption(f"Part of Speech: *{current_row.get('pos','')}*")

    if not st.session_state.quiz_answered:
        cols = st.columns(2)
        for i, option in enumerate(st.session_state.quiz_options):
            if cols[i % 2].button(option, key=f"btn_{i}", use_container_width=True):
                st.session_state.quiz_answered = True
                st.session_state.selected_option = option

                is_correct = grade_question(
                    {"correct_answers": st.session_state.correct_answers},
                    option
                )
                update_srs(current_id, is_correct)
                st.rerun()

    else:
        selected = st.session_state.selected_option
        is_correct = selected in st.session_state.correct_answers
        final_answer_text = next(iter(st.session_state.correct_answers), word_text)

        if is_correct:
            st.success(f"✅ Correct! **'{selected}'**")
        else:
            st.error(f"❌ Incorrect. The answer is **'{final_answer_text}'**.")

        st.markdown("---")
        st.markdown(f"#### 📖 Study: **{word_text}**")
        st.info(
            f"**Definition:** {current_row.get('definition','')}\n\n"
            f"**Example:** *{current_row.get('example','')}*"
        )

        if st.session_state.question_type == "blank":
            colls = parse_list(current_row.get("collocations", ""))
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

# ---------------------------------------------------------
# Summary Screen
# ---------------------------------------------------------
elif st.session_state.app_mode == "summary":
    st.balloons()
    st.markdown("## 🏆 Session Complete!")

    stats = st.session_state.session_stats
    score = int((stats["correct"] / stats["total"]) * 100) if stats["total"] > 0 else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("Total", stats["total"])
    col2.metric("Correct 🟢", stats["correct"])
    col3.metric("Wrong 🔴", stats["wrong"])

    st.progress(score / 100)
    st.caption(f"Final Score: {score}%")

    st.divider()

    if st.button("🏠 Back to Home", use_container_width=True):
        st.session_state.app_mode = "setup"
        st.session_state.session_stats = {"correct": 0, "wrong": 0, "total": 0}
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


