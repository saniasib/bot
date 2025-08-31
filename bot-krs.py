import requests
from bs4 import BeautifulSoup
import re
import logging
import time
import threading
from urllib.parse import urljoin
import copy
from telegram.error import BadRequest
from telegram import ParseMode, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext,
)

# --- CONFIGURATION ---
TELEGRAM_TOKEN = '8047544635:AAFNz6CDsn0hnbIFHskUt54JJVifYzMbDE8'

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- NEW STATES FOR CONVERSATION HANDLER ---
(
    LOGIN_USERNAME,
    LOGIN_PASSWORD,
    ADD_COURSE_CODE,
    ADD_COURSE_NAME,
    ADD_COURSE_CLASS,
    ASK_NEXT_ACTION,
) = range(6)

# --- USER SESSION STORAGE ---
user_sessions = {}
user_message_history = {}  # <- New addition


# ==============================================================================
# CORE CLASS: SIAScraper (With Multi-Course Modification)
# ==============================================================================
class SIAScraper:
    def __init__(self, username, password, chat_id, user_id, context: CallbackContext):
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        }
        self.base_url = "https://sia.uty.ac.id"
        self.username = username
        self.password = password
        self.chat_id = chat_id
        self.user_id = user_id
        self.context = context
        self.monitor_msg_id = None
        self.krs_add_page_link = None
        self.last_sent_text = "" # <-- ADD THIS LINE

    # REPLACE WITH THIS FUNCTION
    # Di dalam kelas SIAScraper

    def send_or_edit_msg(self, text, parse_mode="Markdown"):
        """Mengirim atau mengedit pesan dengan mekanisme retry yang tangguh."""
        max_retries = 3  # Coba maksimal 3 kali
        for attempt in range(max_retries):
            try:
                if self.monitor_msg_id:
                    # Coba edit pesan
                    self.context.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.monitor_msg_id,
                        text=text,
                        parse_mode=parse_mode,
                        timeout=20 # Beri timeout sedikit lebih lama
                    )
                else:
                    # Coba kirim pesan baru
                    msg = self.context.bot.send_message(
                        chat_id=self.chat_id, text=text, parse_mode=parse_mode, timeout=20
                    )
                    self.monitor_msg_id = msg.message_id
                
                # Jika berhasil, keluar dari loop
                return

            except (BadRequest, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if 'Message is not modified' in str(e):
                    return # Jika pesan tidak berubah, anggap berhasil.
                
                if attempt + 1 == max_retries:
                    logger.error("All retry attempts failed. Giving up on this message.")
                    # Reset monitor_msg_id agar upaya berikutnya mengirim pesan baru
                    self.monitor_msg_id = None
                    return
                
                # Tunggu sebelum mencoba lagi (1s, 2s, 4s, ...)
                time.sleep(2 ** attempt)

    def _send_new_msg(self, text, parse_mode):
        try:
            msg = self.context.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=parse_mode
            )
            self.monitor_msg_id = msg.message_id
        except Exception as e:
            logger.error(f"Failed to send new message: {e}")

    def solve_captcha(self, text):
        numbers = re.findall(r"\d+", text)
        return str(int(numbers[0]) + int(numbers[1])) if len(numbers) == 2 else None

    def login(self):
        self.send_or_edit_msg("🔒 Attempting to log into SIA...")
        try:
            p = self.session.get(f"{self.base_url}/login", timeout=15)
            s = BeautifulSoup(p.text, "html.parser")
            cap_tag = s.find("p", style="color:white")
            if not cap_tag:
                self.send_or_edit_msg("❌ Failed to find captcha on the login page.")
                return False
            solution = self.solve_captcha(cap_tag.text.strip())
            if not solution:
                self.send_or_edit_msg("❌ Failed to solve captcha.")
                return False
            hidden_tags = {
                t["name"]: t.get("value", "")
                for t in s.find_all("input", type="hidden") if t.get("name")
            }
            payload = {"loginNipNim": self.username, "loginPsw": self.password, "mumet": solution, **hidden_tags}
            r = self.session.post(f"{self.base_url}/login", data=payload, timeout=15)
            if "home/keluar" in r.text:
                return True
            else:
                self.send_or_edit_msg("❌ Login Failed! Please double-check your NIM and Password.")
                return False
        except Exception as e:
            self.send_or_edit_msg(f"❌ Login failed, an error occurred: `{str(e)}`")
            return False

    def get_krs_add_link(self):
        try:
            r = self.session.get(f"{self.base_url}/std/krs/", timeout=15)
            s = BeautifulSoup(r.text, "html.parser")
            for a in s.find_all("a", class_="btn-primary"):
                if "tambah mk" in a.text.lower():
                    self.krs_add_page_link = urljoin(self.base_url, a["href"])
                    return self.krs_add_page_link
        except Exception:
            return None
        return None

    def attempt_registration(self, info, course):
        try:
            url = f"{self.base_url}/std/krslist/{info['key']}/true"
            payload = {"add": info["course_id"]}
            headers = {"X-Requested-With": "XMLHttpRequest", "Referer": self.krs_add_page_link}
            r = self.session.post(url, data=payload, headers=headers, timeout=15)

            if r.status_code == 200 and "sukses" in r.text.lower():
                # 🔹 Ambil info dosen dari kuesioner
                dosen_info = self.get_kuesioner_info(info["course_id"])

                if dosen_info:
                    success_msg = (
                        f"✅ *SUCCESS!* ✅\n\n"
                        f"The course *{course['name']} (Class {course['class']})* "
                        f"has been added to your KRS!\n\n"
                        f"👨‍🏫 Lecturer: *{dosen_info['dosen']}*"
                    )
                else:
                    success_msg = (
                        f"✅ *SUCCESS!* ✅\n\n"
                        f"The course *{course['name']} (Class {course['class']})* "
                        f"has been added to your KRS!"
                    )

                self.context.bot.send_message(self.chat_id, success_msg, parse_mode="Markdown")
                return True
            else:
                logger.warning(f"Failed to get course {course['name']}: {r.text.strip()}")
                return False
        except Exception as e:
            logger.error(f"Error during course registration {course['name']}: {e}")
            return False


    # FINAL VERSION: MORE ROBUST AND ACCURATE
    # REPLACE WITH THIS FINAL FUNCTION
    def monitor_courses(self, courses_to_monitor, interval=10):
        if not self.login():
            user_sessions.pop(self.user_id, None)
            return

        monitoring_list = copy.deepcopy(courses_to_monitor)
        course_statuses = {f"{c['code']}-{c['class']}": "Searching..." for c in monitoring_list}
        check_counter = 0

        while monitoring_list and not user_sessions.get(self.user_id, {}).get("stop_flag", False):
            check_counter += 1
            try:
                page_soup = None
                if self.get_krs_add_link():
                    r = self.session.get(self.krs_add_page_link, timeout=15)
                    page_soup = BeautifulSoup(r.text, "html.parser")
                else:
                    for key in course_statuses:
                        course_statuses[key] = "No KRS session"

                registered_in_this_cycle = False
                registered_courses = []

                if page_soup:
                    for course in monitoring_list:
                        course_key = f"{course['code']}-{course['class']}"
                        found_on_page = False

                        for row in page_soup.find_all("tr"):
                            tds = row.find_all("td")
                            if len(tds) < 7:
                                continue

                            row_code = tds[1].text.strip().upper()
                            row_cls = tds[6].text.strip().upper()

                            # ✅ Fokus pencocokan pada kode + kelas
                            if row_code == course['code'].upper() and row_cls == course['class'].upper():
                                found_on_page = True

                                if not row.find("label", string=re.compile(r"full", re.I)):
                                    take_button = row.find("button", class_="btn-success")
                                    if take_button and take_button.has_attr('onclick'):
                                        course_statuses[course_key] = "✅ SLOT AVAILABLE!"
                                        onclick = take_button["onclick"]
                                        match = re.search(r"setkeyin\s*\(\s*'([^']+)'.*?,\s*(\d+)", onclick)
                                        if match:
                                            info = {"key": match.group(1), "course_id": match.group(2)}
                                            if self.attempt_registration(info, course):
                                                registered_courses.append(course)
                                                registered_in_this_cycle = True
                                            else:
                                                course_statuses[course_key] = "🔴 Failed/Full"
                                    else:
                                        course_statuses[course_key] = "🤔 Available but locked"
                                else:
                                    course_statuses[course_key] = "🔴 Full"

                        if not found_on_page:
                            course_statuses[course_key] = "🧐 Not found"

                    if registered_courses:
                        monitoring_list = [c for c in monitoring_list if c not in registered_courses]

                # --- Update pesan ---
                from datetime import datetime
                timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
                msg_header = f"🚀 *Monitoring {len(monitoring_list)} Courses...* | Check #{check_counter}\n_Last checked: {timestamp}_\n\n"

                msg_body = ""
                for course in monitoring_list:
                    course_key = f"{course['code']}-{course['class']}"
                    status = course_statuses.get(course_key, "Searching...")
                    msg_body += f"- `{course['name']}` | Class `{course['class']}` | Status: *{status}*\n"

                final_text = msg_header + msg_body + "\n_Press /stop to halt._"

                if final_text != self.last_sent_text:
                    self.send_or_edit_msg(final_text)
                    self.last_sent_text = final_text

                if registered_in_this_cycle:
                    continue

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                self.send_or_edit_msg(f"An error occurred: `{e}`. Retrying...")

            time.sleep(interval)

        # --- keluar loop ---
        if self.user_id in user_sessions:
            final_msg = "🏁 *Monitoring Finished.*\n\n"
            if not monitoring_list:
                final_msg += "All target courses have been successfully acquired."
            else:
                final_msg += "Process stopped by the user."
            self.send_or_edit_msg(final_msg)
            user_sessions.pop(self.user_id, None)


        # Exiting the loop
        if self.user_id in user_sessions:
            final_msg = "🏁 *Monitoring Finished.*\n\n"
            if not monitoring_list:
                final_msg += "All target courses have been successfully acquired."
            else:
                final_msg += "Process stopped by the user."
            self.send_or_edit_msg(final_msg)
            user_sessions.pop(self.user_id, None)

    def get_kuesioner_info(self, value):
        """Get mata kuliah info dari halaman kuesioner"""
        try:
            kuesioner_url = f"{self.base_url}/std/kuesioner/{value}"
            response = self.session.get(kuesioner_url, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            mata_kuliah = soup.find("h3", class_="text-center")
            dosen = soup.find("h4", class_="text-center")

            if mata_kuliah and dosen:
                return {
                    "mata_kuliah": mata_kuliah.text.strip(),
                    "dosen": dosen.text.strip()
                }
            return None
        except Exception as e:
            logger.error(f"Error accessing kuesioner {value}: {e}")
            return None



# ==============================================================================
# TELEGRAM BOT HANDLERS (With New Flow)
# ==============================================================================

def start(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    if uid in user_sessions and user_sessions[uid].get('thread', threading.Thread()).is_alive():
        update.message.reply_text("Your monitoring session is still active. Use /stop first.")
        return ConversationHandler.END
    
    user_sessions[uid] = {"courses": []} # Initialize the list for courses
    msg = update.message.reply_text("👋 Welcome! Please enter your *NIM*:", parse_mode="Markdown")
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return LOGIN_USERNAME

def ask_pass(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    user_sessions[uid]['username'] = update.message.text.strip()
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    msg = update.message.reply_text("Enter your *SIA Password*:", parse_mode="Markdown")
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return LOGIN_PASSWORD

def ask_course_code(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    # Save the password if it's the first time, or if the user chose '➕ Add another course'
    if update.message.text != "➕ Add another course":
        user_sessions[uid]['password'] = update.message.text.strip()
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    
    # Remove the custom keyboard
    msg = update.message.reply_text("OK. Let's add a course.\n\nEnter the *Course Code* (e.g., TIF001):",
                                    parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardRemove())
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return ADD_COURSE_CODE

def ask_course_name(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    # Temporarily store the course code
    context.user_data['temp_course_code'] = update.message.text.strip()
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    msg = update.message.reply_text("Enter the *Course Name* (doesn't have to be exact):", parse_mode="Markdown")
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return ADD_COURSE_NAME

def ask_course_class(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    # Temporarily store the course name
    context.user_data['temp_course_name'] = update.message.text.strip()
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    msg = update.message.reply_text("Finally, enter the desired *Class* (e.g., A):", parse_mode="Markdown")
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return ADD_COURSE_CLASS

def ask_next_action(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    
    # Gather all course data and add it to the list
    new_course = {
        "code": context.user_data.get('temp_course_code'),
        "name": context.user_data.get('temp_course_name'),
        "class": update.message.text.strip()
    }
    user_sessions[uid]['courses'].append(new_course)
    
    # Delete previous messages
    if uid in user_message_history:
        for msg_id in user_message_history[uid]:
            try:
                context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except BadRequest:
                pass  # Ignore if the message is already deleted or not found
        user_message_history[uid] = []  # Clear the history after deletion

    # Display the list of added courses
    message = "✅ Course added successfully!\n\n*Your Target List:*\n"
    for idx, course in enumerate(user_sessions[uid]['courses']):
        message += f"{idx+1}. `{course['name']}` - Class `{course['class']}`\n"
        
    message += "\nWhat's next?"
    
    # Offer options to the user
    reply_keyboard = [['➕ Add another course'], ['🚀 Start Monitoring Now']]
    msg = update.message.reply_text(
        message,
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    user_message_history.setdefault(uid, []).append(msg.message_id)
    return ASK_NEXT_ACTION

def begin_monitor(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    session_data = user_sessions[uid]
    user_message_history.setdefault(uid, []).append(update.message.message_id)
    
    if not session_data.get("courses"):
        update.message.reply_text("You haven't added any courses. Process cancelled.", reply_markup=ReplyKeyboardRemove())
        user_sessions.pop(uid, None)
        return ConversationHandler.END

    # Delete previous messages
    if uid in user_message_history:
        for msg_id in user_message_history[uid]:
            try:
                context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except BadRequest:
                pass
        user_message_history.pop(uid, None)

    update.message.reply_text("Alright! I will start monitoring. The status message will appear shortly...", reply_markup=ReplyKeyboardRemove())
    session_data["stop_flag"] = False

    def run_monitoring_thread():
        d = user_sessions[uid]
        scraper = SIAScraper(d["username"], d["password"], update.effective_chat.id, uid, context)
        scraper.monitor_courses(d["courses"]) # Call the multi-course function

    thread = threading.Thread(target=run_monitoring_thread, daemon=True)
    session_data["thread"] = thread
    thread.start()

    return ConversationHandler.END

def stop(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    session = user_sessions.get(uid)
    if session and session.get('thread') and session['thread'].is_alive():
        msg = update.message.reply_text("⏳ Stopping the monitoring process...")
        session["stop_flag"] = True
        session['thread'].join(timeout=15)
        context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text="✅ *Monitoring successfully stopped.*", parse_mode="Markdown")
        if uid in user_sessions: user_sessions.pop(uid, None)
    else:
        update.message.reply_text("ℹ️ No active monitoring process found.")

def cancel(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    if uid in user_message_history:
        for msg_id in user_message_history[uid]:
            try:
                context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except BadRequest:
                pass
        user_message_history.pop(uid, None)
    if uid in user_sessions:
        user_sessions.pop(uid, None)
    update.message.reply_text("ℹ️ Process cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True, workers=4)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LOGIN_USERNAME: [MessageHandler(Filters.text & ~Filters.command, ask_pass)],
            LOGIN_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, ask_course_code)],
            ADD_COURSE_CODE: [MessageHandler(Filters.text & ~Filters.command, ask_course_name)],
            ADD_COURSE_NAME: [MessageHandler(Filters.text & ~Filters.command, ask_course_class)],
            ADD_COURSE_CLASS: [MessageHandler(Filters.text & ~Filters.command, ask_next_action)],
            ASK_NEXT_ACTION: [
                MessageHandler(Filters.regex('^➕ Add another course$'), ask_course_code),
                MessageHandler(Filters.regex('^🚀 Start Monitoring Now$'), begin_monitor)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("stop", stop)],
        conversation_timeout=600 # 10-minute timeout
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("stop", stop))

    logger.info("Multi-Course Bot is now running...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()