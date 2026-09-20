import hashlib
import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import List, Optional

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
)

from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from fastapi.security import (
    HTTPBearer,
    HTTPAuthorizationCredentials,
)

from google import genai
from google.genai import types

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
)

from sqlalchemy import text
from sqlalchemy.orm import Session

from passlib.context import CryptContext
from jose import jwt

from .database import Base, engine, get_db
from . import models

from .schemas import (
    TaskCreate,
    TaskUpdate,
    TaskResponse,
)


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

print(
    f"TIMEPILOT MAIN LOADED FROM: "
    f"{os.path.abspath(__file__)}"
)


GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY"
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.6-flash",
)


# ============================================================
# EMAIL VERIFICATION
# ============================================================

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "TimePilot <onboarding@resend.dev>")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def hash_verification_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_verification_email(email: str, name: str, verification_token: str):
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not configured.")

    verification_url = f"{FRONTEND_URL}/verify-email?token={verification_token}"

    html = f"""
    <html><body style="margin:0;padding:40px 20px;background:#050505;font-family:Arial;color:#fff;">
      <div style="max-width:560px;margin:auto;padding:40px;border-radius:24px;background:#111;border:1px solid #2a2a2a;">
        <div style="font-size:24px;font-weight:bold;color:#00e5a0;">TimePilot</div>
        <h1 style="margin:28px 0 12px;">Verify your TimePilot account</h1>
        <p style="color:#a1a1aa;font-size:16px;line-height:1.7;">Hi {name},</p>
        <p style="color:#a1a1aa;font-size:16px;line-height:1.7;">Thanks for creating your TimePilot account. Click below to verify your email address.</p>
        <p style="margin:32px 0;"><a href="{verification_url}" style="display:inline-block;padding:16px 28px;border-radius:12px;background:#fff;color:#000;text-decoration:none;font-weight:600;">Verify my email</a></p>
        <p style="color:#71717a;font-size:13px;">This verification link expires in 24 hours.</p>
        <p style="color:#52525b;font-size:12px;word-break:break-all;">{verification_url}</p>
      </div>
    </body></html>
    """

    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [email],
        "subject": "Verify your TimePilot account",
        "html": html,
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print("VERIFICATION EMAIL SENT:", response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print("RESEND EMAIL ERROR:", error.code, body)
        raise RuntimeError("Could not send verification email.") from error
    except Exception as error:
        print("EMAIL SENDING ERROR:", repr(error))
        raise RuntimeError("Could not send verification email.") from error


# ============================================================
# GEMINI CLIENT
# ============================================================

gemini_client = None


if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        print(
            f"Gemini AI configured: {GEMINI_MODEL}"
        )

    except Exception as error:

        print(
            "GEMINI CLIENT ERROR:",
            repr(error),
        )

else:

    print(
        "WARNING: GEMINI_API_KEY is not configured."
    )


# ============================================================
# PASSWORD HASHING
# ============================================================

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)


# ============================================================
# JWT
# ============================================================

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is not configured. "
        "Add it to backend/.env before starting TimePilot."
    )

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "timepilot"
JWT_AUDIENCE = "timepilot-client"
JWT_EXPIRE_MINUTES = 60 * 24


# ============================================================
# AUTHENTICATION
# ============================================================

security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(
        security
    ),
    db: Session = Depends(get_db),
):

    token = credentials.credentials

    try:

        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
        )

        user_id = payload.get("sub")

        if not user_id:

            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token.",
            )

        user_id = int(user_id)

    except HTTPException:

        raise

    except Exception:

        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token.",
        )

    user = (
        db.query(models.User)
        .filter(
            models.User.id == user_id
        )
        .first()
    )

    if user is None:

        raise HTTPException(
            status_code=401,
            detail="User not found.",
        )

    return user


# ============================================================
# RATE LIMITING
# ============================================================

limiter = Limiter(key_func=get_remote_address)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="TimePilot AI",
    description="AI-powered time management assistant",
    version="0.2.0",
)

app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)


# ============================================================
# DATABASE
# ============================================================

