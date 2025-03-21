from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, Header, Form
import openai
from dotenv import load_dotenv, find_dotenv
import os
from fastapi.middleware.cors import CORSMiddleware
import boto3
import traceback
import json
from prompts import all_prompts
from typing import Optional
from supabase import create_client, Client
import requests

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
    # Validate token with Supabase
    # response = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers={"Authorization": f"Bearer {token}"})
    # print(response)
    # if response.status_code != 200:
    #     raise HTTPException(status_code=401, detail="Invalid Token")
    
    # user_data = response.json()
    # email = user_data.get("email")
    # supabase.auth.set_auth(token)
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
    print(credit_query)
    if not credit_query.data["credit_balance"]:
        print('if not credit_query.data["credit_balance"]:')
        raise HTTPException(status_code=500, detail="Failed to fetch user credits")
    
    credits = credit_query.data["credit_balance"]

    # **2️⃣ Ensure user has at least 1 credit**
    print("credits - ", credits)
    if credits < 1:
        print("in credits less than one")
        raise HTTPException(status_code=402, detail="Insufficient credits. Please purchase more.")

    # **3️⃣ Deduct 1 credit atomically**
    update_query = supabase.from_("user_credits").update({"credit_balance": credits - 1}).eq("email", user_email).execute()
    print(update_query)
    if not update_query.data:
        raise HTTPException(status_code=500, detail="Failed to deduct credit")


    if style=="landscape and travel":
      style = 'landscape_travel'

    if style=="Product Photography":
      style = 'product'
    file_key = f"uploads/{file.filename}"  # File path in R2 bucket

    # Upload image to Cloudflare R2
    s3_client.upload_fileobj(file.file, R2_BUCKET_NAME, file_key)

    # Generate public URL (if bucket allows public access)
    image_url = f"{R2_BUCKET_PUBLIC_ADDRESS}/{file_key}"
    print

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
          "role": "user",
          "content": [{"type": "text", "text": all_prompts[mode][style]}]
      })
    else:
      conversation.append({
          "role": "user",
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
