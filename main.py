import streamlit as st
import pandas as pd
import datetime
import random
import json
import os  # 파일 존재 여부 확인을 위해 추가

# ---------------------------------------------------------
# 1. 데이터 세팅 (CSV 우선 로드, 없으면 JSON 로드)
# ---------------------------------------------------------
CSV_FILE = 'vocab_progress.csv'
JSON_FILE = 'vocab.json'

if 'vocab_db' not in st.session_state:
    # 1. 학습 기록 파일(CSV)이 있으면 그걸 먼저 로드
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            # CSV로 읽으면 None이 NaN(Not a Number)으로 변환되므로 처리 필요할 수 있음
            # 하지만 pandas 함수들이 NaN을 잘 처리하므로 일단 유지
        except Exception as e:
            st.error(f"Error loading saved progress: {e}")
            st.stop()

    # 2. CSV가 없으면(처음 실행이면) JSON 원본 로드
    else:
        try:
            with open(JSON_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            df = pd.DataFrame(data)

            # 초기화 컬럼 추가
            if 'box' not in df.columns:
                df['box'] = 0
            if 'next_review' not in df.columns:
                df['next_review'] = None

            # 바로 CSV로 한 번 저장 (파일 생성)
            df.to_csv(CSV_FILE, index=False)

        except FileNotFoundError:
            st.error(f"❌ '{JSON_FILE}' file not found. Please make sure the file exists.")
            st.stop()

    st.session_state.vocab_db = df

# 현재 학습 중인 단어 상태를 저장할 변수 초기화
if 'current_word_id' not in st.session_state:
    st.session_state.current_word_id = None
if 'quiz_options' not in st.session_state:
    st.session_state.quiz_options = []
if 'show_answer' not in st.session_state:
    st.session_state.show_answer = False


# ---------------------------------------------------------
# 2. 로직 함수 (SRS 및 CSV 저장)
# ---------------------------------------------------------
def get_next_word(df, difficulty, topic):
    """조건에 맞는 단어 중 하나를 뽑아 세션 상태에 고정"""
    today_str = str(datetime.date.today())

    # 필터링: (레벨 조건) AND (주제 조건)
    mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])
    if topic != "All":
        mask = mask & (df['topic'] == topic)

    # 날짜 필터: next_review가 비어있거나(NaN/None), 오늘보다 작거나 같은 경우
    # pd.isna()를 사용하여 None과 NaN을 모두 처리
    date_mask = (pd.isna(df['next_review'])) | (df['next_review'] <= today_str)

    candidates = df[mask & date_mask]

    if len(candidates) == 0:
        return None

    # 랜덤 선택하여 ID 반환
    selected = candidates.sample(1).iloc[0]
    return selected['id']


def update_srs(word_id, is_correct):
    """DB 업데이트, CSV 저장, UI 상태 초기화"""
    df = st.session_state.vocab_db
    idx = df[df['id'] == word_id].index[0]

    current_box = df.at[idx, 'box']

    if is_correct:
        new_box = min(current_box + 1, 5)
        days_to_add = int(2 ** new_box)
    else:
        new_box = 0
        days_to_add = 0

    next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)

    # 1. 메모리(Session State) 업데이트
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)

    # 2. 파일(CSV) 저장 (영구 저장) --- [핵심 추가 부분] ---
    st.session_state.vocab_db.to_csv(CSV_FILE, index=False)

    # 3. UI 상태 초기화 (다음 단어 준비)
    st.session_state.current_word_id = None
    st.session_state.quiz_options = []
    st.session_state.show_answer = False


# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 TOEFL Voca Master (Auto-Save)")

# 사이드바 설정
with st.sidebar:
    st.header("Settings")
    # JSON 파일에 있는 실제 토픽들을 가져오면 더 좋습니다 (지금은 하드코딩 유지)
    topic = st.selectbox("Topic",
                         ["All", "Social Science", "Science", "Linguistics", "Sociology", "Economics", "Medicine",
                          "Art", "Biology", "History", "Geology", "Chemistry", "Ecology", "Psychology", "Business",
                          "Law", "Physics", "Philosophy", "Education", "Technology", "General"])
    difficulty = st.slider("Level Difficulty", 1, 3, (1, 3))

    # 남은 단어 수 계산
    today = str(datetime.date.today())
    df = st.session_state.vocab_db
    # 복습 대상: 날짜가 지났거나(<= today), 아직 날짜가 없는(isna) 단어
    rem_count = len(df[(pd.isna(df['next_review'])) | (df['next_review'] <= today)])
    st.write(f"Words to review today: {rem_count}")

    if st.button("Reset All Progress (Warning!)"):
        if os.path.exists(CSV_FILE):
            os.remove(CSV_FILE)
            st.cache_data.clear()
            st.session_state.clear()
            st.rerun()

