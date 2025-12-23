import streamlit as st
import pandas as pd
import datetime
import random
import json
import ast
from io import BytesIO
from gtts import gTTS
from streamlit_gsheets import GSheetsConnection

# ---------------------------------------------------------
# 1. 데이터 세팅 (Google Sheets 연결)
# ---------------------------------------------------------
conn = st.connection("gsheets", type=GSheetsConnection)

def load_data():
    try:
        # ttl=0 : 캐시 없이 매번 최신 데이터 읽기
        df = conn.read(worksheet="Sheet1", ttl=0)
        
        # 시트가 비어있을 경우 경고
        if df.empty:
            st.warning("Google Sheet is empty. Please add words to your sheet.")
            st.stop()
            
        return df
    except Exception as e:
        st.error(f"Google Sheet Connection Error: {e}")
        st.stop()

# 데이터 로드
if 'vocab_db' not in st.session_state:
    st.session_state.vocab_db = load_data()

df = st.session_state.vocab_db
# 필수 컬럼 확인 및 처리
if 'next_review' not in df.columns:
    df['next_review'] = None

# 날짜 포맷 정리
df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

# 세션 변수 초기화
if 'current_word_id' not in st.session_state:
    st.session_state.current_word_id = None
if 'quiz_options' not in st.session_state:
    st.session_state.quiz_options = []
if 'show_answer' not in st.session_state:
    st.session_state.show_answer = False

# 이번 세션에서 공부한 단어 수 카운트
if 'session_count' not in st.session_state:
    st.session_state.session_count = 0

# ---------------------------------------------------------
# 2. 로직 함수
# ---------------------------------------------------------
def get_next_word(df, difficulty, topic):
    today_str = str(datetime.date.today())
    
    # 난이도 및 주제 필터
    mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])
    
    # [수정됨] 주제가 'All'이 아닐 경우 해당 주제만 필터링
    if topic != "All":
        mask = mask & (df['topic'] == topic)
    
    # 복습 날짜 필터
    date_mask = df['next_review'] <= today_str
    
    candidates = df[mask & date_mask]
    
    if len(candidates) == 0:
        return None
    
    selected = candidates.sample(1).iloc[0]
    return selected['id']

def update_srs(word_id, is_correct):
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
    
    # 메모리 업데이트
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    
    # 구글 시트 업데이트
    conn.update(worksheet="Sheet1", data=st.session_state.vocab_db)
    
    # 상태 초기화
    st.session_state.current_word_id = None
    st.session_state.quiz_options = []
    st.session_state.show_answer = False
    
    # 공부한 단어 수 증가
    st.session_state.session_count += 1
    
    st.toast("Progress Saved! 💾")

# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 TOEFL Voca Master")

with st.sidebar:
    st.header("Settings")
    
    # 목표 단어 수 설정
    goal_options = [10, 15, 20, "Unlimited"]
    session_goal = st.selectbox("🎯 Daily Goal (Words)", goal_options)
    
    # 진행 상황 표시
    if session_goal != "Unlimited":
        st.write(f"**Progress:** {st.session_state.session_count} / {session_goal}")
        st.progress(min(st.session_state.session_count / session_goal, 1.0))
    
    st.divider()
    
    # [수정됨] 사용자가 요청한 주제 목록 적용
    topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
    topic = st.selectbox("Topic (Subject)", topic_list)
    
    difficulty = st.slider("Level Difficulty", 1, 3, (1, 3))
    
    today = str(datetime.date.today())
    # 현재 설정된 주제와 난이도에 맞는 남은 단어 수 계산
    filtered_df = st.session_state.vocab_db
    if topic != "All":
        filtered_df = filtered_df[filtered_df['topic'] == topic]
    filtered_df = filtered_df[(filtered_df['level'] >= difficulty[0]) & (filtered_df['level'] <= difficulty[1])]
    rem_count = len(filtered_df[filtered_df['next_review'] <= today])
    
    st.write(f"Words to review: {rem_count}")
    
    # 초기화 버튼 (데이터 유지)
    if st.button("Reset Progress (Keep Words)"):
        df_reset = st.session_state.vocab_db.copy()
        df_reset['box'] = 0
        df_reset['next_review'] = '0000-00-00'
        conn.update(worksheet="Sheet1", data=df_reset)
        st.toast("Progress reset! Start fresh.")
        st.session_state.clear()
        st.rerun()

