import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.cloud import discoveryengine_v1

# -----------------------------------------------------------------------------
# Load environment variables
# -----------------------------------------------------------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
LOCATION = os.getenv("GCP_LOCATION", "global")
ENGINE_ID = os.getenv("GCP_ENGINE_ID")  # Use Engine ID from your GCP URL
SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# Validate environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN not found in .env")
if not PROJECT_ID:
    raise ValueError("❌ GCP_PROJECT_ID not found in .env")
if not ENGINE_ID:
    raise ValueError("❌ GCP_ENGINE_ID not found in .env")
if not SERVICE_ACCOUNT_PATH or not os.path.exists(SERVICE_ACCOUNT_PATH):
    raise ValueError("❌ GOOGLE_APPLICATION_CREDENTIALS is missing or file not found")

# Make GCP creds visible to SDK
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_PATH

# -----------------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Telegram bot handlers
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.username or update.effective_user.first_name
    logger.info("👋 User %s started the bot", user)
    await update.message.reply_text("Hi! 👋 Send me your question and I'll search Discovery Engine for you.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user = update.effective_user.username or update.effective_user.first_name
    print(f"📝 User query: {user_input}")

    client = discoveryengine_v1.SearchServiceClient()

    serving_config = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/"
        f"engines/{ENGINE_ID}/servingConfigs/default_search"
    )
    # print(f"⚙️ Using serving_config: {serving_config}")

    request = discoveryengine_v1.SearchRequest(
        serving_config=serving_config,
        query=user_input,
        page_size=5,
    )

    try:
        response = client.search(request=request)
        print("✅ Search API call successful")
        print("📦 RAW RESPONSE START")
        for r in response:
            print(r)
        print("📦 RAW RESPONSE END")

        results = []
        for idx, result in enumerate(response):
            doc = result.document
            title = link = None

            # Safely get fields if derived_struct_data exists
            if doc and hasattr(doc, "derived_struct_data") and doc.derived_struct_data:
                fields = getattr(doc.derived_struct_data, "fields", None)
                if fields:
                    # Convert MapComposite to dict safely
                    try:
                        field_dict = {f.key: getattr(f.value, "string_value", "") for f in fields}
                        title = field_dict.get("title") or field_dict.get("htmlTitle")
                        link = field_dict.get("link")
                    except Exception as e:
                        print(f"⚠️ Field parsing error: {e}")

            # Fallback if no title/link found
            if not title:
                title = "No Title"
            if not link:
                link = "No Link"

            results.append(f"🔹 {title}\n🌐 {link}")
            print(f"✨ Result {idx+1}: {title} | {link}")

        if not results:
            reply_text = "❌ Sorry, I couldn’t find anything."
        else:
            reply_text = "\n\n".join(results)

    except Exception as e:
        reply_text = f"⚠️ Error occurred: {str(e)}"
        print(f"🚨 ERROR: {e}")

    await update.message.reply_text(reply_text)




# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot is running... waiting for messages")
    app.run_polling()


if __name__ == "__main__":
    main()


# import os
# import logging
# from dotenv import load_dotenv
# from telegram import Update
# from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
# from google.cloud import discoveryengine_v1

# # Load environment variables
# load_dotenv()
# TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# PROJECT_ID = os.getenv("GCP_PROJECT_ID")
# LOCATION = os.getenv("GCP_LOCATION", "global")
# ENGINE_ID = os.getenv("GCP_ENGINE_ID")  # 👈 Use ENGINE instead of DATA_STORE
# SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# # Make sure GCP creds are visible to SDK
# os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_PATH

# logging.basicConfig(level=logging.INFO)

# async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await update.message.reply_text("Hi! 👋 Send me your question and I'll search Discovery Engine for you.")

# async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_input = update.message.text
#     print(f"📝 User query: {user_input}")

#     client = discoveryengine_v1.SearchServiceClient()

#     # ✅ Correct serving_config path (engine instead of datastore)
#     serving_config = (
#         f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/"
#         f"engines/{ENGINE_ID}/servingConfigs/default_search"
#     )
#     print(f"⚙️ Using serving_config: {serving_config}")

#     request = discoveryengine_v1.SearchRequest(
#         serving_config=serving_config,
#         query=user_input,
#         page_size=3,
#     )

#     try:
#         response = client.search(request=request)
#         print("✅ Search API call successful")

#         results = []
#         for idx, result in enumerate(response):
#             if result.document and result.document.content:
#                 content = result.document.content
#                 results.append(content)
#                 print(f"📄 Result {idx+1}: {content}")

#         if not results:
#             reply_text = "❌ Sorry, I couldn’t find anything."
#         else:
#             reply_text = "\n\n".join(results)

#     except Exception as e:
#         reply_text = f"⚠️ Error occurred: {str(e)}"
#         print(f"🚨 ERROR: {e}")

#     await update.message.reply_text(reply_text)

# def main():
#     app = Application.builder().token(TELEGRAM_TOKEN).build()
#     app.add_handler(CommandHandler("start", start))
#     app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
#     print("🤖 Bot is running... waiting for messages")
#     app.run_polling()

# if __name__ == "__main__":
#     main()
