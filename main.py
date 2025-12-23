import streamlit as st
import pandas as pd
import datetime
import random
import json
import os 
import ast # CSV 리스트 파싱을 위해 명시적 임포트

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
            # 날짜 컬럼을 문자열로 확실하게 변환 (에러 방지 핵심)
            df['next_review'] = df['next_review'].astype(str)
            # 'nan'이나 'None' 문자열을 실제 None 값으로 치환
            df['next_review'] = df['next_review'].replace(['nan', 'None'], None)
        except Exception as e:
            st.error(f"Error loading saved progress: {e}")
            st.stop()
            
    # 2. CSV가 없으면(처음 실행이면) JSON 원본 로드
    else:
        try:
            with open(JSON_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            df = pd.DataFrame(data)
            
            if 'box' not in df.columns:
                df['box'] = 0
            if 'next_review' not in df.columns:
                df['next_review'] = None # 초기값은 None
                
            # 바로 CSV로 한 번 저장
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
    
    # [수정된 부분] 날짜 필터: 에러 방지를 위해 fillna 사용
    # None(처음 보는 단어)은 '0000-00-00'으로 치환하여 오늘보다 작게 만듦 -> 학습 대상 포함
    date_check_col = df['next_review'].fillna('0000-00-00')
    date_mask = date_check_col <= today_str
    
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
    
    # 1. 메모리 업데이트
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    
    # 2. 파일 저장
    st.session_state.vocab_db.to_csv(CSV_FILE, index=False)
    
    # 3. UI 상태 초기화
    st.session_state.current_word_id = None
    st.session_state.quiz_options = []
    st.session_state.show_answer = False

# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 TOEFL Voca Master")

# 사이드바 설정
with st.sidebar:
    st.header("Settings")
    topic = st.selectbox("Topic", ["All", "Social Science", "Science", "Linguistics", "Sociology", "Economics", "Medicine", "Art", "Biology", "History", "Geology", "Chemistry", "Ecology", "Psychology", "Business", "Law", "Physics", "Philosophy", "Education", "Technology", "General"])
    difficulty = st.slider("Level Difficulty", 1, 3, (1, 3))
    
    # [수정된 부분] 남은 단어 수 계산 (에러 났던 곳)
    today = str(datetime.date.today())
    df = st.session_state.vocab_db
    
    # 에러 방지: NaN 값을 '0000-00-00'으로 채워서 비교 (문자열 vs 문자열 비교로 통일)
    rem_count = len(df[df['next_review'].fillna('0000-00-00') <= today])
    st.write(f"Words to review today: {rem_count}")
    
    if st.button("Reset Progress"):
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
        synonyms = current_word['synonyms']
        if isinstance(synonyms, str):
            synonyms = ast.literal_eval(synonyms)
            
        options = synonyms[:] 
        
        # 오답 풀 생성
        wrong_pool = []
        other_words = st.session_state.vocab_db[st.session_state.vocab_db['id'] != new_id]
        
        for syn_list in other_words['synonyms']:
            if isinstance(syn_list, str):
                try:
                    syn_list = ast.literal_eval(syn_list)
                except:
                    continue # 파싱 에러나면 건너뜀
            if isinstance(syn_list, list):
                wrong_pool.extend(syn_list)
        
        if len(wrong_pool) >= 3:
            wrong_options = random.sample(wrong_pool, 2)
            options = [options[0]] + wrong_options
            random.shuffle(options)
        else:
            # 오답 데이터가 부족할 경우를 대비한 안전장치
            options = options + ["Similar Word A", "Similar Word B"]
            options = options[:3]
            
        st.session_state.quiz_options = options 

    else:
        st.success("🎉 You've finished all words for today!")
        st.write("Come back tomorrow for review.")
        st.stop()

# 현재 단어 데이터 가져오기
word_id = st.session_state.current_word_id
row = st.session_state.vocab_db[st.session_state.vocab_db['id'] == word_id].iloc[0]

# UI 렌더링
st.markdown(f"""
<div style="padding: 20px; border-radius: 10px; background-color: #f0f2f6; text-align: center; margin-bottom: 20px;">
    <p style="color: grey; font-size: 0.9em;">{row['topic']} | Level {row['level']}</p>
    <h1 style="color: #1f77b4; font-size: 3em; margin: 0;">{row['word']}</h1>
</div>
""", unsafe_allow_html=True)

tab1, tab2 = st.tabs(["📖 Flashcard", "🧩 Synonym Quiz"])

# --- TAB 1: 뜻풀이 (Flashcard) ---
with tab1:
    st.subheader("Do you know this word?")
    
    # 동의어 디스플레이용 처리
    synonyms_display = row['synonyms']
    if isinstance(synonyms_display, str):
        try:
            synonyms_display = ast.literal_eval(synonyms_display)
        except:
            synonyms_display = [synonyms_display]

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
    
    # 동의어 정답 확인용 처리
    synonyms_check = row['synonyms']
    if isinstance(synonyms_check, str):
        try:
            synonyms_check = ast.literal_eval(synonyms_check)
        except:
            synonyms_check = [synonyms_check]

    if not st.session_state.quiz_options:
        st.warning("Not enough data to generate quiz.")
    else:
        with st.form("quiz_form"):
            choice = st.radio("Choose the best answer:", st.session_state.quiz_options)
            submitted = st.form_submit_button("Submit Answer")
            
            if submitted:
                if choice in synonyms_check:
                    st.success(f"Correct! '{choice}' is a synonym.")
                    st.session_state.last_quiz_result = True
                else:
                    st.error(f"Wrong. The answer was one of: {', '.join(synonyms_check)}")
                    st.session_state.last_quiz_result = False

        if 'last_quiz_result' in st.session_state:
            if st.button("Next Word ➡️"):
                result = st.session_state.last_quiz_result
                del st.session_state['last_quiz_result'] 
                update_srs(word_id, result) 
                st.rerun()