Base.metadata.create_all(
    bind=engine
)


def migrate_authentication_columns():
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_hash VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_expires_at TIMESTAMP",
        "CREATE INDEX IF NOT EXISTS ix_users_verification_token_hash ON users (verification_token_hash)",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


try:
    migrate_authentication_columns()
    print("Authentication database migration completed.")
except Exception as error:
    print("AUTHENTICATION DATABASE MIGRATION ERROR:", repr(error))


# ============================================================
# SECURITY HEADERS
# ============================================================

@app.middleware("http")
async def add_security_headers(request, call_next):
    response: Response = await call_next(request)

    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), "
        "payment=(), usb=()"
    )

    # Prevent shared/proxy caches from storing authenticated data.
    if request.url.path.startswith(("/auth/", "/tasks", "/agent/")):
        response.headers["Cache-Control"] = "no-store"

    return response


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,

    # Production + development frontend origins.
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://timepilot-frontend-nine.vercel.app",
    ],

    allow_credentials=True,

    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],

    allow_headers=[
        "*",
    ],

    expose_headers=[
        "Content-Length",
    ],
)


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
def root():

    return {
        "message": "TimePilot AI is running"
    }


@app.get("/health")
def health():

    return {
        "status": "healthy"
    }


# ============================================================
# DATABASE TEST
# ============================================================

@app.get("/database-test")
def database_test():

    with engine.connect() as connection:

        result = connection.execute(
            text("SELECT 1")
        )

        value = result.scalar()

    return {
        "database": "connected",
        "result": value,
    }


# ============================================================
# GEMINI STATUS
# ============================================================

@app.get("/agent/status")
def agent_status():

    return {
        "provider": "google-gemini",
        "configured": gemini_client is not None,
        "model": GEMINI_MODEL,
    }


# ============================================================
# AUTH SCHEMAS
# ============================================================

class SignupRequest(BaseModel):

    name: str = Field(
        ...,
        min_length=2,
        max_length=100,
    )

    email: EmailStr

    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
    )


class SignupResponse(BaseModel):

    message: str

    user_id: int

    name: str

    email: str


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=32, max_length=200)


class VerifyEmailResponse(BaseModel):
    message: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ResendVerificationResponse(BaseModel):
    message: str


class LoginRequest(BaseModel):

    email: EmailStr

    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
    )


class LoginResponse(BaseModel):

    message: str

    access_token: str

    token_type: str

    user_id: int

    name: str

    email: str


# ============================================================
# SIGNUP
# ============================================================

@app.post(
    "/auth/signup",
    response_model=SignupResponse,
)
@limiter.limit("5/minute")
def signup(
    request: Request,
    data: SignupRequest,
    db: Session = Depends(get_db),
):

    name = data.name.strip()

    email = (
        str(data.email)
        .strip()
        .lower()
    )

    password = data.password

    if len(name) < 2:

        raise HTTPException(
            status_code=400,
            detail="Name must contain at least 2 characters.",
        )

    if len(name) > 100:

        raise HTTPException(
            status_code=400,
            detail="Name must not exceed 100 characters.",
        )

    if len(password) < 8:

        raise HTTPException(
            status_code=400,
            detail="Password must contain at least 8 characters.",
        )

    if len(password) > 128:

        raise HTTPException(
            status_code=400,
            detail="Password must not exceed 128 characters.",
        )

    if len(password) > 128:

        raise HTTPException(
            status_code=400,
            detail="Password must not exceed 128 characters.",
        )

    existing_user = (
        db.query(models.User)
        .filter(
            models.User.email == email
        )
        .first()
    )

    if existing_user is not None:

        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists.",
        )

    password_hash = pwd_context.hash(
        password
    )

    verification_token = secrets.token_urlsafe(48)
    verification_token_hash = hash_verification_token(verification_token)
    verification_expires_at = datetime.utcnow() + timedelta(hours=24)

    new_user = models.User(
        name=name,
        email=email,
        password_hash=password_hash,
        email_verified=False,
        verification_token_hash=verification_token_hash,
        verification_token_expires_at=verification_expires_at,
    )

    db.add(new_user)

    try:

        db.commit()

        db.refresh(new_user)

    except Exception as error:

        print(
            "SIGNUP DATABASE ERROR:",
            repr(error),
        )

        db.rollback()

        raise HTTPException(
            status_code=500,
            detail="Could not create account.",
        )

    try:
        send_verification_email(
            email=new_user.email,
            name=new_user.name,
            verification_token=verification_token,
        )
    except Exception as error:
        print("VERIFICATION EMAIL ERROR:", repr(error))
        db.delete(new_user)
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Account could not be created because the verification email could not be sent.",
        )

    return SignupResponse(
        message="Verification email sent.",
        user_id=new_user.id,
        name=new_user.name,
        email=new_user.email,
    )


