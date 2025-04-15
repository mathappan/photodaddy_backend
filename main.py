from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, Header, Form
import openai
from dotenv import load_dotenv, find_dotenv
import os
from fastapi.middleware.cors import CORSMiddleware
import boto3
import traceback
import json
from prompts import all_prompts, rewriting_for_conversion
from typing import Optional, Tuple
from supabase import create_client, Client
from PIL import Image
import io
import random
from pillow_heif import register_heif_opener
import asyncio

register_heif_opener()  # Enables PIL to open HEIC files

MAX_SIZE_MB = 5

app = FastAPI()

origins = ["http://localhost:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],  # Ensure Authorization is included
)
_ = load_dotenv(find_dotenv('.env.txt')) # read local .env file

# OpenAI API Key
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']

# Cloudflare R2 Credentials (from .env file)
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_BUCKET_PUBLIC_ADDRESS = os.getenv("R2_BUCKET_PUBLIC_ADDRESS")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Initialize Supabase client with service role
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# Configure Cloudflare R2 client (S3-compatible)
s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY
)

async def compress_image_if_needed(file: UploadFile) -> Tuple[io.BytesIO, str]:
    """
    Compresses the uploaded image to be under 5MB if needed.
    Returns a BytesIO buffer ready for upload and a new filename (if converted to JPEG).
    """
    if asyncio.iscoroutinefunction(file.read):
        original_bytes = await file.read()
    else:
        original_bytes = file.read()
    # original_bytes = await file.read()
    size_mb = len(original_bytes) / (1024 * 1024)

    if size_mb <= MAX_SIZE_MB:
        buffer = io.BytesIO(original_bytes)
        buffer.seek(0)
        return buffer

    print(f"Original image is {size_mb:.2f}MB. Compressing...")

    image = Image.open(io.BytesIO(original_bytes))

    # Convert to RGB if not already (e.g., for PNG with alpha)
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    # Try compressing to JPEG at reducing quality levels
    for quality in range(95, 10, -5):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        size_mb = buffer.tell() / (1024 * 1024)
        if size_mb <= MAX_SIZE_MB:
            print(f"Compressed to {size_mb:.2f}MB at quality {quality}")
            buffer.seek(0)
            return buffer

    # If unable to compress under 5MB
    print("Could not compress to under 5MB, uploading with lowest quality.")
    buffer.seek(0)
    
    return buffer

import random

def add_jitter_to_overlapping_coordinates(feedback, jitter_amount=0.1):
    """
    Adds a small jitter to overlapping coordinates in the feedback suggestions.
    Also adds jitter to suggestions that share the same exact horizontal (x) 
    or vertical (y) coordinates.
    
    jitter_amount: maximum offset to add/subtract (e.g., 0.05 means 5% of the normalized scale)
    """
    suggestions = feedback.get("suggestions", [])
    
    # Group suggestions by full (x, y), by x, and by y (all rounded to 2 decimals)
    coord_dict = {}
    x_dict = {}
    y_dict = {}
    
    for i, suggestion in enumerate(suggestions):
        x = round(suggestion["coordinates"]["x"], 2)
        y = round(suggestion["coordinates"]["y"], 2)
        # update with rounded values
        suggestion["coordinates"]["x"] = x
        suggestion["coordinates"]["y"] = y
        
        # Group by full coordinate
        coord_dict.setdefault((x, y), []).append(i)
        # Group by x (horizontal alignment)
        x_dict.setdefault(x, []).append(i)
        # Group by y (vertical alignment)
        y_dict.setdefault(y, []).append(i)
    
    # 1. Handle suggestions that overlap exactly (both x and y are the same)
    for (x, y), indices in coord_dict.items():
        if len(indices) > 1:
            for order, idx in enumerate(indices):
                # Optionally, let the first suggestion remain unchanged
                if order == 0:
                    continue
                offset = jitter_amount * order
                # Randomly decide the direction of the offset for both coordinates
                new_x = x + offset if random.choice([True, False]) else x - offset
                new_y = y + offset if random.choice([True, False]) else y - offset
                # Clamp values to [0, 1]
                suggestions[idx]["coordinates"]["x"] = max(0, min(1, round(new_x, 2)))
                suggestions[idx]["coordinates"]["y"] = max(0, min(1, round(new_y, 2)))
    
    # 2. Handle suggestions sharing the same horizontal coordinate (x)
    for x_val, indices in x_dict.items():
        if len(indices) > 1:
            for order, idx in enumerate(indices):
                # If this suggestion's x is still exactly x_val then add jitter
                if round(suggestions[idx]["coordinates"]["x"], 2) == x_val:
                    # Optionally leave the first occurrence untouched
                    if order == 0:
                        continue
                    offset = jitter_amount * order
                    new_x = x_val + offset if random.choice([True, False]) else x_val - offset
                    suggestions[idx]["coordinates"]["x"] = max(0, min(1, round(new_x, 2)))
    
    # 3. Handle suggestions sharing the same vertical coordinate (y)
    for y_val, indices in y_dict.items():
        if len(indices) > 1:
            for order, idx in enumerate(indices):
                if round(suggestions[idx]["coordinates"]["y"], 2) == y_val:
                    if order == 0:
                        continue
                    offset = jitter_amount * order
                    new_y = y_val + offset if random.choice([True, False]) else y_val - offset
                    suggestions[idx]["coordinates"]["y"] = max(0, min(1, round(new_y, 2)))
    
    return feedback

