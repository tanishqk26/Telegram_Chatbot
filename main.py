from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from pymongo import MongoClient
import google.generativeai as genai
from datetime import datetime
import io
import re
import os
from dotenv import load_dotenv
from PIL import Image
from dotenv import load_dotenv
from datetime import datetime
import requests
from crewai import Agent, Task, Crew
from bs4 import BeautifulSoup
from flask import Flask
from threading import Thread

app = Flask(__name__)

# Render provides a PORT environment variable
port = int(os.environ.get("PORT", 8080))


@app.route('/')
def home():
    return "Welcome to the Telegram Bot Flask Server!"

load_dotenv()

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db.users
chat_collection = db.chat_history
file_metadata_collection = db.file_metadata  # New collection for file metadata

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CX = os.getenv("CX")

# Configure the Gemini API
genai.configure(api_key=os.getenv("GENAI_API_KEY"))

# Load the Gemini model
model = genai.GenerativeModel('gemini-1.5-flash')

web_search_collection = db.web_search_history  # New collection for web search history

async def websearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = " ".join(context.args)
    
    if not user_input:
        await update.message.reply_text("Please provide a search query after /websearch.")
        return

    # Perform web search
    try:
        search_results = perform_web_search(user_input)
        
        if not search_results:
            await update.message.reply_text("Sorry, no results found for your query.")
        else:
            await update.message.reply_text(f"Here are the top results for your query:\n\n{search_results}")
            
            # Save the query and response in MongoDB
            web_search_collection.insert_one({
                "chat_id": update.message.chat_id,
                "user_input": user_input,
                "search_results": search_results,
                "timestamp": datetime.now()
            })
    except Exception as e:
        await update.message.reply_text(f"Sorry, there was an error: {e}")


def perform_web_search(query):
    # Use Google Custom Search API to search for the query
    url = f"https://www.googleapis.com/customsearch/v1?q={query}&key={GOOGLE_API_KEY}&cx={CX}"
    response = requests.get(url)
    search_data = response.json()

    # Check if the search returns results
    if "items" in search_data:
        # Extract the top 3 search results
        results = search_data["items"][:3]
        search_summary = "\n".join([f"{item['title']}\n{item['link']}" for item in results])
        return search_summary
    return None



# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    first_name = update.message.chat.first_name
    username = update.message.chat.username

    # Check if user already exists
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

    # Request phone number
    button = [[KeyboardButton("Register", request_contact=True)]]
    reply_markup = ReplyKeyboardMarkup(button, one_time_keyboard=True)
    await update.message.reply_text("Welcome! Please register yourself:", reply_markup=reply_markup)

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        chat_id = update.message.chat_id
        phone_number = update.message.contact.phone_number

        # Save phone number in MongoDB
        users_collection.update_one({"chat_id": chat_id}, {"$set": {"phone_number": phone_number}})
        await update.message.reply_text(f"Thank you! You can access the chatbot now type 'Hello' to start chatting.")
    else:
        await update.message.reply_text("Please register using the button.")

def get_gemini_response(input, image):
    if input != "":
        response = model.generate_content(["Describe the given image", image])
    else:
        response = model.generate_content(image)
    return clean_markdown(response.text)



def clean_markdown(text):
    return re.sub(r"[*_]+", "", text)  # Removes *, **, ***, _


from pdf2image import convert_from_path, convert_from_bytes


async def gemini_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    print("Received a message.")  # Debugging message

    try:
        # If the user sent an image
        if update.message.photo:
            print("Processing image...")  # Debug: Image section triggered
            # Get the highest quality photo
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            image_bytes = io.BytesIO(await photo_file.download_as_bytearray())

            # Open the image using PIL
            img = Image.open(image_bytes)

            # Get the Gemini response (image description)
            description = get_gemini_response("Describe the given image.", img)

            # Save file metadata to MongoDB
            file_metadata_collection.insert_one({
                "chat_id": chat_id,
                "file_type": "image",
                "file_name": f"{photo_file.file_id}.jpg",  # Use file ID for unique name
                "file_description": description,
                "timestamp": datetime.now()
            })

            # Send response back to user with image description
            await update.message.reply_text(f"Description of the image: {description}")
            return  # Exit the function here, as the image has been handled

        # If the user sent a document
        elif update.message.document:
            document = update.message.document
            mime_type = document.mime_type
            print(f"Received document with MIME type: {mime_type}")  # Debug: Check MIME type

            # Check if the document is a PDF
            if mime_type == "application/pdf":
                print("Processing PDF document...")  # Debug: PDF section triggered

                # Download the PDF file
                pdf_file = document
                pdf_file_obj = await pdf_file.get_file()
                pdf_bytes = io.BytesIO(await pdf_file_obj.download_as_bytearray())

                # Debug: Check if the PDF file is downloaded properly
                print("PDF file downloaded successfully.")

                # Convert PDF to images (each page will be an image)
                # Convert PDF to images (each page will be an image)
                pdf_bytes_data = pdf_bytes.read()  # Read the bytes from the BytesIO object
                images = convert_from_bytes(pdf_bytes_data, dpi=200)  # Pass the byte data directly

                # Debug: Check how many pages were converted
                print(f"PDF converted to {len(images)} images.")


                if len(images) > 0:
                    # Process the first page of the PDF (or you can modify to process all pages)
                    img = images[0]

                    # Get the Gemini response (image description)
                    description = get_gemini_response("Describe the given image.", img)

                    # Debug: Check if Gemini returns a response
                    print(f"Gemini description: {description}")

                    # Save file metadata to MongoDB
                    file_metadata_collection.insert_one({
                        "chat_id": chat_id,
                        "file_type": "pdf",
                        "file_name": f"{pdf_file.file_id}.pdf",  # Use file ID for unique name
                        "file_description": description,
                        "timestamp": datetime.now()
                    })

                    # Send response back to user with PDF description
                    await update.message.reply_text(f"Description of the first page of the PDF: {description}")
                else:
                    print("No images generated from PDF.")

                return  # Exit the function here, as the PDF has been handled
            else:
                print(f"Received a document, but it's not a PDF. MIME type: {mime_type}")
                await update.message.reply_text("Sorry, I can only process PDF files.")
                return  # Exit the function if it's not a PDF

        # If the user sends text (non-image, non-PDF message)
        user_input = update.message.text if update.message.text else ""

        if user_input:
            print(f"Received text message: {user_input}")  # Debug: Log the text message

            # Use Gemini API to get the response for text
            response = model.generate_content([user_input])

            # Extract the generated text from the response
            bot_response = response.text

            # Reply to the user with the extracted response
            # Split the response into chunks of 4000 characters
            max_length = 4000
            for i in range(0, len(bot_response), max_length):
                await update.message.reply_text(bot_response[i:i + max_length])

            # Save chat history in MongoDB
            chat_collection.insert_one({
                "chat_id": chat_id,
                "user_message": user_input,
                "bot_response": bot_response,
                "timestamp": datetime.now()
            })

    except Exception as e:
        print(f"Error in processing message: {e}")
        await update.message.reply_text("Sorry, I couldn't process your request. Please try again later.")



# Main function
def main():
    # Create the application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("websearch", websearch))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gemini_chat))
    application.add_handler(MessageHandler(filters.PHOTO, gemini_chat))
    application.add_handler(MessageHandler(filters.Document.ALL, gemini_chat))# Handle photo messages
      # Function to run the Flask app
    def run_flask():
        app.run(host="0.0.0.0", port=port)

    # Start Flask app in a separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.start()

    # Run the Telegram bot
    application.run_polling()

if __name__ == "__main__":
    main()

