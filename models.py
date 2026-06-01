"""Pydantic models used by the API for schema editing, chat, and auth."""
from typing import List, Dict, Optional
from pydantic import BaseModel, Field

class FieldDescription(BaseModel):
    """Per-column description and optional mapping of categorical values."""
    description: str = ""
    technical_description: Optional[str] = None  # Auto-generated, LLM-facing, hidden from UI
    values: Optional[Dict[str, str]] = None

class FileSchemaDescription(BaseModel):
    """Schema for a single uploaded file: mapping column -> FieldDescription.

    `file_description` is optional and, when provided, the schema-save endpoints persist it
    on the file entry (alongside `schema.fields`) so the editable descriptions modal can
    update both column AND file descriptions in one POST.
    """
    file_name: str
    fields: Dict[str, FieldDescription]
    file_description: Optional[str] = None

class SaveDescriptionsRequest(BaseModel):
    """Payload for saving schema edits across multiple files."""
    files: List[FileSchemaDescription]

class ChatRequest(BaseModel):
    """Chat question request; optional flags may guide the LLM/planner."""
    question: str
    conv_id: Optional[str] = None

class GenerateChatRequest(BaseModel):
    """Payload for generating a chat dataset page."""
    name: Optional[str] = None
    days: Optional[int] = None

class RegisterRequest(BaseModel):
    """User registration payload (local auth)."""
    username: str
    password: str
    email: Optional[str] = None
    full_name: Optional[str] = None

class LoginRequest(BaseModel):
    """User login payload."""
    username: str
    password: str

class ProfileUpdateRequest(BaseModel):
    """Update profile fields; username change is optional."""
    email: Optional[str] = None
    full_name: Optional[str] = None
    new_username: Optional[str] = None

class PasswordChangeRequest(BaseModel):
    """Change or set password for the logged-in user."""
    current_password: Optional[str] = None
    new_password: str = Field(..., min_length=1)

class SubscriptionPlanUpdateRequest(BaseModel):
    """Update subscription plan for the logged-in user."""
    plan: str

class ShareChatRequest(BaseModel):
    """Share a chat with a list of emails."""
    emails: List[str]
    comment: Optional[str] = None  # Optional comment to include in share notification email

class PublicFlagRequest(BaseModel):
    """Toggle public visibility for a chat page."""
    public: bool


class ImportFromUrlRequest(BaseModel):
    """Import a file from Google Drive or Google Sheets URL."""
    url: str
    description: Optional[str] = None


class EditRegenerateRequest(BaseModel):
    """Edit last user message and regenerate AI response."""
    edited_question: str
    conv_id: str