def convert_heic_to_jpg_stream(uploaded_file: UploadFile) -> io.BytesIO:
    image = Image.open(uploaded_file.file)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer


@app.get("/")
def home():
    return {"message": "Backend is running!"}


from fastapi import FastAPI, UploadFile, File, Form
import json, traceback
import openai

app = FastAPI()

# (Assume s3_client, R2_BUCKET_NAME, and R2_BUCKET_PUBLIC_ADDRESS are defined elsewhere)

from fastapi import Request

def verify_supabase_token(request: Request) -> str:
    print("nhere")
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        print("in if not")
        raise HTTPException(status_code=401, detail="Invalid Authorization Header")
    token = auth_header.split(" ")[1]
    user = supabase.auth.get_user(token)
    if not user.user.user_metadata['email']:
        raise HTTPException(status_code=400, detail="Email not found")
    
    return user.user.user_metadata['email']  # Return email as a unique user identifier




@app.post("/upload/")
async def upload_image(
    file: UploadFile = File(...),
    messages: str = Form(...),  # messages is a JSON string from the frontend
    style: str = Form(...),
    mode: str = Form(...),
    editingSubOption: Optional[str] = Form(None),  # Optional field
    user_email: str = Depends(verify_supabase_token)  # Validate user and get email
):
    """
    Handles image uploads: stores the image in Cloudflare R2 and
    gets AI feedback. It updates the conversation history so that the
    latest user message (the image URL) is the one just generated.
    """
    print("file:", "None" if not file else file.filename)
    print("messages:", messages)
    print("style:", style)
    print("mode:", mode)
    print("editingSubOption:", editingSubOption)
    # **1️⃣ Check if the user has enough credits**
    credit_query = supabase.from_("user_credits").select("credit_balance").eq("email", user_email).single().execute()
    if not credit_query.data["credit_balance"]:
        print('if not credit_query.data["credit_balance"]:')
        raise HTTPException(status_code=500, detail="Failed to fetch user credits")
    
    credits = credit_query.data["credit_balance"]

    # **2️⃣ Ensure user has at least 1 credit**
    print("credits - ", credits)
    if credits < 1:
        print("in credits less than one")
        raise HTTPException(status_code=402, detail="Insufficient credits. Please purchase more.")




    if style=="landscape and travel":
      style = 'landscape_travel'

    if style=="Product Photography":
      style = 'product'
    file_key = f"uploads/{file.filename}"  # File path in R2 bucket

    compressed_buffer = await compress_image_if_needed(file)
    # Upload image to Cloudflare R2
    s3_client.upload_fileobj(compressed_buffer, R2_BUCKET_NAME, file_key)
    
    # s3_client.upload_fileobj(file.file, R2_BUCKET_NAME, file_key)
    print("file uploaded")
    # Generate public URL (if bucket allows public access)
    image_url = f"{R2_BUCKET_PUBLIC_ADDRESS}/{file_key}"

    # Update the history messages: Replace the last user message with the new image_url.
    history_messages = json.loads(messages)
    
    history_messages.append({"user": image_url})

    # Call get_ai_feedback with the updated messages and the new image_url.
    ai_feedback = json.loads(get_ai_feedback(
        messages=json.dumps(history_messages), 
        style = style,
        mode=mode,
        editingSubOption=editingSubOption

    ))
    print(json.dumps(ai_feedback, indent=4))

    # **3️⃣ Deduct 1 credit atomically**
    update_query = supabase.from_("user_credits").update({"credit_balance": credits - 1}).eq("email", user_email).execute()
    print(update_query)
    if not update_query.data:
        raise HTTPException(status_code=500, detail="Failed to deduct credit")
    ai_feedback = add_jitter_to_overlapping_coordinates(ai_feedback)
    print("new feedback")
    print(json.dumps(ai_feedback, indent=4))
    return {"image_url": image_url, "feedback": ai_feedback, "remaining_credits": credits - 1}