# 목표 달성 체크
if session_goal != "Unlimited" and st.session_state.session_count >= session_goal:
    st.balloons()
    st.success(f"🏆 Mission Complete! You reviewed {session_goal} words today.")
    
    if st.button("Start New Session (Reset Count)"):
        st.session_state.session_count = 0
        st.rerun()
        
    st.stop() 

# 메인 로직
if st.session_state.current_word_id is None:
    new_id = get_next_word(st.session_state.vocab_db, difficulty, topic)
    if new_id is not None:
        st.session_state.current_word_id = new_id
        
        current_word = st.session_state.vocab_db[st.session_state.vocab_db['id'] == new_id].iloc[0]
        synonyms = current_word['synonyms']
        if isinstance(synonyms, str):
            try: synonyms = ast.literal_eval(synonyms)
            except: synonyms = [synonyms]
            
        options = synonyms[:] 
        
        wrong_pool = []
        other_words = st.session_state.vocab_db[st.session_state.vocab_db['id'] != new_id]
        for syn_list in other_words['synonyms']:
            if isinstance(syn_list, str):
                try: syn_list = ast.literal_eval(syn_list)
                except: continue
            if isinstance(syn_list, list):
                wrong_pool.extend(syn_list)
        
        if len(wrong_pool) >= 3:
            wrong_options = random.sample(wrong_pool, 2)
            options = [options[0]] + wrong_options
            random.shuffle(options)
        else:
            options = options + ["Similar A", "Similar B"][:3]
            
        st.session_state.quiz_options = options 
    else:
        st.success(f"🎉 No words left for '{topic}'!")
        st.write("Try changing the Topic or Level.")
        st.stop()

word_id = st.session_state.current_word_id
row = st.session_state.vocab_db[st.session_state.vocab_db['id'] == word_id].iloc[0]

st.markdown(f"""
<div style="padding: 20px; border-radius: 10px; background-color: #f0f2f6; text-align: center; margin-bottom: 20px;">
    <p style="color: grey; font-size: 0.9em;">{row['topic']} | Level {row['level']}</p>
    <h1 style="color: #1f77b4; font-size: 3em; margin: 0;">{row['word']}</h1>
</div>
""", unsafe_allow_html=True)

# 발음 듣기
try:
    sound_file = BytesIO()
    tts = gTTS(text=row['word'], lang='en')
    tts.write_to_fp(sound_file)
    sound_file.seek(0)
    st.audio(sound_file, format='audio/mpeg')
except Exception as e:
    st.warning(f"Voice unavailable: {e}")

tab1, tab2 = st.tabs(["📖 Flashcard", "🧩 Synonym Quiz"])

with tab1:
    syn_disp = row['synonyms']
    if isinstance(syn_disp, str):
        try: syn_disp = ast.literal_eval(syn_disp)
        except: syn_disp = [syn_disp]

    if not st.session_state.show_answer:
        if st.button("🔍 Show Definition", use_container_width=True):
            st.session_state.show_answer = True
            st.rerun()
    else:
        st.markdown(f"**Def:** {row['definition']}")
        st.markdown(f"**Ex:** *\"{row['example']}\"*")
        st.markdown(f"**Syn:** {', '.join(syn_disp)}")
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("❌ No", use_container_width=True):
                update_srs(word_id, False)
                st.rerun()
        with c2:
            if st.button("✅ Yes", use_container_width=True):
                update_srs(word_id, True)
                st.rerun()

with tab2:
    st.write(f"Synonym for **'{row['word']}'**?")
    syn_check = row['synonyms']
    if isinstance(syn_check, str):
        try: syn_check = ast.literal_eval(syn_check)
        except: syn_check = [syn_check]

    with st.form("quiz"):
        choice = st.radio("Choose:", st.session_state.quiz_options)
        if st.form_submit_button("Submit"):
            if choice in syn_check:
                st.success("Correct!")
                st.session_state.lqr = True
            else:
                st.error(f"Wrong. Answer: {', '.join(syn_check)}")
                st.session_state.lqr = False
    
    if 'lqr' in st.session_state:
        if st.button("Next ➡️"):
            res = st.session_state.lqr
            del st.session_state['lqr']
            update_srs(word_id, res)
            st.rerun()
