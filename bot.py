import os
import logging
import re
import requests
from typing import Optional
 
from langdetect import detect, DetectorFactory
DetectorFactory.seed = 0   # makes detection results consistent
 
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
 
# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
load_dotenv()
 
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
LOCATION = os.getenv("GCP_LOCATION", "global")
ENGINE_ID = os.getenv("GCP_ENGINE_ID")
 
# LibreTranslate endpoint (default)
TRANSLATE_URL = os.getenv("TRANSLATE_URL", "https://libretranslate.com")
 
# ----------------------------------------------------------------------------
 
# Validate environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN not found in .env")
if not PROJECT_ID:
    raise ValueError("❌ GCP_PROJECT_ID not found in .env")
if not ENGINE_ID:
    raise ValueError("❌ GCP_ENGINE_ID not found in .env")
 
# Configure Google Application Credentials
SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if not SERVICE_ACCOUNT_PATH or not os.path.exists(SERVICE_ACCOUNT_PATH):
    raise ValueError("❌ GOOGLE_APPLICATION_CREDENTIALS is missing or file not found")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_PATH
 
# ---------------------------------------------------------------------------
# Logging setup (console only)
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
 
# ---------------------------------------------------------------------------
# Helpers: Text chunking + HTML formatting (kept from your original file)
# ---------------------------------------------------------------------------
def chunk_text(text: str, limit: int = 4000):
    chunks = []
    while len(text) > limit:
        split_point = text.rfind('\n\n', 0, limit)
        if split_point == -1:
            split_point = text.rfind('\n', 0, limit)
        if split_point == -1:
            split_point = limit
        chunks.append(text[:split_point])
        text = text[split_point:].lstrip()
    chunks.append(text)
    return chunks
 