@app.post("/get_ai_feedback/")
def get_ai_feedback(
    messages: str = Form(...), 
    style: str = Form(...),
    mode: str = Form(...),
    editingSubOption: Optional[str] = Form(None)  # Optional field
):
    """
    Constructs a conversation for OpenAI based on the provided messages history.
    The conversation is built as follows:
      - Start with an initial expert prompt.
      - Append each message from the history:
          * User messages are converted into image URL messages.
          * Bot messages are converted into assistant messages,
            where the content is the feedback JSON (which includes both
            top_level_suggestion and suggestions).
    The OpenAI API is then called using this conversation.
    """
    print("options - ")
    print(json.dumps({"style": style, "mode": mode, "editingsuboption": editingSubOption},
                     indent=4))
    # Parse the incoming messages list
    history_messages = json.loads(messages)

    # Define the base prompt for the feedback.


    # Build the conversation list for the OpenAI API.
    conversation = []
    # Start with the prompt as a user message.
    if mode=='photography':
      conversation.append({
          "role": "system",
          "content": [{"type": "text", "text": all_prompts[mode][style]}]
      })
    else:
      conversation.append({
          "role": "system",
          "content": [{"type": "text", "text": all_prompts[mode][editingSubOption][style]}]
      })

    # Append each message from the history.
    for msg in history_messages:
        if "user" in msg:
            conversation.append({
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": msg["user"], "detail": "high"}
                }]
            })
        elif "bot" in msg:
            # For bot messages, we expect the value to be the full feedback object.
            # It is stored as JSON in the message field.
            bot_feedback = msg["bot"]
            # If it's a string, parse it (otherwise assume it's already a dict)
            if isinstance(bot_feedback, str):
                bot_feedback = json.loads(bot_feedback)
            conversation.append({
                "role": "assistant",
                "content": [{"type": "text", "text": json.dumps(bot_feedback)}]
            })
    client = openai.OpenAI()
    print(json.dumps(conversation, indent=4))
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversation,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "photo_feedback_response",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "top_level_suggestion": {
                                "type": "string",
                                "description": "A professional expert critique of the photo, summarizing its overall strengths and weaknesses."
                            },
                            "suggestions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "suggestion": {
                                            "type": "string",
                                            "description": "A detailed suggestion on how to improve the photo"
                                        },
                                        "coordinates": {
                                            "type": "object",
                                            "properties": {
                                                "x": {
                                                    "type": "number",
                                                    "description": "Normalized x-coordinate (0 to 1) of the area to improve with 2 decimal places"
                                                   
                                                },
                                                "y": {
                                                    "type": "number",
                                                    "description": "Normalized y-coordinate (0 to 1) of the area to improve with 2 decimal places"
                                                
                                                },
                                            },
                                            "required": ["x", "y"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "required": ["suggestion", "coordinates"],
                                    "additionalProperties": False,
                                }
                            },
                            "rating": {
                                            "type": "number",
                                            "description": "A rating of the photo out of 10. Can have one decimal place"
                                        },
                        },
                        "required": ["top_level_suggestion", "suggestions", "rating"],
                        "additionalProperties": False
                    }
                }
            }
        )
        print('****************response***********')
        print(response)
        # Return the feedback content (which will be a JSON string)
        return response.choices[0].message.content
    except Exception as e:
        traceback.print_exc()
        print("Error:", e)
        return "Failed to get AI feedback."


