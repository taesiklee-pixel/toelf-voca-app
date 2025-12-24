import streamlit as st
import pandas as pd
import datetime
import random
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
        
        # 1. 컬럼명 소문자 통일
        df.columns = df.columns.str.lower()
        
        # 2. 중복 단어 제거
        df = df.drop_duplicates(subset=['word'], keep='first')
        
        # 3. [핵심 수정] 컬럼 구조 동기화 체크
        needs_initial_save = False
        
        # mistake_count 없으면 생성
        if 'mistake_count' not in df.columns:
            df['mistake_count'] = 0
            needs_initial_save = True
            
        # box 없으면 생성
        if 'box' not in df.columns:
            df['box'] = 0
            needs_initial_save = True

        # next_review 없으면 생성
        if 'next_review' not in df.columns:
            df['next_review'] = '0000-00-00'
            needs_initial_save = True

        # 데이터 타입 정리 (NaN 방지)
        df['mistake_count'] = df['mistake_count'].fillna(0).astype(int)
        df['box'] = df['box'].fillna(0).astype(int)
        df['next_review'] = df['next_review'].astype(str).replace(['nan', 'None'], '0000-00-00')

        # [중요] 컬럼을 새로 만들었으면 시트에도 즉시 반영하여 헤더를 생성함
        if needs_initial_save:
            conn.update(worksheet="Sheet1", data=df)
            st.toast("Updated Google Sheet structure (added columns).")

        if df.empty:
            st.warning("Google Sheet is empty.")
            st.stop()
            
        return df
    except Exception as e:
        st.error(f"Google Sheet Connection Error: {e}")
        st.stop()

if 'vocab_db' not in st.session_state:
    st.session_state.vocab_db = load_data()

# 데이터 전처리 (세션용)
df = st.session_state.vocab_db

# --- [앱 상태 관리 변수들] ---
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
        
    # 3. 모드별 필터
    mode = config.get('mode', 'Standard Study')
    today_str = str(datetime.date.today())
    
    if mode == 'Review Mistakes Only':
        # 오답 노트: Box가 0이면서 AND 오답 횟수가 1 이상인 것
        logic_mask = (df['box'] == 0) & (df['mistake_count'] > 0)
        
        # 틀린 단어가 없으면 안내 후 일반 모드로 전환 고려 (여기선 토스트만)
        if df[mask & logic_mask].empty:
            st.toast("No historical mistakes found! (Box 0 & Count > 0)")
            
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
    # id로 인덱스 찾기
    idx_list = df[df['id'] == word_id].index.tolist()
    if not idx_list:
        return # 에러 방지
    idx = idx_list[0]
    
    current_box = int(df.at[idx, 'box'])
    current_mistakes = int(df.at[idx, 'mistake_count'])
    
    if is_correct:
        st.session_state.session_stats['correct'] += 1
        new_box = min(current_box + 1, 5)
        days_to_add = int(2 ** new_box)
        new_mistakes = current_mistakes # 정답이면 유지
    else:
        st.session_state.session_stats['wrong'] += 1
        new_box = 0 # 박스 초기화
        days_to_add = 0
        new_mistakes = current_mistakes + 1 # 오답 횟수 증가
    
    st.session_state.session_stats['total'] += 1
        
    next_date = datetime.date.today() + datetime.timedelta(days=days_to_add)
    
    # DB 메모리 업데이트
    st.session_state.vocab_db.at[idx, 'box'] = new_box
    st.session_state.vocab_db.at[idx, 'next_review'] = str(next_date)
    st.session_state.vocab_db.at[idx, 'mistake_count'] = new_mistakes
    
    # 구글 시트 저장
    try:
        conn.update(worksheet="Sheet1", data=st.session_state.vocab_db)
        # st.toast("Progress saved to Sheet.") # 디버깅용 메시지
    except Exception as e:
        st.error(f"Save failed: {e}")

# ---------------------------------------------------------
# 3. UI 구성
# ---------------------------------------------------------
st.title("🎓 NicholaSOOBIN TOEFL Voca")

# 사이드바 데이터 관리
with st.sidebar:
    st.header("Data Management")
    if st.button("Reset All Progress"):
        df_reset = st.session_state.vocab_db.copy()
        df_reset['box'] = 0
        df_reset['next_review'] = '0000-00-00'
        df_reset['mistake_count'] = 0 
        conn.update(worksheet="Sheet1", data=df_reset)
        st.toast("All progress has been reset.")
        st.session_state.clear()
        st.rerun()

# --- 화면 1: 설정 (Setup) ---
if st.session_state.app_mode == 'setup':
    st.markdown("### ⚙️ Study Setup")
    
    with st.form("setup_form"):
        c1, c2 = st.columns(2)
        with c1:
            topic_list = ["All", "Science", "History", "Social Science", "Business", "Environment", "Education"]
            sel_topic = st.selectbox("Topic", topic_list)
            sel_mode = st.radio("Mode", ["Standard Study (SRS)", "Review Mistakes Only"], 
                                help="Standard: New & Due words | Mistakes: Words you got wrong before")
        with c2:
            sel_goal = st.selectbox("Daily Goal", [5, 10, 15, 20, 30])
            sel_diff = st.slider("Difficulty", 1, 3, (1, 3))

        submitted = st.form_submit_button("🚀 Start Session", use_container_width=True)
        
        if submitted:
            st.session_state.session_config = {
                'topic': sel_topic, 'goal': sel_goal, 'difficulty': sel_diff, 'mode': sel_mode
            }
            st.session_state.session_stats = {'correct': 0, 'wrong': 0, 'total': 0}
            st.session_state.app_mode = 'quiz'
            st.rerun()

