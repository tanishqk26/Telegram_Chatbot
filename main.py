from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from pymongo import MongoClient
import google.generativeai as genai
from datetime import datetime
import io
import re
import os
import requests
from dotenv import load_dotenv
from PIL import Image
from pdf2image import convert_from_bytes
from fastapi import FastAPI
import uvicorn
from threading import Thread

# Initialize FastAPI
app = FastAPI()

# Load environment variables
load_dotenv()

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db.users
chat_collection = db.chat_history
file_metadata_collection = db.file_metadata  # Collection for file metadata
web_search_collection = db.web_search_history  # Collection for web search history

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Google Custom Search API
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CX = os.getenv("CX")

# Configure the Gemini API
genai.configure(api_key=os.getenv("GENAI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# FastAPI route
@app.get("/")
async def home():
    return {"message": "Welcome to the Telegram Bot FastAPI Server!"}


async def websearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = " ".join(context.args)

    if not user_input:
        await update.message.reply_text("Please provide a search query after /websearch.")
        return

    try:
        search_results = perform_web_search(user_input)

        if not search_results:
            await update.message.reply_text("Sorry, no results found for your query.")
        else:
            await update.message.reply_text(f"Here are the top results for your query:\n\n{search_results}")
            
            # Save search query and results in MongoDB
            web_search_collection.insert_one({
                "chat_id": update.message.chat_id,
                "user_input": user_input,
                "search_results": search_results,
                "timestamp": datetime.now()
            })
    except Exception as e:
        await update.message.reply_text(f"Sorry, there was an error: {e}")


def perform_web_search(query):
    url = f"https://www.googleapis.com/customsearch/v1?q={query}&key={GOOGLE_API_KEY}&cx={CX}"
    response = requests.get(url)
    search_data = response.json()

    if "items" in search_data:
        results = search_data["items"][:3]
        return "\n".join([f"{item['title']}\n{item['link']}" for item in results])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    first_name = update.message.chat.first_name
    username = update.message.chat.username

    # Check if user exists
    user = users_collection.find_one({"chat_id": chat_id})
    if user:
        await update.message.reply_text("You're already registered! Send me a message to start chatting.")
        return

    # Save new user to MongoDB
    users_collection.insert_one({
        "chat_id": chat_id,
        "first_name": first_name,
        "username": username,
        "phone_number": None
    })

    button = [[KeyboardButton("Register", request_contact=True)]]
    reply_markup = ReplyKeyboardMarkup(button, one_time_keyboard=True)
    await update.message.reply_text("Welcome! Please register yourself:", reply_markup=reply_markup)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        chat_id = update.message.chat_id
        phone_number = update.message.contact.phone_number

        users_collection.update_one({"chat_id": chat_id}, {"$set": {"phone_number": phone_number}})
        await update.message.reply_text("Thank you! You can access the chatbot now. Type 'Hello' to start chatting.")
    else:
        await update.message.reply_text("Please register using the button.")


def get_gemini_response(input, image):
    response = model.generate_content(["Describe the given image", image])
    return clean_markdown(response.text)


def clean_markdown(text):
    return re.sub(r"[*_]+", "", text)


async def gemini_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            image_bytes = io.BytesIO(await photo_file.download_as_bytearray())

            img = Image.open(image_bytes)
            description = get_gemini_response("Describe the given image.", img)

            file_metadata_collection.insert_one({
                "chat_id": chat_id,
                "file_type": "image",
                "file_name": f"{photo_file.file_id}.jpg",
                "file_description": description,
                "timestamp": datetime.now()
            })

            await update.message.reply_text(f"Description of the image: {description}")
            return

        elif update.message.document:
            document = update.message.document
            mime_type = document.mime_type

            if mime_type == "application/pdf":
                pdf_file = document
                pdf_file_obj = await pdf_file.get_file()
                pdf_bytes = io.BytesIO(await pdf_file_obj.download_as_bytearray())

                images = convert_from_bytes(pdf_bytes.read(), dpi=200)

                if images:
                    img = images[0]
                    description = get_gemini_response("Describe the given image.", img)

                    file_metadata_collection.insert_one({
                        "chat_id": chat_id,
                        "file_type": "pdf",
                        "file_name": f"{pdf_file.file_id}.pdf",
                        "file_description": description,
                        "timestamp": datetime.now()
                    })

                    await update.message.reply_text(f"Description of the first page of the PDF: {description}")
                return
            else:
                await update.message.reply_text("Sorry, I can only process PDF files.")
                return

        user_input = update.message.text if update.message.text else ""

        if user_input:
            response = model.generate_content([user_input])
            bot_response = response.text

            max_length = 4000
            for i in range(0, len(bot_response), max_length):
                await update.message.reply_text(bot_response[i:i + max_length])

            chat_collection.insert_one({
                "chat_id": chat_id,
                "user_message": user_input,
                "bot_response": bot_response,
                "timestamp": datetime.now()
            })

    except Exception as e:
        await update.message.reply_text("Sorry, I couldn't process your request. Please try again later.")


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("websearch", websearch))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gemini_chat))
    application.add_handler(MessageHandler(filters.PHOTO, gemini_chat))
    application.add_handler(MessageHandler(filters.Document.ALL, gemini_chat))

    def run_fastapi():
        uvicorn.run(app, host="0.0.0.0", port=8080)

    # Start FastAPI server in a separate thread
    fastapi_thread = Thread(target=run_fastapi)
    fastapi_thread.start()

    # Run Telegram bot
    application.run_polling()


if __name__ == "__main__":
    main()