# ============================================================
# VERIFY EMAIL
# ============================================================

@app.post("/auth/verify-email", response_model=VerifyEmailResponse)
@limiter.limit("10/minute")
def verify_email(
    request: Request,
    data: VerifyEmailRequest,
    db: Session = Depends(get_db),
):
    token_hash = hash_verification_token(data.token)
    user = (
        db.query(models.User)
        .filter(models.User.verification_token_hash == token_hash)
        .first()
    )

    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link.")

    if user.email_verified:
        return VerifyEmailResponse(message="Email is already verified.")

    if (
        user.verification_token_expires_at is None
        or user.verification_token_expires_at < datetime.utcnow()
    ):
        raise HTTPException(status_code=400, detail="Verification link has expired.")

    user.email_verified = True
    user.verification_token_hash = None
    user.verification_token_expires_at = None
    db.commit()

    return VerifyEmailResponse(message="Email verified successfully.")


# ============================================================
# RESEND VERIFICATION EMAIL
# ============================================================

@app.post("/auth/resend-verification", response_model=ResendVerificationResponse)
@limiter.limit("3/10minutes")
def resend_verification(
    request: Request,
    data: ResendVerificationRequest,
    db: Session = Depends(get_db),
):
    email = str(data.email).strip().lower()
    user = db.query(models.User).filter(models.User.email == email).first()

    if user is None:
        return ResendVerificationResponse(
            message="If an account exists for this email, a verification email has been sent."
        )

    if user.email_verified:
        return ResendVerificationResponse(message="Email is already verified.")

    verification_token = secrets.token_urlsafe(48)
    user.verification_token_hash = hash_verification_token(verification_token)
    user.verification_token_expires_at = datetime.utcnow() + timedelta(hours=24)
    db.commit()

    try:
        send_verification_email(
            email=user.email,
            name=user.name,
            verification_token=verification_token,
        )
    except Exception as error:
        print("RESEND VERIFICATION ERROR:", repr(error))
        raise HTTPException(status_code=503, detail="Could not send the verification email.")

    return ResendVerificationResponse(message="Verification email sent.")


# ============================================================
# LOGIN
# ============================================================

