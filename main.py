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
# 1. 데이터 및 세션 초기화
# ---------------------------------------------------------
conn = st.connection("gsheets", type=GSheetsConnection)

def load_data():
    try:
        # 캐시 없이 매번 최신 데이터 로드
        df = conn.read(worksheet="Sheet1", ttl=0)
        if df.empty:
            st.warning("Google Sheet is empty.")
            st.stop()
        return df
    except Exception as e:
        st.error(f"Google Sheet Connection Error: {e}")
        st.stop()

if 'vocab_db' not in st.session_state:
    st.session_state.vocab_db = load_data()

# 데이터 전처리
df = st.session_state.vocab_db
if 'next_review' not in df.columns:
    df['next_review'] = None
df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

# --- [앱 상태 관리 변수들] ---
if 'app_mode' not in st.session_state:
    st.session_state.app_mode = 'setup'  # setup / quiz / summary
if 'session_config' not in st.session_state:
    st.session_state.session_config = {} # 사용자가 선택한 설정 저장
if 'session_stats' not in st.session_state:
    st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
if 'current_word_id' not in st.session_state:
    st.session_state.current_word_id = None
if 'quiz_options' not in st.session_state:
    st.session_state.quiz_options = []
if 'show_answer' not in st.session_state:
    st.session_state.show_answer = False

# ---------------------------------------------------------
# 2. 로직 함수
# ---------------------------------------------------------
def get_next_word():
    df = st.session_state.vocab_db
    config = st.session_state.session_config
    
    # 1. 난이도 필터
    difficulty = config.get('difficulty', (1, 3))
    mask = (df['level'] >= difficulty[0]) & (df['level'] <= difficulty[1])
    
    # 2. 주제 필터
    topic = config.get('topic', 'All')
    if topic != "All":
        mask = mask & (df['topic'] == topic)
        
    # 3. 모드별 필터 (일반 vs 오답노트)
    mode = config.get('mode', 'Standard Study')
    today_str = str(datetime.date.today())
    
    if mode == 'Review Mistakes Only':
        # 오답 노트: Box가 0인 것(틀려서 리셋된 것)만 필터링
        logic_mask = df['box'] == 0
    else:
        # 일반 모드: 오늘 복습해야 할 단어 OR 아직 안 본 단어
        logic_mask = df['next_review'] <= today_str
    
    candidates = df[mask & logic_mask]
    
    if len(candidates) == 0:
        return None
    
    # 랜덤 추출
    selected = candidates.sample(1).iloc[0]
    return selected['id']

def update_srs(word_id, is_correct):
    df = st.session_state.vocab_db
    idx = df[df['id'] == word_id].index[0]
    current_box = df.at[idx, 'box']
    
    if is_correct:
        # 정답: 통계 업데이트
        st.session_state.session_stats['correct'] += 1
        # SRS 로직: 박스 이동
        new_box = min(current_box + 1, 5)
        days_to_add = int(2 ** new_box)
    else:
        # 오답: 통계 업데이트
        st.session_state.session_stats['wrong'] += 1
        # SRS 로직: 박스 0으로 초기화 (이게 곧 오답 기록입니다)
        new_box = 0
        days_to_add = 0
    
    # 전체 진행 수 증가
    st.session_state.session_stats['total'] += 1
        
    next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)
    
    # DB 메모리 업데이트
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    
    # 구글 시트 저장
    conn.update(worksheet="Sheet1", data=st.session_state.vocab_db)
    
    # 상태 초기화
    st.session_state.current_word_id = None
    st.session_state.quiz_options = []
    st.session_state.show_answer = False
    st.toast(f"{'Correct! 🟢' if is_correct else 'Saved to Mistakes 🔴'}")

# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 NicholaSOOBIN TOEFL Voca")

# 사이드바는 이제 '데이터 관리' 용도로만 사용
with st.sidebar:
    st.header("Data Management")
    if st.button("Reset All Progress (Keep Words)"):
        df_reset = st.session_state.vocab_db.copy()
        df_reset['box'] = 0
        df_reset['next_review'] = '0000-00-00'
        conn.update(worksheet="Sheet1", data=df_reset)
        st.toast("DB Reset Complete!")
        st.session_state.clear()
        st.rerun()
    st.info("Settings are now on the main screen.")