# 메인 학습 로직
if st.session_state.current_word_id is None:
    new_id = get_next_word(st.session_state.vocab_db, difficulty, topic)
    if new_id is not None:
        st.session_state.current_word_id = new_id

        # 퀴즈 보기 생성
        current_word = st.session_state.vocab_db[st.session_state.vocab_db['id'] == new_id].iloc[0]
        # JSON 문자열 리스트 처리 (CSV 로드 시 문자열로 변환될 수 있음)
        # 안전하게 리스트인지 확인
        synonyms = current_word['synonyms']
        if isinstance(synonyms, str):
            # CSV 저장 후 다시 읽으면 "['a', 'b']" 같은 문자열이 될 수 있음. eval로 리스트 복원
            import ast

            synonyms = ast.literal_eval(synonyms)

        options = synonyms[:]

        # 오답 풀 생성
        wrong_pool = []
        other_words = st.session_state.vocab_db[st.session_state.vocab_db['id'] != new_id]

        for syn_list in other_words['synonyms']:
            if isinstance(syn_list, str):
                import ast

                syn_list = ast.literal_eval(syn_list)
            wrong_pool.extend(syn_list)

        if len(wrong_pool) >= 3:
            wrong_options = random.sample(wrong_pool, 2)
            options = [options[0]] + wrong_options
            random.shuffle(options)
        st.session_state.quiz_options = options

    else:
        st.success("🎉 You've finished all words for today!")
        st.write("Come back tomorrow for review.")
        st.stop()

# 현재 단어 데이터 가져오기
word_id = st.session_state.current_word_id
row = st.session_state.vocab_db[st.session_state.vocab_db['id'] == word_id].iloc[0]

# CSV에서 읽어온 리스트 문자열 처리 (synonyms)
synonyms_display = row['synonyms']
if isinstance(synonyms_display, str):
    import ast

    synonyms_display = ast.literal_eval(synonyms_display)

# UI 렌더링
st.markdown(f"""
<div style="padding: 20px; border-radius: 10px; background-color: #f0f2f6; text-align: center; margin-bottom: 20px;">
    <p style="color: grey; font-size: 0.9em;">{row['topic']} | Level {row['level']}</p>
    <h1 style="color: #1f77b4; font-size: 3em; margin: 0;">{row['word']}</h1>
</div>
""", unsafe_allow_html=True)

tab1, tab2 = st.tabs(["📖 Flashcard (Definition)", "🧩 Synonym Quiz"])

# --- TAB 1: 뜻풀이 (Flashcard) ---
with tab1:
    st.subheader("Do you know this word?")

    if not st.session_state.show_answer:
        if st.button("🔍 Show Definition & Example", use_container_width=True):
            st.session_state.show_answer = True
            st.rerun()
    else:
        st.markdown(f"**Definition:** {row['definition']}")
        st.markdown(f"**Example:** *\"{row['example']}\"*")
        st.markdown(f"**Synonyms:** {', '.join(synonyms_display)}")

        st.divider()
        st.caption("Rate your knowledge to proceed:")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("❌ No (Review Soon)", use_container_width=True):
                update_srs(word_id, False)
                st.rerun()
        with col2:
            if st.button("✅ Yes (Easy)", use_container_width=True):
                update_srs(word_id, True)
                st.rerun()

# --- TAB 2: 퀴즈 (Quiz) ---
with tab2:
    st.write(f"Which word is a synonym for **'{row['word']}'**?")

    if not st.session_state.quiz_options:
        st.warning("Not enough data to generate quiz.")
    else:
        with st.form("quiz_form"):
            choice = st.radio("Choose the best answer:", st.session_state.quiz_options)
            submitted = st.form_submit_button("Submit Answer")

            if submitted:
                if choice in synonyms_display:
                    st.success(f"Correct! '{choice}' is a synonym.")
                    st.session_state.last_quiz_result = True
                else:
                    st.error(f"Wrong. The answer was one of: {', '.join(synonyms_display)}")
                    st.session_state.last_quiz_result = False

        if 'last_quiz_result' in st.session_state:
            if st.button("Next Word ➡️"):
                result = st.session_state.last_quiz_result
                del st.session_state['last_quiz_result']
                update_srs(word_id, result)
                st.rerun()