@app.post(
    "/auth/login",
    response_model=LoginResponse,
)
@limiter.limit("5/minute")
def login(
    request: Request,
    data: LoginRequest,
    db: Session = Depends(get_db),
):

    email = (
        str(data.email)
        .strip()
        .lower()
    )

    password = data.password

    user = (
        db.query(models.User)
        .filter(
            models.User.email == email
        )
        .first()
    )

    if user is None:

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    if not pwd_context.verify(
        password,
        user.password_hash,
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in.",
        )

    now = datetime.utcnow()
    expires_at = now + timedelta(
        minutes=JWT_EXPIRE_MINUTES
    )

    token_payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": now,
        "exp": expires_at,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }

    access_token = jwt.encode(
        token_payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    return LoginResponse(

        message="Login successful.",

        access_token=access_token,

        token_type="bearer",

        user_id=user.id,

        name=user.name,

        email=user.email,

    )


# ============================================================
# CREATE TASK
# ============================================================

@app.post(
    "/tasks",
    response_model=TaskResponse,
)
def create_task(

    task: TaskCreate,

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    new_task = models.Task(

        user_id=current_user.id,

        title=task.title,

        description=task.description,

        priority=task.priority,

        estimated_minutes=task.estimated_minutes,

        deadline=task.deadline,

    )

    db.add(new_task)

    db.commit()

    db.refresh(new_task)

    return new_task


# ============================================================
# GET TASKS
# ============================================================

@app.get(
    "/tasks",
    response_model=list[TaskResponse],
)
def get_tasks(

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    tasks = (

        db.query(models.Task)

        .filter(
            models.Task.user_id
            == current_user.id
        )

        .order_by(
            models.Task.created_at.desc()
        )

        .all()

    )

    return tasks


# ============================================================
# GET SINGLE TASK
# ============================================================

@app.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
)
def get_task(

    task_id: int,

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    task = (

        db.query(models.Task)

        .filter(

            models.Task.id == task_id,

            models.Task.user_id
            == current_user.id,

        )

        .first()

    )

    if task is None:

        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    return task


# ============================================================
# UPDATE TASK
# ============================================================

@app.put(
    "/tasks/{task_id}",
    response_model=TaskResponse,
)
def update_task(

    task_id: int,

    task_data: TaskUpdate,

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    task = (

        db.query(models.Task)

        .filter(

            models.Task.id == task_id,

            models.Task.user_id
            == current_user.id,

        )

        .first()

    )

    if task is None:

        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    update_data = task_data.model_dump(
        exclude_unset=True
    )

    for field, value in update_data.items():

        setattr(
            task,
            field,
            value,
        )

    db.commit()

    db.refresh(task)

    return task


# ============================================================
# DELETE TASK
# ============================================================

@app.delete(
    "/tasks/{task_id}"
)
def delete_task(

    task_id: int,

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    task = (

        db.query(models.Task)

        .filter(

            models.Task.id == task_id,

            models.Task.user_id
            == current_user.id,

        )

        .first()

    )

    if task is None:

        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    db.delete(task)

    db.commit()

    return {
        "message": "Task deleted successfully"
    }


# ============================================================
# CHAT REQUEST
# ============================================================

class ChatRequest(BaseModel):

    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
    )


class AITaskData(BaseModel):

    title: str = Field(
        ...,
        min_length=1,
        max_length=200,
    )

    description: Optional[str] = Field(
        default=None,
        max_length=2000,
    )

    priority: str = Field(
        default="medium",
        max_length=20,
    )

    estimated_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
    )

    deadline: Optional[str] = Field(
        default=None,
        max_length=50,
    )


class AIChatResponse(BaseModel):

    action: str = "chat"
    response: str
    task: Optional[AITaskData] = None


# ============================================================
# DAY PLAN SCHEMAS
# ============================================================

class ScheduleItem(BaseModel):

    task_id: Optional[int] = None

    title: str

    start: str

    end: str

    type: str = "task"


class UnscheduledItem(BaseModel):

    task_id: int

    title: str

    reason: str


class DayPlanResponse(BaseModel):

    summary: str

    schedule: List[ScheduleItem]

    unscheduled: List[UnscheduledItem]


# ============================================================
# TASK CONTEXT
# ============================================================

def build_task_context(
    tasks: list,
) -> str:

    if not tasks:

        return (
            "The user currently has no tasks."
        )

    task_lines = []

    for task in tasks:

        task_lines.append(
            f"""
Task ID: {task.id}
Title: {task.title}
Description: {task.description or "No description"}
Priority: {task.priority}
Estimated minutes: {task.estimated_minutes or "Unknown"}
Deadline: {
    task.deadline.isoformat()
    if task.deadline
    else "No deadline"
}
Status: {task.status}
"""
        )

    return "\n".join(
        task_lines
    )


# ============================================================
# ASK TIMEPILOT
# ============================================================

@app.post(
    "/agent/chat"
)
@limiter.limit("10/minute")
def agent_chat(
    request: Request,
    data: ChatRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if gemini_client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Gemini AI is not configured. "
                "Add GEMINI_API_KEY to backend/.env "
                "and restart the backend."
            ),
        )

    message = data.message.strip()

    if not message:
        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty.",
        )

    if len(message) > 4000:
        raise HTTPException(
            status_code=413,
            detail="AI message must not exceed 4000 characters.",
        )

    tasks = (
        db.query(models.Task)
        .filter(models.Task.user_id == current_user.id)
        .order_by(models.Task.created_at.desc())
        .all()
    )

    tasks_context = build_task_context(tasks)
    now = datetime.now()

    instructions = f"""
You are TimePilot, an AI-powered personal time-management assistant.

The current local date and time is:
{now.isoformat(timespec="minutes")}

Your most important job is to understand whether the user wants
to CREATE A NEW TASK in their TimePilot task list.

Return ONLY valid JSON.
Do not use markdown.
Do not use code fences.
Do not add any text before or after the JSON.

For a normal conversation, return:

{{
  "action": "chat",
  "response": "your helpful response",
  "task": null
}}

For a clear task-creation request, return:

{{
  "action": "create_task",
  "response": "short confirmation message",
  "task": {{
    "title": "clear task title",
    "description": "useful description or null",
    "priority": "low|medium|high",
    "estimated_minutes": 30,
    "deadline": "YYYY-MM-DDTHH:MM:SS or null"
  }}
}}

TASK CREATION RULES:

1. Create a task ONLY when the user clearly asks to add,
   create, make, remember, put, track, or schedule a NEW task.

2. Advice, planning, recommendations, information, or asking
   what existing task to work on next means action="chat".

3. Extract a concise, actionable task title.

4. Convert relative dates using the current date/time above.

5. If the user gives a specific time, include it in deadline.

6. If the user gives only a date, use 18:00 local time.

7. If no deadline is given, deadline MUST be null.
   NEVER invent a deadline.

8. Default priority to "medium".

9. Use high priority only when clearly indicated.

10. Default estimated_minutes to 30.

11. Convert "for 1 hour" to 60 minutes,
    "for 90 minutes" to 90 minutes, etc.

12. Do not create duplicate tasks.

13. "What should I work on next?", "plan my day",
    and "what do I have today?" are action="chat".

14. Never claim a task was created unless action="create_task".

Existing TimePilot tasks:

{tasks_context}
"""

    prompt = f"""
User name:
{current_user.name}

User request:
{message}
"""

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=instructions,
                max_output_tokens=1000,
                response_mime_type="application/json",
                response_schema=AIChatResponse,
            ),
        )

        parsed = getattr(response, "parsed", None)

        if parsed is not None:
            if isinstance(parsed, AIChatResponse):
                ai_result = parsed
            elif isinstance(parsed, dict):
                ai_result = AIChatResponse.model_validate(parsed)
            else:
                ai_result = AIChatResponse.model_validate(parsed)
        else:
            raw_text = (
                getattr(response, "text", None) or ""
            ).strip()

            if not raw_text:
                raise ValueError(
                    "Gemini returned an empty structured response."
                )

            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            elif raw_text.startswith("```"):
                raw_text = raw_text[3:]

            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]

            raw_text = raw_text.strip()

            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "Gemini returned invalid JSON. "
                    "Please try the request again."
                ) from error

            ai_result = AIChatResponse.model_validate(parsed)

        # IMPORTANT:
        # Convert Pydantic AIChatResponse to a plain dictionary
        # before using .get().
        if isinstance(ai_result, AIChatResponse):
            ai_data = ai_result.model_dump()
        elif isinstance(ai_result, dict):
            ai_data = ai_result
        else:
            ai_data = (
                AIChatResponse
                .model_validate(ai_result)
                .model_dump()
            )

        action = str(
            ai_data.get("action", "chat") or "chat"
        ).strip().lower()

        ai_text = str(
            ai_data.get("response", "") or ""
        ).strip()

        task_data = ai_data.get("task")

        if action not in {"chat", "create_task"}:
            action = "chat"

        if action != "create_task":
            return {
                "response": ai_text,
                "action": "chat",
                "task_created": False,
                "task": None,
            }

        if task_data is None:
            return {
                "response": ai_text,
                "action": "chat",
                "task_created": False,
                "task": None,
            }

        if isinstance(task_data, AITaskData):
            task_dict = task_data.model_dump()
        elif isinstance(task_data, dict):
            task_dict = task_data
        else:
            task_dict = (
                AITaskData
                .model_validate(task_data)
                .model_dump()
            )

        title = str(
            task_dict.get("title", "") or ""
        ).strip()

        if not title:
            raise ValueError(
                "Gemini requested task creation but did not provide "
                "a task title."
            )

        if len(title) > 200:
            raise ValueError(
                "Gemini returned a task title longer than 200 characters."
            )

        description_value = task_dict.get("description")

        description = (
            str(description_value).strip()
            if (
                description_value is not None
                and str(description_value).strip()
            )
            else None
        )

        if description is not None and len(description) > 2000:
            raise ValueError(
                "Gemini returned a task description longer than 2000 characters."
            )

        priority = str(
            task_dict.get("priority", "medium") or "medium"
        ).strip().lower()

        if priority not in {"low", "medium", "high"}:
            priority = "medium"

        try:
            estimated_minutes = int(
                task_dict.get("estimated_minutes", 30) or 30
            )
        except (TypeError, ValueError):
            estimated_minutes = 30

        if estimated_minutes < 1:
            estimated_minutes = 30

        if estimated_minutes > 1440:
            estimated_minutes = 1440

        deadline = None
        deadline_value = task_dict.get("deadline")

        if deadline_value:
            deadline_string = str(deadline_value).strip()

            try:
                deadline = datetime.fromisoformat(
                    deadline_string
                )

                if deadline.tzinfo is not None:
                    deadline = (
                        deadline.astimezone()
                        .replace(tzinfo=None)
                    )

            except ValueError as error:
                raise ValueError(
                    f"Invalid deadline returned by Gemini: "
                    f"{deadline_string}"
                ) from error

        duplicate_task = (
            db.query(models.Task)
            .filter(
                models.Task.user_id == current_user.id,
                models.Task.title == title,
                models.Task.status != "completed",
            )
            .first()
        )

        if duplicate_task is not None:
            existing_task = (
                TaskResponse
                .model_validate(duplicate_task)
                .model_dump(mode="json")
            )

            return {
                "response": (
                    f'You already have an unfinished task called '
                    f'"{duplicate_task.title}".'
                ),
                "action": "chat",
                "task_created": False,
                "task": existing_task,
            }

        new_task = models.Task(
            user_id=current_user.id,
            title=title,
            description=description,
            priority=priority,
            estimated_minutes=estimated_minutes,
            deadline=deadline,
        )

        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        created_task = (
            TaskResponse
            .model_validate(new_task)
            .model_dump(mode="json")
        )

        return {
            "response": (
                ai_text
                if ai_text
                else f'Task created: "{title}".'
            ),
            "action": "create_task",
            "task_created": True,
            "task": created_task,
        }

    except HTTPException:
        raise

    except Exception as error:
        print()
        print("=" * 70)
        print("GEMINI CHAT / TASK CREATION ERROR")
        print("=" * 70)
        print("ERROR TYPE:", type(error).__name__)
        print("ERROR:", repr(error))
        print("=" * 70)
        print()

        db.rollback()

        raise HTTPException(
            status_code=502,
            detail=f"Gemini error: {str(error)}",
        )


