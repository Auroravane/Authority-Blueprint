"""
Project ACHILLES — Report Engine
Generates a daily Market Intelligence Report using the NVIDIA NIM API
and exports it to Google Docs.
"""

import os
import sys
import json
import time
import datetime
import traceback
from pathlib import Path
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
MODEL = "mistralai/mistral-nemotron"
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_TOKENS = 16384  # ACHILLES Format A reports are long; 4096 truncates them
TEMPERATURE = 0.6
TOP_P = 0.7
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30

# Google Docs batchUpdate has a practical character limit per insertText request.
# We chunk content at this boundary to avoid silent truncation or 400 errors.
GOOGLE_DOCS_CHUNK_SIZE = 40000

# JST offset for consistent date/time across the pipeline
JST_OFFSET = datetime.timedelta(hours=9)

REQUIRED_ENV_VARS = ["NVIDIA_API_KEY", "GOOGLE_CREDENTIALS", "GOOGLE_FOLDER_ID"]

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# Errors that should NOT be retried — failing fast saves 90s of pointless waiting
NON_RETRYABLE_ERRORS = (
    "401",          # Bad API key
    "403",          # Forbidden / quota exceeded
    "invalid_api",  # NVIDIA-specific auth failure
)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def jst_now() -> datetime.datetime:
    """Return the current time in JST (UTC+9)."""
    return datetime.datetime.utcnow() + JST_OFFSET


def jst_today() -> datetime.date:
    """Return today's date in JST (UTC+9)."""
    return jst_now().date()


# ──────────────────────────────────────────────
# GUARDS
# ──────────────────────────────────────────────
def validate_environment():
    """Fail fast if any required secret is missing or malformed."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        print(f"❌ FATAL: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Validate GOOGLE_CREDENTIALS is parseable JSON before we reach the upload step
    try:
        creds = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        if "client_email" not in creds or "private_key" not in creds:
            raise ValueError("JSON is valid but missing 'client_email' or 'private_key' fields.")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"❌ FATAL: GOOGLE_CREDENTIALS is not valid service account JSON: {e}")
        print("   Ensure you pasted the FULL contents of the .json key file into the GitHub Secret.")
        sys.exit(1)

    print("✅ All environment variables present and validated.")


def resolve_prompt_path() -> Path:
    """Resolve SystemPrompt.md relative to the repo root, not the script's CWD."""
    # GitHub Actions checks out to GITHUB_WORKSPACE; local runs use repo root
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parent.parent))
    prompt_path = repo_root / "SystemPrompt.md"
    if not prompt_path.exists():
        print(f"❌ FATAL: SystemPrompt.md not found at {prompt_path}")
        sys.exit(1)
    return prompt_path


def is_retryable(error: Exception) -> bool:
    """Check if an error is worth retrying or should fail immediately."""
    error_str = str(error).lower()
    for marker in NON_RETRYABLE_ERRORS:
        if marker.lower() in error_str:
            return False
    return True