def _escape_html_basic(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
 
def format_for_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    bold_map = {}
    def _bold_repl(m):
        idx = len(bold_map)
        inner = m.group(1)
        inner_escaped = _escape_html_basic(inner)
        placeholder = f"__BOLD_PLACEHOLDER_{idx}__"
        bold_map[placeholder] = f"<b>{inner_escaped}</b>"
        return placeholder
    text = re.sub(r"\*\*(.+?)\*\*", _bold_repl, text, flags=re.DOTALL)
    text = text.replace("**", "")
    out_lines = []
    for raw_line in text.split("\n"):
        leading_spaces = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line.lstrip(" ")
        m = re.match(r'^([\*\u2022])\s*(.*)$', content)
        if m:
            inner = m.group(2)
            nbps = "\u00A0" * leading_spaces
            inner = _escape_html_basic(inner)
            out_lines.append(f"{nbps}• {inner}")
        else:
            nbps = "\u00A0" * leading_spaces
            escaped = _escape_html_basic(raw_line.lstrip(" "))
            out_lines.append(f"{nbps}{escaped}")
    result = "\n".join(out_lines)
    for ph, repl in bold_map.items():
        result = result.replace(ph, repl)
    return result
 
def strip_html_tags_for_plaintext(html_text: str) -> str:
    text = re.sub(r"</?b>", "", html_text)
    text = text.replace("\u00A0", " ")
    text = re.sub(r"<.*?>", "", text)
    return text
 
# ---------------------------------------------------------------------------
# Language detection: langdetect + script heuristics for many scripts
# ---------------------------------------------------------------------------
def detect_language(text: str) -> str:
    """Primary local detection with langdetect, safe fallback -> 'en'."""
    if not text or not text.strip():
        return "en"
    try:
        lang_code = detect(text)
        logger.info("langdetect returned: %s", lang_code)
        return lang_code
    except Exception as e:
        logger.warning("langdetect failed: %s", e)
        return "en"
 
def infer_language_from_script(text: str, detected: str) -> str:
    """
    Improve detection using Unicode script heuristics.
    Returns ISO 639-1 code when possible, fallback to 'en'.
    """
    if not text or not text.strip():
        return "en"
    t = text.strip()
 
    # Script presence checks
    if re.search(r'[\u0900-\u097F]', t):  # Devanagari
        # check for explicit language tokens
        if re.search(r'मराठी', t, re.IGNORECASE):
            return "mr"
        if re.search(r'हिन्दी|हिंदी', t, re.IGNORECASE):
            return "hi"
        return detected if detected != "en" else "hi"  # default to Hindi for Devanagari if uncertain
 
    if re.search(r'[\u0980-\u09FF]', t):  # Bengali
        return "bn"
    if re.search(r'[\u0A00-\u0A7F]', t):  # Gurmukhi (Punjabi)
        return "pa"
    if re.search(r'[\u0A80-\u0AFF]', t):  # Gujarati
        return "gu"
    if re.search(r'[\u0B00-\u0B7F]', t):  # Oriya (Odia)
        return "or"
    if re.search(r'[\u0B80-\u0BFF]', t):  # Tamil
        return "ta"
    if re.search(r'[\u0C00-\u0C7F]', t):  # Telugu
        return "te"
    if re.search(r'[\u0C80-\u0CFF]', t):  # Kannada
        return "kn"
    if re.search(r'[\u0D00-\u0D7F]', t):  # Malayalam
        return "ml"
    if re.search(r'[\u0400-\u04FF]', t):  # Cyrillic -> likely Russian
        return "ru"
    if re.search(r'[\u0600-\u06FF\u0750-\u077F]', t):  # Arabic script
        return "ar"
    if re.search(r'[\u3040-\u309F\u30A0-\u30FF]', t):  # Japanese Hiragana/Katakana
        return "ja"
    if re.search(r'[\u4E00-\u9FFF]', t):  # CJK Unified Ideographs -> Chinese (could be zh or ja/ko but zh is common)
        # Check for kana/kanji combos to differentiate Japanese (approx)
        if re.search(r'[\u3040-\u309F\u30A0-\u30FF]', t):
            return "ja"
        return "zh"
    if re.search(r'[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]', t):  # Hangul / Korean
        return "ko"
 
    # If detected by langdetect and it's not 'en', trust it
    if detected and detected != "en":
        return detected
 
    # final fallback
    return detected or "en"
 
# ---------------------------------------------------------------------------
# LibreTranslate helper
# ---------------------------------------------------------------------------
def libretranslate_translate(text: str, source: str, target: str) -> Optional[str]:
    """
    Translate text using LibreTranslate.
    source/target: ISO 639-1 code or 'auto' for source.
    Returns translated text or None on error.
    """
    try:
        payload = {
            "q": text,
            "source": source,
            "target": target,
            "format": "text"
        }
        resp = requests.post(f"{TRANSLATE_URL.rstrip('/')}/translate", data=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # LibreTranslate returns {"translatedText": "..."}
        translated = data.get("translatedText") or data.get("translated_text") or data.get("text")
        if translated is None:
            logger.warning("LibreTranslate returned unexpected shape: %s", data)
            return None
        return translated
    except Exception as e:
        logger.warning("LibreTranslate translation failed: %s", e)
        return None
 
# ---------------------------------------------------------------------------
# Utility: Decide whether text is 'in-language' (used to decide fallback)
# ---------------------------------------------------------------------------
def is_text_in_language(text: str, target_lang: str) -> bool:
    """
    Heuristic: run langdetect on response text and compare to target_lang.
    For short text, be conservative: require match or known alternate codes (e.g. 'zh-cn' variants).
    """
    if not text or not text.strip():
        return False
    try:
        resp_lang = detect(text)
        # Normalize resp_lang to first two chars
        resp_lang_short = resp_lang.split('-')[0]
        target_short = target_lang.split('-')[0]
        logger.info("Response language detected: %s (target was %s)", resp_lang_short, target_short)
        return resp_lang_short == target_short
    except Exception as e:
        logger.warning("Language detect on response failed: %s", e)
        return False
 
# ---------------------------------------------------------------------------
# Telegram bot handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.username or update.effective_user.first_name
    logger.info("👋 User %s started the bot", user)
    await update.message.reply_text(
        "Hi! 👋 Send me your question in any language and I'll reply in the same language."
    )
 
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text or ""
    logger.info("📝 User query: %s", user_input)
 
    thinking_msg = await update.message.reply_text("🤔 Please wait, I'm thinking...")
 
    # 1) detect language (langdetect + heuristics)
    local_detected = detect_language(user_input)
    detected_lang = infer_language_from_script(user_input, local_detected)
    logger.info("Final language code used for request: %s", detected_lang)
 
    # Prepare model preamble map for some languages (add more as required)
    preamble_map = {
        "en": "You are an advanced AI assistant. Provide a comprehensive, well-reasoned, and clearly structured answer. Use bullets where helpful.",
        "hi": "आप एक उन्नत एआई सहायक हैं। संक्षेप में और स्पष्ट रूप से उत्तर दें। जहाँ ज़रूरी हो बुलेट का उपयोग करें।",
        "mr": "आप एक प्रगत AI सहाय्यक आहात. सविस्तर व स्पष्ट उत्तर द्या. जिथे आवश्यक असते तिथे बुलेट वापरा.",
        "ar": "أنت مساعد ذكاء اصطناعي متقدم. قدم إجابة واضحة ومبنية بشكل جيد. استخدم النقاط عند الضرورة.",
        "ru": "Вы продвинутый AI-помощник. Предоставьте ясный и обоснованный ответ. Используйте список по необходимости.",
        "zh": "你是一个高级的 AI 助手。请提供结构清晰、理由充分的答案，必要时使用要点。",
        "ja": "あなたは高度なAIアシスタントです。構造的で分かりやすい回答を提供してください。必要に応じて箇条書きを使用してください。",
        "es": "Eres un asistente de IA avanzado. Proporciona una respuesta clara y bien razonada. Usa viñetas cuando sea necesario."
    }
    preamble = preamble_map.get(detected_lang, preamble_map["en"])
 
    # Set up client options
    client_options = (
        ClientOptions(api_endpoint=f"{LOCATION}-discoveryengine.googleapis.com")
        if LOCATION != "global" else None
    )
 
    # Create the client
    client = discoveryengine.ConversationalSearchServiceClient(client_options=client_options)
    serving_config = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/"
        f"engines/{ENGINE_ID}/servingConfigs/default_serving_config"
    )
 
    # Build the request: ask AI to answer in detected_lang
    request = discoveryengine.AnswerQueryRequest(
        serving_config=serving_config,
        query=discoveryengine.Query(text=user_input),
        user_pseudo_id=str(update.effective_user.id),
        answer_generation_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec(
            model_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.ModelSpec(
                model_version="gemini-2.5-flash/answer_gen/v1"
            ),
            prompt_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.PromptSpec(
                preamble=preamble
            ),
            include_citations=True,
            answer_language_code=detected_lang,
        ),
    )
 
    fallback_used = False
    final_answer_text = None
    final_answer_language = None
 
    try:
        # Try original-language request first
        logger.info("Sending AnswerQueryRequest with answer_language_code=%s", detected_lang)
        response = client.answer_query(request)
 
        # Extract answer
        if hasattr(response, "answer") and response.answer:
            answer_text = response.answer.answer_text or ""
        else:
            answer_text = ""
 
        # Decide if answer is acceptable (non-empty and in expected language)
        if answer_text and is_text_in_language(answer_text, detected_lang):
            logger.info("Received answer in expected language (%s).", detected_lang)
            final_answer_text = answer_text
            final_answer_language = detected_lang
        else:
            logger.info("Answer missing or not in expected language. Will attempt translation fallback.")
            # Use translation fallback: translate query -> English, query Discovery Engine in English, translate back
            fallback_used = True
 
            # Translate user query -> English. If detected_lang is 'en', skip translate.
            if detected_lang != "en":
                logger.info("Translating user query to English (source=%s)", detected_lang)
                translated_query = libretranslate_translate(user_input, source=detected_lang, target="en")
                if not translated_query:
                    # Try auto-detect as source
                    translated_query = libretranslate_translate(user_input, source="auto", target="en")
                if not translated_query:
                    logger.warning("Translation to English failed; will still try original query in English fallback.")
                    translated_query = user_input
            else:
                translated_query = user_input
 
            # Build English request
            eng_preamble = preamble_map.get("en")
            english_request = discoveryengine.AnswerQueryRequest(
                serving_config=serving_config,
                query=discoveryengine.Query(text=translated_query),
                user_pseudo_id=str(update.effective_user.id),
                answer_generation_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec(
                    model_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.ModelSpec(
                        model_version="gemini-2.5-flash/answer_gen/v1"
                    ),
                    prompt_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.PromptSpec(
                        preamble=eng_preamble
                    ),
                    include_citations=True,
                    answer_language_code="en",
                ),
            )
 
            logger.info("Sending fallback AnswerQueryRequest in English.")
            eng_response = client.answer_query(english_request)
 
            if hasattr(eng_response, "answer") and eng_response.answer:
                eng_answer_text = eng_response.answer.answer_text or ""
            else:
                eng_answer_text = ""
 
            if not eng_answer_text:
                logger.warning("English request returned empty answer. Using placeholder message.")
                final_answer_text = "⚠️ Sorry, I couldn't generate an answer."
                final_answer_language = detected_lang
            else:
                # Translate English answer back to user's language (if needed)
                if detected_lang != "en":
                    logger.info("Translating English answer back to %s", detected_lang)
                    translated_back = libretranslate_translate(eng_answer_text, source="en", target=detected_lang)
                    if translated_back:
                        final_answer_text = translated_back
                        final_answer_language = detected_lang
                    else:
                        logger.warning("Back-translation failed; returning English answer as fallback.")
                        final_answer_text = eng_answer_text
                        final_answer_language = "en"
                else:
                    final_answer_text = eng_answer_text
                    final_answer_language = "en"
 
    except Exception as e:
        logger.error("🚨 Error during answer_query or fallback process: %s", e)
        # Provide a user-visible error
        try:
            await thinking_msg.edit_text(f"⚠️ Error occurred while processing your request: {e}")
        except Exception:
            await update.message.reply_text(f"⚠️ Error occurred: {e}")
        return
 
    # Final safety: if still empty, send fallback
    if not final_answer_text:
        final_answer_text = "⚠️ No answer could be generated."
        final_answer_language = detected_lang
 
    # Log summary to console
    logger.info("---- Request summary ----")
    logger.info("User ID: %s", update.effective_user.id)
    logger.info("Original query: %s", user_input)
    logger.info("Detected language: %s", detected_lang)
    logger.info("Fallback used: %s", fallback_used)
    logger.info("Final answer language: %s", final_answer_language)
    logger.info("-------------------------")
 
    # Format for Telegram HTML and send
    formatted = format_for_html(final_answer_text)
    message_chunks = chunk_text(formatted)
    try:
        if message_chunks:
            first_chunk = message_chunks[0].strip()
            try:
                await thinking_msg.edit_text(first_chunk, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.warning("HTML parse/edit_text failed for first chunk: %s. Falling back to plain text.", e)
                await thinking_msg.edit_text(strip_html_tags_for_plaintext(first_chunk), parse_mode=None)
 
        for i, chunk in enumerate(message_chunks[1:], start=2):
            header = f"<b>Answer (Part {i} of {len(message_chunks)}):</b>\n"
            payload = header + chunk
            try:
                await update.message.reply_text(payload, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.warning("HTML reply failed for chunk %d: %s. Sending plain text fallback.", i, e)
                await update.message.reply_text(strip_html_tags_for_plaintext(payload), parse_mode=None)
    except Exception as e:
        logger.error("Failed to deliver message to user: %s", e)
        try:
            await update.message.reply_text(strip_html_tags_for_plaintext(final_answer_text))
        except Exception:
            logger.error("Failed to send even plain-text fallback.")
 
# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 Bot is running... waiting for messages")
    app.run_polling()
 
if __name__ == "__main__":
    main()