# ============================================================
# PLAN MY DAY
# ============================================================

@app.post(
    "/agent/plan-day",
    response_model=DayPlanResponse,
)
@limiter.limit("5/minute")
def plan_day(

    request: Optional[dict] = None,

    db: Session = Depends(get_db),

    current_user: models.User = Depends(
        get_current_user
    ),

):

    # --------------------------------------------------------
    # Gemini check
    # --------------------------------------------------------

    if gemini_client is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Gemini AI is not configured. "
                "Add GEMINI_API_KEY to backend/.env "
                "and restart the backend."
            ),
        )

    # --------------------------------------------------------
    # Current date/time
    # --------------------------------------------------------

    now = datetime.now()

    current_time = now.strftime(
        "%H:%M"
    )

    current_date = now.strftime(
        "%Y-%m-%d"
    )

    # --------------------------------------------------------
    # Get unfinished tasks
    # --------------------------------------------------------

    tasks = (

        db.query(models.Task)

        .filter(

            models.Task.user_id
            == current_user.id,

            models.Task.status
            != "completed",

        )

        .order_by(
            models.Task.created_at.asc()
        )

        .all()

    )

    # --------------------------------------------------------
    # No tasks
    # --------------------------------------------------------

    if not tasks:

        return DayPlanResponse(

            summary=(
                "You don't have any unfinished "
                "tasks to schedule. Add some tasks "
                "and TimePilot can plan your day."
            ),

            schedule=[],

            unscheduled=[],

        )

    tasks_context = build_task_context(
        tasks
    )

    # --------------------------------------------------------
    # Scheduling instructions
    # --------------------------------------------------------

    instructions = """
You are TimePilot's day-planning engine.

Create a realistic schedule for the user's
remaining day.

You MUST return JSON matching the supplied
response schema.

RULES:

1. Use ONLY tasks supplied by the user.

2. Never invent task IDs.

3. Never schedule completed tasks.

4. Prioritize high-priority tasks.

5. Respect deadlines.

6. Use estimated_minutes when available.

7. Do not schedule work in the past.

8. Start from the current time.

9. Use 24-hour HH:MM format.

10. Do not create overlapping blocks.

11. Include reasonable breaks.

12. Breaks must have:
    task_id = null
    type = "break"

13. Task blocks must have:
    task_id = actual task ID
    type = "task"

14. Tasks that cannot reasonably fit should
    be placed in unscheduled.

15. Every unscheduled task must contain:
    task_id
    title
    reason

16. Never duplicate a task between schedule
    and unscheduled.

17. Keep the summary short.

18. Do not create calendar events.

19. Do not create reminders.

20. The schedule is for today only.
"""

    # --------------------------------------------------------
    # Planning prompt
    # --------------------------------------------------------

    prompt = f"""
Today:

{current_date}

Current local time:

{current_time}

User:

{current_user.name}

Unfinished TimePilot tasks:

{tasks_context}

Create the best realistic schedule for
the remaining part of today.
"""

    # --------------------------------------------------------
    # Gemini structured response
    # --------------------------------------------------------

    try:

        response = gemini_client.models.generate_content(

            model=GEMINI_MODEL,

            contents=prompt,

            config=types.GenerateContentConfig(

                system_instruction=instructions,

                response_mime_type="application/json",

                response_schema=DayPlanResponse,


                max_output_tokens=2500,

            ),

        )

        raw_text = (
            response.text
            if response.text
            else ""
        )

        if not raw_text:

            raise ValueError(
                "Gemini returned an empty response."
            )

        # ----------------------------------------------------
        # Parse JSON
        # ----------------------------------------------------

        try:

            parsed = json.loads(
                raw_text
            )

        except json.JSONDecodeError:

            cleaned = raw_text.strip()

            if cleaned.startswith(
                "```json"
            ):

                cleaned = cleaned[7:]

            elif cleaned.startswith(
                "```"
            ):

                cleaned = cleaned[3:]

            if cleaned.endswith(
                "```"
            ):

                cleaned = cleaned[:-3]

            parsed = json.loads(
                cleaned.strip()
            )

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        plan = DayPlanResponse.model_validate(
            parsed
        )

        return plan

    except Exception as error:

        print()
        print("=" * 70)
        print("GEMINI PLAN DAY ERROR")
        print("=" * 70)
        print(
            repr(error)
        )
        print("=" * 70)
        print()

        raise HTTPException(
            status_code=502,
            detail=f"Gemini plan error: {str(error)}",
        )