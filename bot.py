import os
import re
import asyncio
import logging
import threading
import time
import json
import random
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
RENDER_APP_URL = os.getenv('RENDER_APP_URL', '')

# States for conversation
GETTING_FILE, GETTING_QUIZ_NAME, GETTING_TIME, GETTING_MARKS, GETTING_NEGATIVE, GETTING_CREATOR = range(6)

# Store user data
user_data = {}
user_progress = {}
last_activity = time.time()

# Keep-alive configuration
KEEP_ALIVE_INTERVAL = 5 * 60  # Ping every 5 minutes

def create_progress_bar(current, total, bar_length=20):
    """Create a visual progress bar"""
    progress = current / total
    filled_length = int(bar_length * progress)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    percentage = int(progress * 100)
    return f"{bar} {percentage}%"

# Simple HTTP server for health checks
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global last_activity
        last_activity = time.time()
        
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        elif self.path == '/wake':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'AWAKE')
        elif self.path == '/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            status = {
                'status': 'running',
                'last_activity': datetime.fromtimestamp(last_activity).isoformat(),
                'active_users': len(user_data)
            }
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        return

def run_health_server():
    """Run a simple HTTP server for health checks"""
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()

def keep_alive_ping():
    """Ping the app itself to keep it awake"""
    if RENDER_APP_URL:
        try:
            # Send multiple pings to ensure wake-up
            for i in range(3):
                try:
                    response = requests.get(f"{RENDER_APP_URL}/wake", timeout=5)
                    logger.info(f"Keep-alive ping {i+1}: {response.status_code}")
                except:
                    pass
                time.sleep(1)
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

def keep_alive_worker():
    """Background thread to keep the app alive"""
    logger.info("Keep-alive worker started")
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        keep_alive_ping()

def update_activity():
    """Update the last activity timestamp"""
    global last_activity
    last_activity = time.time()

def parse_txt_file(content):
    """Parse various TXT file formats and extract questions"""
    questions = []
    
    # Split by double newlines or question patterns
    blocks = re.split(r'\n\s*\n|(?=Q\.\d+|\d+\.\s*[A-Z])', content.strip())
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
            
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if len(lines) < 3:  # Minimum lines for a question
            continue
        
        question = {
            "question": "",
            "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
            "answer": "",
            "solution_text": ""
        }
        
        current_line = 0
        
        # Detect format and parse accordingly
        # Format 1: "1. Question" or "Q.1 Question"
        if re.match(r'^(?:\d+\.\s*|Q\.\d+\s+)', lines[0]):
            # Extract question (remove number prefix)
            question_text = re.sub(r'^(?:\d+\.\s*|Q\.\d+\s+)', '', lines[0])
            question_lines = [question_text]
            current_line = 1
            
            # Check if next line is Hindi question (not starting with option pattern)
            while (current_line < len(lines) and 
                   not re.match(r'^[a-e]\)\s*|^\([a-e]\)\s*|^[a-e]\.\s*', lines[current_line], re.IGNORECASE)):
                question_lines.append(lines[current_line])
                current_line += 1
        else:
            # Format without question number
            question_lines = []
            while (current_line < len(lines) and 
                   not re.match(r'^[a-e]\)\s*|^\([a-e]\)\s*|^[a-e]\.\s*', lines[current_line], re.IGNORECASE)):
                question_lines.append(lines[current_line])
                current_line += 1
        
        question["question"] = '<br>'.join(question_lines)
        
        # Extract options (4-5 options)
        option_count = 0
        option_pattern = re.compile(r'^([a-e])[\)\.]\s*|^\(([a-e])\)\s*', re.IGNORECASE)
        
        while (current_line < len(lines) and option_count < 5 and
               (option_pattern.match(lines[current_line]) or 
                re.match(r'^Correct|^Answer:|^ex:', lines[current_line], re.IGNORECASE) is None)):
            
            if option_pattern.match(lines[current_line]):
                option_key = f"option_{option_count + 1}"
                option_text = lines[current_line]
                current_line += 1
                
                # Add next line if it's Hindi text (doesn't start with option pattern, Correct, or ex:)
                if (current_line < len(lines) and 
                    not re.match(r'^[a-e]\)|^\([a-e]\)|^[a-e]\.|^Correct|^Answer:|^ex:', 
                                lines[current_line], re.IGNORECASE)):
                    option_text += f"<br>{lines[current_line]}"
                    current_line += 1
                
                question[option_key] = option_text
                option_count += 1
            else:
                current_line += 1
        
        # Extract correct answer
        while current_line < len(lines):
            line = lines[current_line]
            # Check for various answer formats
            if re.match(r'^Correct\s*(?:option)?\s*[:-]', line, re.IGNORECASE):
                match = re.search(r'[:-]\s*([a-e])', line, re.IGNORECASE)
                if match:
                    ans = match.group(1).lower()
                    answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
                    question["answer"] = answer_map.get(ans, '1')
            elif re.match(r'^Answer\s*[:-]', line, re.IGNORECASE):
                match = re.search(r'\(([a-e])\)', line, re.IGNORECASE)
                if not match:
                    match = re.search(r'[:-]\s*([a-e])', line, re.IGNORECASE)
                if match:
                    ans = match.group(1).lower()
                    answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
                    question["answer"] = answer_map.get(ans, '1')
            current_line += 1
        
        # Extract explanation
        solution_lines = []
        for i in range(len(lines)):
            if re.match(r'^ex:', lines[i], re.IGNORECASE):
                solution_lines.append(re.sub(r'^ex:\s*', '', lines[i], flags=re.IGNORECASE))
        
        question["solution_text"] = '<br>'.join(solution_lines)
        
        # Add metadata
        question["correct_score"] = "3"
        question["negative_score"] = "1"
        question["deleted"] = "0"
        question["difficulty_level"] = "0"
        question["option_image_1"] = question["option_image_2"] = question["option_image_3"] = ""
        question["option_image_4"] = question["option_image_5"] = ""
        question["question_image"] = ""
        question["solution_heading"] = ""
        question["solution_image"] = ""
        question["solution_video"] = ""
        question["sortingparam"] = "0.00"
        
        # Only add if we have question and at least 2 options
        if question["question"] and (question["option_1"] or question["option_2"]):
            questions.append(question)
    
    return questions

