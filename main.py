import streamlit as st
import pandas as pd
import datetime
import random
import json
import ast
from io import BytesIO # 소리 데이터를 메모리에서 다루기 위해 추가
from gtts import gTTS  # 구글 TTS 라이브러리 추가
from streamlit_gsheets import GSheetsConnection

# ---------------------------------------------------------
# 1. 데이터 세팅 (Google Sheets 연결)
# ---------------------------------------------------------
conn = st.connection("gsheets", type=GSheetsConnection)

# [수정된] 데이터 로드 함수
# [수정된] 데이터 로드 함수: JSON은 절대 보지 않고, 시트만 믿습니다.
def load_data():
    try:
        # ttl=0 : 캐시(기억)를 남기지 말고 매번 시트에서 새로 가져오라는 뜻
        df = conn.read(worksheet="Sheet1", ttl=0)
        
        # 데이터가 비어있어도 JSON에서 복구하지 않음 (덮어쓰기 방지)
        # 그냥 빈 상태면 빈 상태인 대로 둡니다.
        if df.empty:
            st.warning("구글 시트가 비어있습니다. 시트에 데이터를 채워주세요.")
            
        return df
    except Exception as e:
        st.error(f"Google Sheet 연결 에러: {e}")
        st.stop()
        
if 'vocab_db' not in st.session_state:
    st.session_state.vocab_db = load_data()

df = st.session_state.vocab_db
if 'next_review' not in df.columns:
    df['next_review'] = None
    
df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

if 'current_word_id' not in st.session_state:
    st.session_state.current_word_id = None
if 'quiz_options' not in st.session_state:
    st.session_state.quiz_options = []
if 'show_answer' not in st.session_state:
    st.session_state.show_answer = False

# ---------------------------------------------------------
# 2. 로직 함수
# ---------------------------------------------------------
def get_next_word(df, difficulty, topic):
    today_str = str(datetime.date.today())
    
    mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])
    if topic != "All":
        mask = mask & (df['topic'] == topic)
    
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
    
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    
    conn.update(worksheet="Sheet1", data=st.session_state.vocab_db)
    
    st.session_state.current_word_id = None
    st.session_state.quiz_options = []
    st.session_state.show_answer = False
    st.toast("Progress Saved to Google Sheets! 💾")

# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 TOEFL Voca (with Voice 🔊)")

with st.sidebar:
    st.header("Settings")
    topic = st.selectbox("Topic", ["All", "Social Science", "Science", "Linguistics", "Sociology", "Economics", "Medicine", "Art", "Biology", "History", "Geology", "Chemistry", "Ecology", "Psychology", "Business", "Law", "Physics", "Philosophy", "Education", "Technology", "General"])
    difficulty = st.slider("Level Difficulty", 1, 3, (1, 3))
    
    today = str(datetime.date.today())
    rem_count = len(st.session_state.vocab_db[st.session_state.vocab_db['next_review'] <= today])
    st.write(f"Words to review: {rem_count}")
    
# 버튼 이름을 더 명확하게 바꿉니다
    if st.button("Reset Progress (Keep Words)"):
        # 1. 현재 보고 있는 데이터(80개)를 가져옵니다.
        df_reset = st.session_state.vocab_db.copy()
        
        # 2. 점수(box)와 날짜(next_review)만 초기화합니다.
        df_reset['box'] = 0
        df_reset['next_review'] = '0000-00-00'
        
        # 3. 구글 시트에 업데이트합니다.
        conn.update(worksheet="Sheet1", data=df_reset)
        
        # 4. 앱을 새로고침합니다.
        st.toast("Progress has been reset! (Words are safe)")
        st.session_state.clear()
        st.rerun()

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
        st.success("🎉 All done for today!")
        st.write("Check your Google Sheet to see the progress.")
        st.stop()

word_id = st.session_state.current_word_id
row = st.session_state.vocab_db[st.session_state.vocab_db['id'] == word_id].iloc[0]

st.markdown(f"""
<div style="padding: 20px; border-radius: 10px; background-color: #f0f2f6; text-align: center; margin-bottom: 20px;">
    <p style="color: grey; font-size: 0.9em;">{row['topic']} | Level {row['level']}</p>
    <h1 style="color: #1f77b4; font-size: 3em; margin: 0;">{row['word']}</h1>
</div>
""", unsafe_allow_html=True)

# --- [새 기능] 발음 듣기 버튼 ---
# --- [수정된 기능] 발음 듣기 (되감기 코드 추가) ---
try:
    sound_file = BytesIO()
    tts = gTTS(text=row['word'], lang='en')
    tts.write_to_fp(sound_file)
    
    # [중요] 다 쓴 데이터를 처음부터 읽을 수 있도록 '커서'를 맨 앞으로 이동!
    sound_file.seek(0)
    
    # format을 'audio/mpeg'로 명시 (호환성 향상)
    st.audio(sound_file, format='audio/mpeg')
    
except Exception as e:
    st.warning(f"Voice Error: {e}")

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