# --- 화면 1: 설정 (Setup) ---
if st.session_state.app_mode == 'setup':
    st.markdown("### ⚙️ Study Setup")
    
    with st.form("setup_form"):
        c1, c2 = st.columns(2)
        with c1:
            topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
            sel_topic = st.selectbox("Topic", topic_list)
            
            sel_mode = st.radio("Mode", ["Standard Study (SRS)", "Review Mistakes Only"], 
                                help="Standard: Due words | Mistakes: Only words you got wrong (Box 0)")
            
        with c2:
            sel_goal = st.selectbox("Daily Goal", [5, 10, 15, 20, 30])
            sel_diff = st.slider("Difficulty", 1, 3, (1, 3))

        submitted = st.form_submit_button("🚀 Start Session", use_container_width=True)
        
        if submitted:
            # 설정 저장
            st.session_state.session_config = {
                'topic': sel_topic,
                'goal': sel_goal,
                'difficulty': sel_diff,
                'mode': sel_mode
            }
            # 통계 초기화
            st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
            # 퀴즈 모드로 전환
            st.session_state.app_mode = 'quiz'
            st.rerun()

# --- 화면 2: 퀴즈 (Quiz) ---
elif st.session_state.app_mode == 'quiz':
    config = st.session_state.session_config
    stats = st.session_state.session_stats
    
    # 상단 진행바
    goal = config['goal']
    current = stats['total']
    st.progress(min(current / goal, 1.0))
    st.caption(f"Progress: {current} / {goal} (Topic: {config['topic']})")

    # 목표 달성 체크
    if current >= goal:
        st.session_state.app_mode = 'summary'
        st.rerun()

    # # 문제 로딩
    # if st.session_state.current_word_id is None:
    #     new_id = get_next_word()
    #     if new_id is not None:
    #         st.session_state.current_word_id = new_id
            
    #         # 보기 생성 로직
    #         current_word = st.session_state.vocab_db[st.session_state.vocab_db['id'] == new_id].iloc[0]
    #         synonyms = current_word['synonyms']
    #         if isinstance(synonyms, str):
    #             try: synonyms = ast.literal_eval(synonyms)
    #             except: synonyms = [synonyms]
                
    #         options = synonyms[:]
            
    #         # 오답 풀 만들기
    #         wrong_pool = []
    #         other_words = st.session_state.vocab_db[st.session_state.vocab_db['id'] != new_id]
    #         for syn_list in other_words['synonyms']:
    #             if isinstance(syn_list, str):
    #                 try: syn_list = ast.literal_eval(syn_list)
    #                 except: continue
    #             if isinstance(syn_list, list):
    #                 wrong_pool.extend(syn_list)
            
    #         if len(wrong_pool) >= 3:
    #             wrong_options = random.sample(wrong_pool, 2)
    #             options = [options[0]] + wrong_options
    #             random.shuffle(options)
    #         else:
    #             options = options + ["Similar A", "Similar B"][:3]
                
    #         st.session_state.quiz_options = options
    #     else:
    #         st.warning("No words found matching your criteria!")
    #         if st.button("Back to Home"):
    #             st.session_state.app_mode = 'setup'
    #             st.rerun()
    #         st.stop()

    # -------------------------------------------------------
    # 문제 로딩 로직 (품사 기반 오답 필터링 적용)
    # -------------------------------------------------------
    
    if st.session_state.current_word_id is None:
        new_id = get_next_word()
        if new_id is not None:
            st.session_state.current_word_id = new_id
            
            # 1. 현재 문제 단어 정보 가져오기
            current_word = st.session_state.vocab_db[st.session_state.vocab_db['id'] == new_id].iloc[0]
            
            # 정답 보기 (현재 단어의 동의어들)
            synonyms = current_word['synonyms']
            if isinstance(synonyms, str):
                try: synonyms = ast.literal_eval(synonyms)
                except: synonyms = [synonyms]
            
            # 보기 1번은 정답 중 하나
            correct_option = synonyms[0] 
            options = [correct_option]
            
            # 2. 오답 풀(Pool) 만들기전략
            # "현재 단어와 품사(pos)가 같은 다른 단어들"을 찾습니다.
            # 그 단어들이 가진 synonym들을 오답 후보로 씁니다.
            
            df = st.session_state.vocab_db
            target_pos = current_word.get('pos', None) # 현재 단어의 품사 (예: verb)
            
            # (A) 품사 정보가 있고, 같은 품사를 가진 다른 단어가 충분할 때
            if target_pos and len(df[df['pos'] == target_pos]) > 5:
                # 같은 품사이면서 + 현재 단어가 아닌 것들
                candidate_df = df[(df['pos'] == target_pos) & (df['id'] != new_id)]
            else:
                # (B) 품사가 없거나 데이터가 부족하면 -> 그냥 전체 단어에서 찾음 (에러 방지)
                candidate_df = df[df['id'] != new_id]

            # 오답 후보군 수집 (후보 단어들의 synonym을 싹 긁어모음)
            wrong_pool = []
            for syn_list in candidate_df['synonyms']:
                if isinstance(syn_list, str):
                    try: syn_list = ast.literal_eval(syn_list)
                    except: continue
                if isinstance(syn_list, list):
                    wrong_pool.extend(syn_list)
            
            # 3. 정제 및 선택
            # 중복 제거
            wrong_pool = list(set(wrong_pool))
            
            # 정답 리스트에 있는 단어가 오답으로 나오면 안 되므로 제거
            wrong_pool = [w for w in wrong_pool if w not in synonyms]
            
            # 오답 개수 설정 (3개 뽑아서 총 4지선다)
            # 만약 오답 후보가 부족하면 "Random A" 등으로 채움
            needed = 3
            if len(wrong_pool) >= needed:
                wrong_options = random.sample(wrong_pool, needed)
            else:
                wrong_options = wrong_pool + ["Option A", "Option B", "Option C"]
                wrong_options = wrong_options[:needed]
            
            # 4. 최종 보기 합치기 및 섞기
            options = options + wrong_options
            random.shuffle(options)
            
            st.session_state.quiz_options = options
            
        else:
            st.warning("No words found matching your criteria!")
            if st.button("Back to Home"):
                st.session_state.app_mode = 'setup'
                st.rerun()
            st.stop()
            
    # UI 렌더링
    word_id = st.session_state.current_word_id
    row = st.session_state.vocab_db[st.session_state.vocab_db['id'] == word_id].iloc[0]

    st.markdown(f"""
    <div style="padding: 30px; border-radius: 15px; background-color: #f0f2f6; text-align: center; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
        <p style="color: grey; margin-bottom: 5px;">{row['topic']} | Level {row['level']}</p>
        <h1 style="color: #2c3e50; font-size: 3.5em; margin: 0;">{row['word']}</h1>
    </div>
    """, unsafe_allow_html=True)

    # 발음 듣기
    try:
        sound_file = BytesIO()
        tts = gTTS(text=row['word'], lang='en')
        tts.write_to_fp(sound_file)
        sound_file.seek(0)
        st.audio(sound_file, format='audio/mpeg')
    except:
        st.caption("Voice unavailable")

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
            st.info(f"**Definition:** {row['definition']}")
            st.caption(f"**Example:** {row['example']}")
            st.write(f"**Synonyms:** {', '.join(syn_disp)}")
            
            c1, c2 = st.columns(2)
            with c1:
                if st.button("❌ Don't Know", use_container_width=True):
                    update_srs(word_id, False)
                    st.rerun()
            with c2:
                if st.button("✅ Know", use_container_width=True):
                    update_srs(word_id, True)
                    st.rerun()

    with tab2:
        st.write(f"Select the synonym for **'{row['word']}'**")
        syn_check = row['synonyms']
        if isinstance(syn_check, str):
            try: syn_check = ast.literal_eval(syn_check)
            except: syn_check = [syn_check]

        with st.form("quiz_form"):
            choice = st.radio("Options:", st.session_state.quiz_options)
            if st.form_submit_button("Submit Answer"):
                if choice in syn_check:
                    st.success("Correct!")
                    st.session_state.lqr = True
                else:
                    st.error(f"Wrong! The answer is {', '.join(syn_check)}")
                    st.session_state.lqr = False
        
        if 'lqr' in st.session_state:
            if st.button("Next Word ➡️", type="primary"):
                res = st.session_state.lqr
                del st.session_state['lqr']
                update_srs(word_id, res)
                st.rerun()

# --- 화면 3: 결과 요약 (Summary) ---
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