def generate_html_quiz(quiz_data):
    """Generate HTML quiz from the parsed data"""
    
    # Updated template with fast auth, candidate name in leaderboard, no ads
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no" />
<title>{quiz_name}</title>
<style>
:root{{
  --accent:#2ec4b6;
  --accent-dark:#1da89a;
  --muted:#69707a;
  --success:#1f9e5a;
  --danger:#c82d3f;
  --warning:#f39c12;
  --info:#3498db;
  --purple:#9b59b6;
  --bg:#f5f7fa;
  --card:#fff;
  --maxw:820px;
  --radius:10px;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:#111;padding-bottom:96px}}
.container{{max-width:var(--maxw);margin:auto;padding:10px 16px}}
header{{background:#fff;box-shadow:0 2px 6px rgba(0,0,0,0.08);position:relative;z-index:10}}
.header-inner{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;gap:12px}}
h1{{margin:0;color:var(--accent);font-size:18px}}
.btn{{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer;transition:background .18s}}
.btn:hover{{background:var(--accent-dark)}}
.btn-ghost{{background:#fff;color:var(--accent);border:2px solid var(--accent);padding:8px 12px;border-radius:999px;font-weight:700;cursor:pointer}}
.timer-text{{color:var(--accent-dark);font-weight:700;font-size:18px;min-width:72px;text-align:right}}
.toggle-pill{{position:relative;width:90px;height:30px;background:#eaeef0;border-radius:999px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:0 6px;font-size:13px;color:#444;font-weight:600}}
.toggle-pill span{{z-index:2;flex:1;text-align:center}}
.toggle-pill::before{{content:"";position:absolute;top:3px;left:3px;width:42px;height:24px;background:var(--accent);border-radius:999px;transition:.28s}}
.toggle-pill.active::before{{transform:translateX(45px);background:var(--accent-dark)}}
.toggle-pill.active span:last-child{{color:#fff}}
.toggle-pill span:first-child{{color:#fff}}

/* quiz card */
.card{{background:var(--card);border-radius:10px;padding:10px 12px;margin:12px 0;box-shadow:0 4px 10px rgba(0,0,0,0.05)}}
.qbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.qmeta{{font-size:13px;color:var(--muted)}}
.marking{{font-size:13px;color:var(--muted)}}
.qtext{{font-size:16px;margin:6px 0;font-weight:500}}
.opt{{padding:10px;border:1px solid #e6eaec;border-radius:8px;background:#fff;cursor:pointer;display:flex;align-items:center;gap:10px;transition:all .12s;font-weight:500}}
.opt:hover{{border-color:#cfd8da}}
.opt.selected{{border-color:var(--accent)}}
.opt.correct{{border-color:var(--success);background:rgba(31,158,90,0.12)}}
.opt.wrong{{border-color:var(--danger);background:rgba(200,45,63,0.12)}}
.custom-radio{{display:none;height:16px;width:16px;border-radius:50%;border:2px solid #ccc}}
.opt.selected .custom-radio{{display:block;border:6px solid var(--accent)}}
.opt.correct .custom-radio{{display:block;border:6px solid var(--success)}}
.opt.wrong .custom-radio{{display:block;border:6px solid var(--danger)}}
.explanation{{margin-top:8px;padding:10px;border-radius:8px;background:#fbfdfe;border:1px solid #edf2f3;display:none;font-size:14px}}

/* bottom nav */
.fbar{{position:fixed;left:0;right:0;bottom:0;background:#fff;box-shadow:0 -3px 12px rgba(0,0,0,0.08);display:flex;justify-content:center;z-index:50}}
.fbar-inner{{display:flex;justify-content:center;align-items:center;gap:10px;max-width:var(--maxw);width:100%;padding:10px}}
.fbar button{{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer}}
.fbar button:hover{{background:var(--accent-dark)}}

/* palette popup */
#palette{{position:fixed;top:64px;right:14px;background:#fff;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.12);padding:12px;display:none;gap:8px;flex-wrap:wrap;z-index:200;max-width:300px;max-height:70vh;overflow-y:auto;overscroll-behavior:contain}}
#palette .qbtn{{width:44px;height:44px;border-radius:8px;border:1px solid #e3eaeb;background:#fbfdff;cursor:pointer;font-weight:700}}
#palette .qbtn.attempted{{background:var(--success);color:#fff;border:none}}
#palette .qbtn.unattempted{{background:var(--danger);color:#fff;border:none}}
#palette .qbtn.marked{{background:var(--purple);color:#fff;border:none}}
#palette .qbtn.current{{border:3px solid var(--accent);font-weight:bold;transform:scale(1.05);box-shadow:0 0 0 2px rgba(46,196,182,0.2)}}
#palette-summary{{margin-top:8px;font-size:13px;color:var(--muted);text-align:center}}

/* modal */
.modal{{position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:center;justify-content:center;z-index:300}}
.modal-content{{background:#fff;border-radius:12px;padding:18px;max-width:420px;width:92%;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.18)}}
.modal h3{{margin:0 0 8px;color:var(--accent)}}
.modal p{{color:#333;margin:8px 0 12px;font-size:15px}}
.modal .actions{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),var(--accent-dark));color:#fff;border:none;padding:8px 14px;border-radius:999px;font-weight:700;cursor:pointer}}

/* results */
.results{{margin-top:12px}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.stat{{flex:1 1 120px;padding:10px;border-radius:10px;text-align:center;background:#f7fbfb;border:1px solid #e6eeed}}
.stat h4{{margin:0;color:var(--accent);font-size:13px}}
.stat p{{margin:6px 0 0;font-weight:700;font-size:18px}}

/* New buttons */
.btn-mark{{background:var(--purple);color:#fff}}
.btn-save{{background:var(--info);color:#fff}}
.btn-clear{{background:var(--warning);color:#fff}}

/* 🔐 COPY & SELECTION BLOCK */
body{{
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
}}
input, textarea{{
  user-select: text !important;
}}

/* 🔢 MathJax mobile safety */
mjx-container{{
  max-width: 100%;
  overflow-x: auto;
  font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif !important;
  font-size: 1em;
}}

/* Responsive design */
@media (max-width: 768px){{
  .header-inner{{ flex-direction: column; gap:8px; padding:8px; }}
  .fbar-inner{{ flex-wrap: wrap; padding:8px; gap:6px; }}
  .fbar button{{ padding:6px 8px; font-size:12px; flex:1 1 80px; }}
  .container{{ padding:8px; }}
  .qtext{{ font-size:15px; }}
  .opt{{ padding:8px; }}
  #palette{{ top:120px; left:50%; transform:translateX(-50%); max-width:90%; }}
  #palette .qbtn{{ width:38px; height:38px; }}
  .stats{{ flex-direction:column; }}
  .stat{{ flex:1 1 auto; }}
}}
@media (max-width: 480px){{
  .timer-text{{ font-size:16px; }}
  h1{{ font-size:16px; }}
  .btn, .btn-ghost{{ padding:6px 10px; font-size:12px; }}
  .fbar-inner{{ gap:4px; }}
  .fbar button{{ padding:6px; font-size:11px; }}
  .card{{ padding:8px; }}
}}
@media (min-width: 1024px){{
  .container{{ max-width:900px; }}
  .fbar-inner{{ max-width:900px; }}
}}
</style>

<!-- 🔢 MathJax -->
<script>
  window.MathJax = {{
    tex: {{
      inlineMath: [['\\\\(', '\\\\)'], ['$', '$']],
      displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]
    }},
    options: {{ skipHtmlTags: ['script', 'style', 'textarea', 'pre'] }}
  }};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" async></script>

<!-- 🔥 Firebase SDK (App, Auth, Database) -->
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-auth-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-database-compat.js"></script>

<script>
  // 🔥 Your Firebase configuration (REPLACE WITH YOUR OWN)
  const firebaseConfig = {{
    apiKey: "AIzaSyBWF7Ojso-w0BucbqJylGR7h9eGeDQodzE",
    authDomain: "ssc-quiz-rank-percentile.firebaseapp.com",
    databaseURL: "https://ssc-quiz-rank-percentile-default-rtdb.firebaseio.com",
    projectId: "ssc-quiz-rank-percentile",
    storageBucket: "ssc-quiz-rank-percentile.firebasestorage.app",
    messagingSenderId: "944635517164",
    appId: "1:944635517164:web:62f0cc83892917f225edc9"
  }};
  firebase.initializeApp(firebaseConfig);
  const auth = firebase.auth();
  const db = firebase.database();

  // ---------- CONFIGURATION ----------
  const LOGIN_PAGE_URL = "/login";  // CHANGE to your actual login page URL
  // -----------------------------------

  // Quiz data from Python
  const QUIZ_TITLE = "{quiz_name}";
  const QUESTIONS = {questions_array};
  const TOTAL_TIME_SECONDS = {seconds};

  // Global state
  let currentUser = null;
  let current = 0;
  let answers = {{}};
  let markedForReview = new Set();
  let seconds = TOTAL_TIME_SECONDS;
  let timerInterval = null;
  let isQuiz = false;
  let LAST_RESULT_HTML = "";

  const QUIZ_STATE_KEY = "ssc_quiz_state_" + QUIZ_TITLE;
  const QUIZ_RESULT_KEY = "ssc_quiz_result_" + QUIZ_TITLE;

  // ----- FAST AUTH CHECK -----
  function showQuizImmediately() {{
    document.getElementById('loadingMessage').style.display = 'none';
    document.getElementById('quizHeader').style.display = 'flex';
    document.getElementById('quizContainer').style.display = 'block';
    document.getElementById('floatBar').style.display = 'flex';
    if (!window.quizInitialized) {{
      initQuiz();
      window.quizInitialized = true;
    }}
  }}

  // Try synchronous currentUser first
  const syncUser = auth.currentUser;
  if (syncUser) {{
    // Already authenticated – use it
    currentUser = syncUser;
    // Cache user data
    localStorage.setItem('quiz_user_uid', syncUser.uid);
    localStorage.setItem('quiz_user_email', syncUser.email);
    localStorage.setItem('quiz_user_displayName', syncUser.displayName || '');
    localStorage.setItem('quiz_login_time', Date.now());
    showQuizImmediately();
  }} else {{
    // Check for cached UID (optimistic)
    const cachedUid = localStorage.getItem('quiz_user_uid');
    const cachedName = localStorage.getItem('quiz_user_displayName') || localStorage.getItem('quiz_user_email');
    const cachedTime = localStorage.getItem('quiz_login_time');
    const now = Date.now();
    const CACHE_VALIDITY = 60 * 60 * 1000; // 1 hour

    if (cachedUid && cachedTime && (now - cachedTime < CACHE_VALIDITY)) {{
      // Show quiz immediately with cached data
      currentUser = {{
        uid: cachedUid,
        email: localStorage.getItem('quiz_user_email'),
        displayName: localStorage.getItem('quiz_user_displayName')
      }};
      showQuizImmediately();
    }}

    // Also set up observer to catch real auth state (and update cache if needed)
    auth.onAuthStateChanged(user => {{
      if (user) {{
        currentUser = user;
        // Update cache
        localStorage.setItem('quiz_user_uid', user.uid);
        localStorage.setItem('quiz_user_email', user.email);
        localStorage.setItem('quiz_user_displayName', user.displayName || '');
        localStorage.setItem('quiz_login_time', Date.now());

        // If quiz not already shown (cached was missing/expired), show it now
        if (!window.quizInitialized) {{
          showQuizImmediately();
        }}
      }} else {{
        // Not logged in – clear cache and redirect
        localStorage.removeItem('quiz_user_uid');
        localStorage.removeItem('quiz_user_email');
        localStorage.removeItem('quiz_user_displayName');
        localStorage.removeItem('quiz_login_time');
        const returnTo = encodeURIComponent(window.location.href);
        window.location.href = LOGIN_PAGE_URL + "?returnTo=" + returnTo;
      }}
    }});
  }}

  const el = id => document.getElementById(id);

  function initQuiz() {{
    el("qtotal").textContent = QUESTIONS.length;
    // Check if already submitted
    const resultSaved = localStorage.getItem(QUIZ_RESULT_KEY);
    if (resultSaved) {{
      const data = JSON.parse(resultSaved);
      if (data.submitted && data.resultHTML) {{
        showSavedResult(data);
        return;
      }}
    }}
    // Resume from saved state
    const saved = localStorage.getItem(QUIZ_STATE_KEY);
    if (saved) {{
      const state = JSON.parse(saved);
      current = state.current ?? 0;
      answers = state.answers ?? {{}};
      seconds = state.seconds ?? seconds;
      if (state.markedForReview) markedForReview = new Set(state.markedForReview);
    }}
    renderQuestion(current);
    startTimer();
    attachListeners();
    buildPalette();
    highlightPalette();
    renderMath();
  }}

  function showSavedResult(data) {{
    el("quizCard").style.display = "none";
    el("floatBar").style.display = "none";
    el("results").innerHTML = data.resultHTML;
    el("results").style.display = "block";
    if (data.headerHTML) {{
      el("headerControls").innerHTML = data.headerHTML;
      rebindResultHeaderActions();
    }}
    renderMath();
  }}

  function renderQuestion(i) {{
    current = i;
    const q = QUESTIONS[i];
    el("qindex").textContent = i+1;
    el("qtext").innerHTML = q.question || "";
    normalizeMathForQuiz(el("qtext"));
    renderMath();
    el("marking").innerHTML = `Marking: <span style="color:var(--success)">+${{Number(q.correct_score ?? 1)}}</span> / <span style="color:var(--danger)">-${{Number(q.negative_score ?? 0)}}</span>`;
    const opts = el("options");
    opts.innerHTML = "";
    el("explanation").style.display = "none";

    ["option_1","option_2","option_3","option_4","option_5"].forEach((k, idx) => {{
      if(!q[k]) return;
      const div = document.createElement("div");
      div.className = "opt";
      div.innerHTML = `<div class="custom-radio"></div><div style="flex:1">${{q[k]}}</div>`;
      div.addEventListener("click", () => selectOption(q, idx+1, div));
      const qid = q.id ?? i;
      if(answers[qid] === String(idx+1)) div.classList.add("selected");
      opts.appendChild(div);
    }});
    highlightPalette();
    renderMath();
    saveQuizState();
  }}

  function selectOption(q, val, div) {{
    const qid = q.id ?? current;
    answers[qid] = String(val);
    Array.from(el("options").children).forEach(o => o.classList.remove("selected"));
    div.classList.add("selected");
    if(isQuiz) showFeedback(q, val);
    highlightPalette();
    saveQuizState();
  }}

  function showFeedback(q, val) {{
    Array.from(el("options").children).forEach((o, idx) => {{
      o.classList.remove("correct","wrong");
      const idx1 = idx+1;
      if(String(q.answer) === String(idx1)) o.classList.add("correct");
      else if(String(val) === String(idx1)) o.classList.add("wrong");
    }});
    if(q.solution_text){{
      el("explanation").innerHTML = `<strong>Explanation:</strong> ${{q.solution_text}}`;
      el("explanation").style.display = "block";
      normalizeMathForQuiz(el("explanation"));
      renderMath();
    }}
  }}

  function startTimer() {{
    el("timer").textContent = fmt(seconds);
    timerInterval = setInterval(() => {{
      seconds--;
      el("timer").textContent = fmt(seconds);
      if(seconds <= 0) {{
        clearInterval(timerInterval);
        document.getElementById("submitBtn")?.click();
      }}
    }}, 1000);
  }}

  function fmt(s) {{
    const m = Math.floor(s/60);
    const sec = s%60;
    return `${{String(m).padStart(2,"0")}}:${{String(sec).padStart(2,"0")}}`;
  }}

  function saveQuizState() {{
    const state = {{
      current,
      answers,
      seconds,
      markedForReview: Array.from(markedForReview)
    }};
    localStorage.setItem(QUIZ_STATE_KEY, JSON.stringify(state));
  }}

  function normalizeMathForQuiz(container) {{
    if (!container) return;
    container.innerHTML = container.innerHTML
      .replace(/(\\S)\\s*\\$\\$(.+?)\\$\\*\\s*(\\S)/g, '$1 \\\\($2\\\\) $3')
      .replace(/\\$\\$(.+?)\\$\\$/g, function(match, math) {{
        if (/^<br>|<div>|<\/div>|<p>|<\/p>/.test(match)) return match;
        return '\\\\(' + math + '\\\\)';
      }});
  }}

  function renderMath() {{
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise();
  }}

  // Leaderboard: fetch all attempts and show top 10 with candidate name
  function showLeaderboard() {{
    db.ref("attempt_history/" + QUIZ_TITLE).once("value").then(snapshot => {{
      const data = snapshot.val();
      if (!data) {{
        document.getElementById("leaderboardBody").innerHTML = "<p>No attempts yet.</p>";
        document.getElementById("leaderboardModal").style.display = "flex";
        return;
      }}
      let attempts = [];
      Object.values(data).forEach(userAttempts => {{
        Object.values(userAttempts).forEach(a => attempts.push(a));
      }});
      // Sort by score descending, then time ascending
      attempts.sort((a,b) => b.score - a.score || a.timeTaken - b.timeTaken);
      // Take top 10
      const top10 = attempts.slice(0,10);
      let html = '';
      top10.forEach((a, idx) => {{
        // Use displayName if available, otherwise email
        const name = a.displayName || a.email || 'Anonymous';
        html += `<div class="leaderboard-entry" style="display:flex; justify-content:space-between; padding:8px; border-bottom:1px solid #eee;">
          <span>${{idx+1}}. ${{name}}</span>
          <span>Score: ${{a.score}} | Time: ${{fmt(a.timeTaken)}}</span>
        </div>`;
      }});
      document.getElementById("leaderboardBody").innerHTML = html;
      document.getElementById("leaderboardModal").style.display = "flex";
    }});
  }}

  // Submit quiz (no ads)
  function submitQuiz() {{
    clearInterval(timerInterval);
    const timeTaken = TOTAL_TIME_SECONDS - seconds;

    let correct = 0, wrong = 0, totalMarks = 0, maxMarks = 0;
    QUESTIONS.forEach(q => {{
      maxMarks += Number(q.correct_score ?? 1);
      const ans = answers[q.id ?? q];
      if (ans && String(ans) === String(q.answer)) {{
        correct++;
        totalMarks += Number(q.correct_score ?? 1);
      }} else if (ans) {{
        wrong++;
        totalMarks -= Number(q.negative_score ?? 0);
      }}
    }});
    const attempted = Object.keys(answers).length;
    const unattempted = QUESTIONS.length - attempted;
    const accuracy = attempted ? ((correct/attempted)*100).toFixed(1) : "0.0";

    // Get display name from currentUser
    const displayName = currentUser.displayName || currentUser.email || 'Anonymous';

    const payload = {{
      userId: currentUser.uid,
      email: currentUser.email,
      displayName: displayName,
      score: totalMarks,
      maxMarks,
      correct,
      wrong,
      unattempted,
      timeTaken,
      quizId: QUIZ_TITLE,
      submittedAt: Date.now(),
      answers: QUESTIONS.map((q, i) => {{
        const qid = q.id ?? i;
        return {{
          question: q.question,
          options: [q.option_1, q.option_2, q.option_3, q.option_4, q.option_5].filter(Boolean),
          correctAnswer: String(q.answer),
          userAnswer: answers[qid] || null
        }};
      }})
    }};

    // Save to Firebase
    db.ref(`attempt_history/${{QUIZ_TITLE}}/${{currentUser.uid}}/${{payload.submittedAt}}`).set(payload);

    // Directly show results
    displayResults(payload);
  }}

  function displayResults(payload) {{
    el("quizCard").style.display = "none";
    el("floatBar").style.display = "none";
    el("quizHeader").style.display = "none";

    let reviewHTML = `<div class="card"><h3 style="color:var(--accent)">Results Summary</h3>
      <div class="stats">
        <div class="stat"><h4>Score</h4><p>${{payload.score}} / ${{payload.maxMarks}}</p></div>
        <div class="stat"><h4>Correct</h4><p>${{payload.correct}}</p></div>
        <div class="stat"><h4>Wrong</h4><p>${{payload.wrong}}</p></div>
        <div class="stat"><h4>Unattempted</h4><p>${{payload.unattempted}}</p></div>
        <div class="stat"><h4>Accuracy</h4><p>${{accuracy}}%</p></div>
        <div class="stat"><h4>Time</h4><p>${{fmt(payload.timeTaken)}}</p></div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
        <button class="btn-ghost" onclick="filterResults('all')">ALL</button>
        <button class="btn-ghost" onclick="filterResults('correct')">CORRECT</button>
        <button class="btn-ghost" onclick="filterResults('wrong')">WRONG</button>
        <button class="btn-ghost" onclick="filterResults('unattempted')">UNATTEMPTED</button>
      </div>
    </div>`;

    QUESTIONS.forEach((q, i) => {{
      const qid = q.id ?? i;
      const ans = answers[qid];
      const isCorrect = ans && String(ans) === String(q.answer);
      const status = !ans ? "unattempted" : (isCorrect ? "correct" : "wrong");
      reviewHTML += `<div class="card result-q" data-status="${{status}}">`;
      reviewHTML += `<div style="font-weight:700; margin-bottom:8px">Q${{i+1}}: ${{q.question}}</div>`;
      ["option_1","option_2","option_3","option_4","option_5"].forEach((key, j) => {{
        if(!q[key]) return;
        const idx = j+1;
        const isOptCorrect = String(idx) === String(q.answer);
        const isUser = ans && String(idx) === String(ans);
        let style = "border:1px solid #eee; background:#fafbfd;";
        if(isOptCorrect) style = "border:2px solid var(--success); background:rgba(31,158,90,0.12);";
        else if(isUser && !isOptCorrect) style = "border:2px solid var(--danger); background:rgba(200,45,63,0.12);";
        reviewHTML += `<div style="padding:8px; margin:6px 0; border-radius:8px; ${{style}}">${{q[key]}}</div>`;
      }});
      const scoreText = isCorrect ? `+${{q.correct_score}}` : (ans ? `-${{q.negative_score}}` : "0");
      reviewHTML += `<div><strong>Score:</strong> ${{scoreText}}</div>`;
      if(q.solution_text) reviewHTML += `<div class="explanation" style="display:block; margin-top:8px"><strong>Explanation:</strong> ${{q.solution_text}}</div>`;
      reviewHTML += `</div>`;
    }});

    el("results").innerHTML = reviewHTML;
    normalizeMathForQuiz(el("results"));
    renderMath();
    el("results").style.display = "block";
    LAST_RESULT_HTML = reviewHTML;

    // Save result to localStorage
    const headerHTML = el("headerControls").innerHTML;
    localStorage.setItem(QUIZ_RESULT_KEY, JSON.stringify({{
      submitted: true,
      resultHTML: reviewHTML,
      headerHTML: headerHTML
    }}));

    // Update header with result controls
    const header = el("headerControls");
    header.innerHTML = `
      <div style="display:flex; align-items:center; gap:10px">
        <h1>${{QUIZ_TITLE}}</h1>
      </div>
      <div style="display:flex; gap:10px">
        <button class="btn" onclick="location.reload()">Re-Attempt</button>
        <button class="btn-ghost" onclick="showLeaderboard()">Leaderboard</button>
      </div>
    `;
  }}

  function filterResults(type) {{
    document.querySelectorAll(".result-q").forEach(card => {{
      card.style.display = (type === "all" || card.dataset.status === type) ? "block" : "none";
    }});
  }}

  function attachListeners() {{
    el("nextBtn").addEventListener("click", () => current < QUESTIONS.length-1 && renderQuestion(current+1));
    el("prevBtn").addEventListener("click", () => current > 0 && renderQuestion(current-1));
    el("clearBtn").addEventListener("click", () => {{
      delete answers[QUESTIONS[current].id ?? current];
      renderQuestion(current);
      highlightPalette();
    }});
    el("markReviewBtn").addEventListener("click", () => {{
      markedForReview.has(current) ? markedForReview.delete(current) : markedForReview.add(current);
      highlightPalette();
      saveQuizState();
    }});
    el("saveNextBtn").addEventListener("click", () => {{
      saveQuizState();
      if (current < QUESTIONS.length-1) renderQuestion(current+1);
    }});
    el("paletteBtn").addEventListener("click", (e) => {{
      e.stopPropagation();
      togglePalette();
    }});
    el("submitBtn").addEventListener("click", () => {{
      const attempted = Object.keys(answers).length;
      el("submitMsg").textContent = `You attempted ${{attempted}} of ${{QUESTIONS.length}}. Submit?`;
      el("submitModal").style.display = "flex";
    }});
    el("cancelSubmit").addEventListener("click", () => el("submitModal").style.display = "none");
    el("confirmSubmit").addEventListener("click", () => {{
      el("submitModal").style.display = "none";
      submitQuiz();
    }});
    el("modeToggle").addEventListener("click", () => {{
      el("modeToggle").classList.toggle("active");
      isQuiz = el("modeToggle").classList.contains("active");
      renderQuestion(current);
    }});
    el("leaderboardBtn").addEventListener("click", showLeaderboard);
    el("closeLeaderboard").addEventListener("click", () => {{
      document.getElementById("leaderboardModal").style.display = "none";
    }});
    document.addEventListener("click", (ev) => {{
      const pal = el("palette");
      if(pal && pal.style.display === "flex" && !pal.contains(ev.target) && ev.target !== el("paletteBtn") && !el("paletteBtn").contains(ev.target)) {{
        pal.style.display = "none";
      }}
    }});
  }}

  function buildPalette() {{
    const pal = el("palette");
    pal.innerHTML = "";
    for(let i=0; i<QUESTIONS.length; i++) {{
      const b = document.createElement("button");
      b.className = "qbtn";
      b.textContent = i+1;
      b.addEventListener("click", (e) => {{
        e.stopPropagation();
        renderQuestion(i);
        pal.style.display = "none";
      }});
      pal.appendChild(b);
    }}
    const summary = document.createElement("div");
    summary.id = "palette-summary";
    pal.appendChild(summary);
    highlightPalette();
  }}

  function highlightPalette() {{
    const pal = el("palette");
    if(!pal) return;
    const total = QUESTIONS.length;
    const attempted = Object.keys(answers).length;
    const marked = markedForReview.size;
    const notAttempted = total - attempted - marked;
    Array.from(pal.children).forEach((child, idx) => {{
      if(child.id === "palette-summary") return;
      child.classList.remove("attempted","unattempted","marked","current");
      const qid = QUESTIONS[idx].id ?? idx;
      if(idx === current) child.classList.add("current");
      else if(markedForReview.has(idx)) child.classList.add("marked");
      else if(answers[qid]) child.classList.add("attempted");
      else child.classList.add("unattempted");
    }});
    const summary = el("palette-summary");
    if(summary) {{
      summary.innerHTML = `Total: ${{total}} | <span style="color:var(--success)">Attempted: ${{attempted}}</span> | <span style="color:var(--danger)">Unattempted: ${{notAttempted}}</span> | <span style="color:var(--purple)">Marked: ${{marked}}</span>`;
    }}
  }}

  function togglePalette() {{
    const pal = el("palette");
    pal.style.display = pal.style.display === "flex" ? "none" : "flex";
    if(pal.style.display === "flex") highlightPalette();
  }}

  function rebindResultHeaderActions() {{
    // Handlers for result header buttons (if needed)
  }}

  // Disable copy protection (optional – keep as in original)
  document.addEventListener("contextmenu", e => e.preventDefault());
  document.addEventListener("keydown", function(e) {{
    if ((e.ctrlKey || e.metaKey) && ["c","x","v","a","s","p","u"].includes(e.key.toLowerCase())) e.preventDefault();
  }});

  window.addEventListener("DOMContentLoaded", () => {{
    document.getElementById('loadingMessage').style.display = 'block';
  }});
</script>
</head>
<body>
<!-- Simple loading indicator -->
<div id="loadingMessage" style="text-align:center; margin-top:50px; font-size:18px;">Checking authentication...</div>

<!-- QUIZ HEADER (hidden initially) -->
<header id="quizHeader" style="display:none;">
  <div class="header-inner" id="headerControls">
    <div style="display:flex; align-items:center; gap:12px">
      <div class="toggle-pill" id="modeToggle"><span>Test</span><span>Quiz</span></div>
    </div>
    <div style="display:flex; align-items:center; gap:10px">
      <div class="timer-text" id="timer">00:00</div>
      <button id="submitBtn" class="btn">Submit</button>
      <button id="paletteBtn" class="btn-ghost">View</button>
      <button id="leaderboardBtn" class="btn-ghost">Leaderboard</button>
    </div>
  </div>
</header>

<!-- QUIZ CONTAINER (hidden initially) -->
<div id="quizContainer" class="container" style="display:none;">
  <div id="quizCard" class="card">
    <div class="qbar">
      <div class="qmeta">Question <span id="qindex">0</span> / <span id="qtotal">0</span></div>
      <div class="marking" id="marking"></div>
    </div>
    <div class="qtext" id="qtext"></div>
    <div class="options" id="options"></div>
    <div id="explanation" class="explanation"></div>
  </div>
  <div id="results" class="results" style="display:none;"></div>
</div>

<!-- BOTTOM NAVIGATION (hidden initially) -->
<div class="fbar" id="floatBar" style="display:none;">
  <div class="fbar-inner">
    <button id="prevBtn">← Prev</button>
    <button id="markReviewBtn" class="btn-mark">Mark for Review</button>
    <button id="clearBtn" class="btn-clear">Clear</button>
    <button id="saveNextBtn" class="btn-save">Save & Next</button>
    <button id="nextBtn">Next →</button>
  </div>
</div>

<!-- SUBMIT MODAL -->
<div id="submitModal" class="modal">
  <div class="modal-content">
    <h3>Submit quiz?</h3>
    <p id="submitMsg">You attempted X/Y. Are you sure you want to submit?</p>
    <div class="actions">
      <button id="cancelSubmit" class="btn-ghost">Cancel</button>
      <button id="confirmSubmit" class="btn-primary">Submit</button>
    </div>
  </div>
</div>

<!-- LEADERBOARD MODAL -->
<div id="leaderboardModal" class="modal">
  <div class="modal-content" style="max-width:600px; text-align:left;">
    <h3>Top 10 Leaderboard</h3>
    <div id="leaderboardBody" style="max-height:400px; overflow-y:auto;"></div>
    <button id="closeLeaderboard" class="btn" style="margin-top:12px;">Close</button>
  </div>
</div>

<!-- PALETTE -->
<div id="palette" aria-hidden="true"></div>

</body>
</html>"""
    
    # Generate QUESTIONS array
    questions_js = "[\n"
    for i, q in enumerate(quiz_data["questions"]):
        q["id"] = str(50000 + i + 1)
        q["quiz_id"] = quiz_data["name"]
        q["correct_score"] = str(quiz_data.get("marks", "3"))
        q["negative_score"] = str(quiz_data.get("negative", "1"))
        q_str = json.dumps(q, ensure_ascii=False)
        questions_js += q_str + ",\n"
    questions_js = questions_js.rstrip(",\n") + "\n]"
    
    seconds = int(quiz_data.get("time", "25")) * 60
    
    html = template.format(
        quiz_name=quiz_data["name"],
        questions_array=questions_js,
        seconds=seconds
    )
    
    return html

# Bot Handlers (keep all existing handlers from deepseek file)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    update_activity()
    await update.message.reply_text(
        "📚 *Quiz Generator Bot*\n\n"
        "Send me a TXT file with questions in any of these formats:\n\n"
        "*Format 1:*\n"
        "1. Question in English\n"
        "   Question in Hindi\n"
        "a) Option 1 English\n"
        "   Option 1 Hindi\n"
        "b) Option 2 English\n"
        "   Option 2 Hindi\n"
        "Correct option:-a\n"
        "ex: Explanation text...\n\n"
        "*Format 2:*\n"
        "Q.1 Question in English\n"
        "Question in Hindi\n"
        "(a) Option 1 English\n"
        "Option 1 Hindi\n"
        "(b) Option 2 English\n"
        "Option 2 Hindi\n"
        "Answer: (a)\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/help - Show help\n"
        "/wake - Keep the bot awake\n"
        "/status - Check bot status\n"
        "/cancel - Cancel current operation",
        parse_mode="Markdown"
    )
    return GETTING_FILE

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle TXT file upload"""
    update_activity()
    
    try:
        document = update.message.document
        user_id = update.effective_user.id
        
        # Check if it's a text file
        if not document.mime_type == 'text/plain' and not document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Please send a text file (.txt)")
            return GETTING_FILE
        
        # Download the file
        await update.message.reply_text("📥 Processing your quiz file...")
        
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        content = file_content.decode('utf-8')
        
        # Parse questions
        questions = parse_txt_file(content)
        
        if not questions:
            await update.message.reply_text("❌ Could not parse any questions from the file. Please check the format.")
            return GETTING_FILE
        
        # Store in context
        context.user_data["questions"] = questions
        context.user_data["file_name"] = document.file_name
        
        await update.message.reply_text(f"✅ Parsed {len(questions)} questions successfully!\n\nNow enter the quiz name:")
        return GETTING_QUIZ_NAME
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text(f"❌ Error reading file: {str(e)}")
        return GETTING_FILE

async def get_quiz_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get quiz name"""
    update_activity()
    context.user_data["name"] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("15 min", callback_data="15"),
         InlineKeyboardButton("20 min", callback_data="20"),
         InlineKeyboardButton("25 min", callback_data="25"),
         InlineKeyboardButton("30 min", callback_data="30")],
        [InlineKeyboardButton("Custom", callback_data="custom")]
    ]
    
    await update.message.reply_text(
        "⏱️ Select quiz time (or choose Custom to enter minutes):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_TIME

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle time selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom":
        await query.edit_message_text("Enter time in minutes:")
        return GETTING_TIME
    
    context.user_data["time"] = query.data
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"),
         InlineKeyboardButton("2", callback_data="2"),
         InlineKeyboardButton("3", callback_data="3"),
         InlineKeyboardButton("4", callback_data="4")],
        [InlineKeyboardButton("Custom", callback_data="custom_marks")]
    ]
    
    await query.edit_message_text(
        "✍️ Select marks per question (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_MARKS

async def get_time_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom time input"""
    update_activity()
    try:
        time_minutes = int(update.message.text)
        if time_minutes <= 0:
            raise ValueError
        context.user_data["time"] = str(time_minutes)
    except:
        await update.message.reply_text("Please enter a valid number (minutes):")
        return GETTING_TIME
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"),
         InlineKeyboardButton("2", callback_data="2"),
         InlineKeyboardButton("3", callback_data="3"),
         InlineKeyboardButton("4", callback_data="4")],
        [InlineKeyboardButton("Custom", callback_data="custom_marks")]
    ]
    
    await update.message.reply_text(
        "✍️ Select marks per question (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_MARKS

async def get_marks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle marks selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom_marks":
        await query.edit_message_text("Enter marks per question:")
        return GETTING_MARKS
    
    context.user_data["marks"] = query.data
    
    keyboard = [
        [InlineKeyboardButton("0 (No negative)", callback_data="0"),
         InlineKeyboardButton("0.25", callback_data="0.25"),
         InlineKeyboardButton("0.5", callback_data="0.5"),
         InlineKeyboardButton("1", callback_data="1")],
        [InlineKeyboardButton("Custom", callback_data="custom_negative")]
    ]
    
    await query.edit_message_text(
        "⚠️ Select negative marking per wrong answer (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_NEGATIVE

async def get_marks_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom marks input"""
    update_activity()
    try:
        marks = float(update.message.text)
        if marks <= 0:
            raise ValueError
        context.user_data["marks"] = str(marks)
    except:
        await update.message.reply_text("Please enter a valid number for marks:")
        return GETTING_MARKS
    
    keyboard = [
        [InlineKeyboardButton("0 (No negative)", callback_data="0"),
         InlineKeyboardButton("0.25", callback_data="0.25"),
         InlineKeyboardButton("0.5", callback_data="0.5"),
         InlineKeyboardButton("1", callback_data="1")],
        [InlineKeyboardButton("Custom", callback_data="custom_negative")]
    ]
    
    await update.message.reply_text(
        "⚠️ Select negative marking per wrong answer (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_NEGATIVE

async def get_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle negative marking selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom_negative":
        await query.edit_message_text("Enter negative marking value:")
        return GETTING_NEGATIVE
    
    context.user_data["negative"] = query.data
    
    await query.edit_message_text("🏆 Enter creator name:")
    return GETTING_CREATOR

async def get_negative_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom negative marking input"""
    update_activity()
    try:
        negative = float(update.message.text)
        if negative < 0:
            raise ValueError
        context.user_data["negative"] = str(negative)
    except:
        await update.message.reply_text("Please enter a valid number for negative marking:")
        return GETTING_NEGATIVE
    
    await update.message.reply_text("🏆 Enter creator name:")
    return GETTING_CREATOR

async def get_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get creator name and generate quiz"""
    update_activity()
    context.user_data["creator"] = update.message.text
    
    # Show summary with proper format
    total_questions = len(context.user_data["questions"])
    summary = (
        "📋 *Quiz Summary*\n\n"
        f"📘 QUIZ ID: {context.user_data['name']}\n"
        f"📊 TOTAL QUESTIONS: {total_questions}\n"
        f"⏱️ TIME: {context.user_data['time']} Minutes\n"
        f"✍️ EACH QUESTION MARK: {context.user_data['marks']}\n"
        f"⚠️ NEGATIVE MARKING: {context.user_data['negative']}\n"
        f"🏆 CREATED BY: {context.user_data['creator']}\n\n"
        "🔄 Generating quiz HTML..."
    )
    
    progress_msg = await update.message.reply_text(summary, parse_mode="Markdown")
    
    # Generate progress bar
    user_id = update.effective_user.id
    user_progress[user_id] = progress_msg.message_id
    
    # Generate HTML
    try:
        # Update progress
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=progress_msg.message_id,
            text=f"{summary}\n\n🔄 Processing {total_questions} questions..."
        )
        
        # Generate quiz HTML
        html_content = generate_html_quiz(context.user_data)
        
        # Save HTML file
        safe_name = re.sub(r'[^\w\s-]', '', context.user_data['name'])
        safe_name = re.sub(r'[-\s]+', '_', safe_name)
        html_file = f"{safe_name}.html"
        
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        # Update progress
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=progress_msg.message_id,
            text=f"{summary}\n\n✅ Quiz generated! Sending file..."
        )
        
        # Send HTML file with proper caption format
        with open(html_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=html_file,
                caption=f"✅ *Quiz Generated Successfully!*\n\n"
                       f"Download and open in any browser.\n\n"
                       f"📘 QUIZ ID: {context.user_data['name']}\n"
                       f"📊 TOTAL QUESTIONS: {total_questions}\n"
                       f"⏱️ TIME: {context.user_data['time']} Minutes\n"
                       f"✍️ EACH QUESTION MARK: {context.user_data['marks']}\n"
                       f"⚠️ NEGATIVE MARKING: {context.user_data['negative']}\n"
                       f"🏆 CREATED BY: {context.user_data['creator']}",
                parse_mode="Markdown"
            )
        
        # Cleanup
        os.remove(html_file)
        if user_id in user_progress:
            del user_progress[user_id]
        
        # Clear user data
        context.user_data.clear()
        
    except Exception as e:
        logger.error(f"Error generating quiz: {e}")
        await update.message.reply_text(f"❌ Error generating quiz: {str(e)}")
        
        if user_id in user_progress:
            del user_progress[user_id]
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation"""
    update_activity()
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    update_activity()
    help_text = """
📚 *Quiz Generator Bot Help*

*Commands:*
/start - Start creating a new quiz
/help - Show this help message
/wake - Keep the bot awake
/status - Check bot status
/cancel - Cancel current operation

*Supported File Formats:*
Format 1:
1. Question text in English
   Question in Hindi
a) Option 1 English
   Option 1 Hindi
b) Option 2 English
   Option 2 Hindi
Correct option:-a
ex: Explanation text...

Format 2:
Q.1 Question text in English
Question in Hindi
(a) Option 1 English
Option 1 Hindi
(b) Option 2 English
Option 2 Hindi
Answer: (a)

*Features:*
• Interactive quiz interface with watermark
• Student name input (required)
• Timer with countdown
• Test/Quiz mode toggle
• Rank and percentile system
• Previous attempts tracking
• Firebase integration
• Mobile responsive design
• Hindi/English bilingual support
• Mark for review feature
• Question palette with colors
• Tamper protection

*No Sleep System:* 
This bot has an integrated keep-alive system that prevents it from sleeping on platforms like Render.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual wake command"""
    update_activity()
    keep_alive_ping()
    await update.message.reply_text("🔔 Bot is awake and active!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot status"""
    update_activity()
    status_text = (
        f"🤖 *Bot Status*\n\n"
        f"• Status: ✅ Running\n"
        f"• Last activity: {datetime.fromtimestamp(last_activity).strftime('%Y-%m-d %H:%M:%S')}\n"
        f"• Active users: {len(user_data)}\n"
        f"• Active processes: {len(user_progress)}\n"
        f"• Render URL: {RENDER_APP_URL if RENDER_APP_URL else 'Not set'}\n"
        f"• Keep-alive interval: {KEEP_ALIVE_INTERVAL//60} minutes\n\n"
        f"*Commands:*\n"
        f"/start - Create new quiz\n"
        f"/help - Show help\n"
        f"/wake - Force wake-up\n"
        f"/status - This status"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    error = context.error
    if "terminated by other getUpdates request" in str(error):
        logger.warning("Another bot instance is running. This is normal during deployment.")
        return
    logger.error(f"Update {update} caused error {error}")

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return
    
    # Start health server in a separate thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Start keep-alive worker if RENDER_APP_URL is set
    if RENDER_APP_URL:
        keep_alive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
        keep_alive_thread.start()
        logger.info("Keep-alive worker started")
    else:
        logger.warning("RENDER_APP_URL not set - keep-alive disabled")
    
    # Create and configure bot application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    
    # Create conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            GETTING_FILE: [
                MessageHandler(filters.Document.FileExtension("txt"), handle_document),
                CommandHandler("cancel", cancel)
            ],
            GETTING_QUIZ_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_quiz_name),
                CommandHandler("cancel", cancel)
            ],
            GETTING_TIME: [
                CallbackQueryHandler(get_time, pattern="^(15|20|25|30|custom)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_time_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_MARKS: [
                CallbackQueryHandler(get_marks, pattern="^(1|2|3|4|custom_marks)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_marks_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_NEGATIVE: [
                CallbackQueryHandler(get_negative, pattern="^(0|0.25|0.5|1|custom_negative)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_negative_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_CREATOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_creator),
                CommandHandler("cancel", cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("wake", wake_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_error_handler(error_handler)
    
    logger.info("Quiz Generator Bot is starting...")
    
    # Start the bot
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        time.sleep(10)
        logger.info("Retrying to start bot...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

if __name__ == '__main__':
    main()