# ──────────────────────────────────────────────
# CORE: REPORT GENERATION
# ──────────────────────────────────────────────
def generate_report() -> str:
    """Call NVIDIA NIM with the full ACHILLES system prompt. Retries on transient failures."""
    prompt_path = resolve_prompt_path()
    system_prompt = prompt_path.read_text(encoding="utf-8")
    print(f"📄 Loaded system prompt: {len(system_prompt):,} characters from {prompt_path.name}")

    client = OpenAI(
        base_url=BASE_URL,
        api_key=os.environ["NVIDIA_API_KEY"],
    )

    today = jst_today().strftime("%A, %B %d, %Y")
    user_message = (
        f"Today is {today}. "
        "Generate today's Market Intelligence Report following Format A: Weekly Signal Report. "
        "Cover all four intelligence domains. Follow every constraint and output format exactly as specified."
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"🔄 Attempt {attempt}/{MAX_RETRIES} — Calling {MODEL}...")
            completion = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=TEMPERATURE,
                top_p=TOP_P,
                max_tokens=MAX_TOKENS,
            )

            content = completion.choices[0].message.content

            # Validate response isn't empty or garbage
            if not content or len(content.strip()) < 200:
                raise ValueError(
                    f"API returned suspiciously short response ({len(content) if content else 0} chars). "
                    "Likely a model error or content filter."
                )

            print(f"✅ Report generated: {len(content):,} characters")
            return content

        except Exception as e:
            last_error = e
            print(f"⚠️ Attempt {attempt} failed: {type(e).__name__}: {e}")

            # Don't retry auth or permission errors — they won't resolve on their own
            if not is_retryable(e):
                print(f"❌ FATAL: Non-retryable error detected. Aborting immediately.")
                sys.exit(1)

            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY_SECONDS * attempt  # Linear backoff
                print(f"   Retrying in {wait}s...")
                time.sleep(wait)

    # All retries exhausted
    print(f"❌ FATAL: All {MAX_RETRIES} attempts failed. Last error: {last_error}")
    sys.exit(1)


# ──────────────────────────────────────────────
# CORE: GOOGLE DOCS UPLOAD
# ──────────────────────────────────────────────
def get_google_services():
    """Authenticate and return Google Docs + Drive service clients."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(
        creds_json, scopes=GOOGLE_SCOPES
    )
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return docs_service, drive_service


def upload_to_google_docs(content: str) -> str:
    """Create a new Google Doc with the report content and move it to the target folder."""
    docs_service, drive_service = get_google_services()

    # Use JST date for the title so it matches the report's actual day in Japan
    title = f"ACHILLES Report — {jst_today().strftime('%Y-%m-%d')}"
    print(f"📝 Creating Google Doc: '{title}'...")
    doc = docs_service.documents().create(body={"title": title}).execute()
    document_id = doc.get("documentId")

    # Insert content in chunks to avoid Google Docs API limits.
    # Chunks must be inserted in REVERSE order because each insert at index 1
    # pushes previous text forward.
    chunks = [content[i:i + GOOGLE_DOCS_CHUNK_SIZE] for i in range(0, len(content), GOOGLE_DOCS_CHUNK_SIZE)]
    for i, chunk in enumerate(reversed(chunks)):
        requests = [
            {
                "insertText": {
                    "location": {"index": 1},
                    "text": chunk,
                }
            }
        ]
        docs_service.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
    print(f"✅ Content written to document ({len(chunks)} chunk{'s' if len(chunks) > 1 else ''}).")

    # Move to the target folder
    folder_id = os.environ["GOOGLE_FOLDER_ID"]
    try:
        file_metadata = drive_service.files().get(
            fileId=document_id, fields="parents"
        ).execute()
        previous_parents = ",".join(file_metadata.get("parents", []))
        drive_service.files().update(
            fileId=document_id,
            addParents=folder_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()
        print(f"📂 Moved to folder: {folder_id}")
    except Exception as e:
        # Document exists but is orphaned in root — log but don't crash
        print(f"⚠️ WARNING: Doc created (ID: {document_id}) but folder move failed: {e}")
        print(f"   The document exists in the service account's root Drive.")

    doc_url = f"https://docs.google.com/document/d/{document_id}/edit"
    print(f"🔗 Document URL: {doc_url}")
    return document_id


# ──────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    now = jst_now()
    print("=" * 60)
    print(f"  ACHILLES Report Engine — {now.strftime('%Y-%m-%d %H:%M JST')}")
    print("=" * 60)

    try:
        validate_environment()
        report = generate_report()
        upload_to_google_docs(report)
        print("\n✅ Pipeline complete. Report delivered to Google Docs.")
    except SystemExit:
        raise  # Let sys.exit() propagate with its code
    except Exception as e:
        print(f"\n❌ UNHANDLED ERROR:\n{traceback.format_exc()}")
        sys.exit(1)