@app.post("/public-upload/")
async def upload_image(
    file: UploadFile = File(...),
    messages: str = Form(...),  # messages is a JSON string from the frontend
    style: str = Form(...),
    mode: str = Form(...),
    editingSubOption: Optional[str] = Form(None),  # Optional field
):
    """
    Handles image uploads: stores the image in Cloudflare R2 and
    gets AI feedback. It updates the conversation history so that the
    latest user message (the image URL) is the one just generated.
    """
    try:
        print("file:", "None" if not file else file.filename)
        print("style:", style)
        print("mode:", mode)
        print("editingSubOption:", editingSubOption)
        # **1️⃣ Check if the user has enough credits**

        if style=="landscape and travel":
         style = 'landscape_travel'
        if style=="landscape":
         style = 'landscape_travel'

        if style=="Product Photography":
            style = 'product'
        file_key = f"uploads/{file.filename}"  # File path in R2 bucket

        filename_lower = file.filename.lower()
        if filename_lower.endswith(".heic"):
            print("Converting HEIC to JPG...")
            file.file.seek(0)
            jpg_buffer = convert_heic_to_jpg_stream(file)
            # Adjust file key to .jpg for storage
            file_key = f"uploads/{os.path.splitext(file.filename)[0]}.jpg"
            compressed_buffer = await compress_image_if_needed(jpg_buffer)
        else:
            compressed_buffer = await compress_image_if_needed(file)
        # Upload image to Cloudflare R2
        s3_client.upload_fileobj(compressed_buffer, R2_BUCKET_NAME, file_key)
        
        # s3_client.upload_fileobj(file.file, R2_BUCKET_NAME, file_key)
        print("file uploaded")
        # Generate public URL (if bucket allows public access)
        image_url = f"{R2_BUCKET_PUBLIC_ADDRESS}/{file_key}"

        # Update the history messages: Replace the last user message with the new image_url.
        history_messages = json.loads(messages)
        
        history_messages.append({"user": image_url})

 
        try:
        # Call get_ai_feedback with the updated messages and the new image_url.
            ai_feedback = json.loads(get_ai_feedback(
                messages=json.dumps(history_messages), 
                style = style,
                mode=mode,
                editingSubOption=editingSubOption

            ))
            print(json.dumps(ai_feedback, indent=4))
        except:
            ai_feedback = json.loads(get_ai_feedback(
                messages=json.dumps(history_messages), 
                style = style,
                mode=mode,
                editingSubOption=editingSubOption

            ))
            print(json.dumps(ai_feedback, indent=4))

        ai_feedback = add_jitter_to_overlapping_coordinates(ai_feedback)
        ai_feedback['to_show'] = -1

        conversation = []

        conversation.append({
                "role": "system",
                "content": [{"type": "text", "text": rewriting_for_conversion}]
            })
        conversation.append({
                "role": "user",
                "content": [{"type": "text", "text": json.dumps(ai_feedback)}]
            })
        
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversation,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "generate_recipe_response",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                                        "top_level_feedback": {
                                            "type": "string",
                                            "description": "Rewritten top level suggestion"
                                        },
                                        "index_of_suggestion": {
                                            "type": "integer",
                                            "description": "Index of most high converting suggestion"
                                        },
                                    },
                                    "required": [
                                        "top_level_feedback", "index_of_suggestion"
                                    ],
                                    "additionalProperties": False
                                },
                    }
                })
        response = json.loads(response.choices[0].message.content)
        ai_feedback['rewritten_top_level_suggestion'] = response['top_level_feedback']
        ai_feedback['to_show'] = response['index_of_suggestion']

        print("new feedback")
        print(json.dumps(ai_feedback, indent=4))
        return {"image_url": image_url, "feedback": ai_feedback }


    except Exception as e:
            traceback.print_exc()
            print("Error:", e)
            return "Failed to get AI feedback."