# --- 화면 2: 퀴즈 (Quiz) ---
elif st.session_state.app_mode == 'quiz':
    config = st.session_state.session_config
    stats = st.session_state.session_stats
    
    # 진행바
    goal = config['goal']
    current = stats['total']
    st.progress(min(current / goal, 1.0))
    st.caption(f"Progress: {current} / {goal} (Topic: {config['topic']})")

    # 목표 달성 체크
    if current >= goal:
        st.session_state.app_mode = 'summary'
        st.rerun()

    # 데이터프레임 확보
    df = st.session_state.vocab_db

    # -------------------------------------------------------
    # 문제 로딩 로직
    # -------------------------------------------------------
    if st.session_state.current_word_id is None:
        new_id = get_next_word()
        if new_id is not None:
            st.session_state.current_word_id = new_id
            
            current_word = df[df['id'] == new_id].iloc[0]
            
            # 정답 파싱
            synonyms = current_word['synonyms']
            if isinstance(synonyms, str):
                try: synonyms = ast.literal_eval(synonyms)
                except: synonyms = [synonyms]
            
            correct_option = synonyms[0]
            options = [correct_option]
            
            # [오답 보기 추출]
            target_pos = str(current_word.get('pos', '')).strip().lower()
            
            df_pool = df.copy()
            df_pool['pos_norm'] = df_pool['pos'].fillna('').astype(str).str.strip().str.lower()
            
            # 품사 필터링
            if target_pos and target_pos != 'nan' and target_pos != '':
                candidate_df = df_pool[(df_pool['pos_norm'] == target_pos) & (df_pool['id'] != new_id)]
                if candidate_df.empty:
                    candidate_df = df_pool[df_pool['id'] != new_id]
            else:
                candidate_df = df_pool[df_pool['id'] != new_id]

            # 오답 풀 수집
            wrong_pool = []
            for syn_list in candidate_df['synonyms']:
                if isinstance(syn_list, str):
                    try: syn_list = ast.literal_eval(syn_list)
                    except: continue
                if isinstance(syn_list, list):
                    for w in syn_list:
                        wrong_pool.append(w)
            
            wrong_pool = list(set(wrong_pool))
            wrong_pool = [w for w in wrong_pool if w not in synonyms]
            
            needed = 3
            if len(wrong_pool) >= needed:
                wrong_options = random.sample(wrong_pool, needed)
            else:
                defaults = ["Option A", "Option B", "Option C"]
                wrong_options = wrong_pool + defaults[:needed - len(wrong_pool)]
            
            options = options + wrong_options
            random.shuffle(options)
            
            st.session_state.quiz_options = options
            
            # 문제 로딩 시 상태 초기화
            st.session_state.quiz_answered = False
            st.session_state.selected_option = None
            
        else:
            st.warning("No words matching your criteria!")
            if config['mode'] == 'Review Mistakes Only':
                st.info("💡 You have no recorded mistakes yet! (Or you've finished reviewing them). Try 'Standard Study'.")
            if st.button("Back to Setup"):
                st.session_state.app_mode = 'setup'
                st.rerun()
            st.stop()

    # -------------------------------------------------------
    # UI 구성 (문제 표시 -> 버튼 -> 결과 및 해설)
    # -------------------------------------------------------
    current_id = st.session_state.current_word_id
    current_word_row = df[df['id'] == current_id].iloc[0]
    
    correct_synonyms = current_word_row['synonyms']
    if isinstance(correct_synonyms, str):
        try: correct_synonyms = ast.literal_eval(correct_synonyms)
        except: correct_synonyms = [correct_synonyms]

    # 문제 화면 출력
    st.markdown(f"### What is a synonym for: **{current_word_row['word']}**?")
    
    # 발음 듣기
    try:
        sound_file = BytesIO()
        tts = gTTS(text=current_word_row['word'], lang='en')
        tts.write_to_fp(sound_file)
        sound_file.seek(0)
        st.audio(sound_file, format='audio/mpeg')
    except:
        pass

    st.caption(f"Part of Speech: *{current_word_row['pos']}*")
    
    # [A] 답변 전
    if not st.session_state.quiz_answered:
        cols = st.columns(2)
        for i, option in enumerate(st.session_state.quiz_options):
            if cols[i % 2].button(option, key=f"btn_{i}", use_container_width=True):
                st.session_state.quiz_answered = True
                st.session_state.selected_option = option
                
                # 정답 처리 및 SRS 업데이트
                is_correct = option in correct_synonyms
                update_srs(current_id, is_correct)
                st.rerun()

    # [B] 답변 후
    else:
        selected = st.session_state.selected_option
        is_correct = selected in correct_synonyms
        
        answer_in_options = [opt for opt in st.session_state.quiz_options if opt in correct_synonyms]
        final_answer_text = answer_in_options[0] if answer_in_options else correct_synonyms[0]

        # 정답 여부 메시지
        if is_correct:
            st.success(f"✅ Correct! **'{selected}'** is a synonym for **'{current_word_row['word']}'**.")
        else:
            st.error(f"❌ Incorrect. The answer is **'{final_answer_text}'**.")

        # 상세 정보
        st.markdown("---")
        st.markdown(f"#### 📖 Study: **{current_word_row['word']}**")
        
        st.info(
            f"**Definition:** {current_word_row['definition']}\n\n"
            f"**Example:** *{current_word_row['example']}*"
        )

        if st.button("Next Question ➡️", type="primary"):
            st.session_state.current_word_id = None
            st.session_state.quiz_answered = False
            st.session_state.selected_option = None
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
