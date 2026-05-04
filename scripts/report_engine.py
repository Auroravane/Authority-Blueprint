"""
Project ACHILLES — Report Engine
Generates a daily Market Intelligence Report using the NVIDIA NIM API
and saves it as a Markdown file in the reports/ directory.
The GitHub Actions workflow then commits it back to the repository.
"""

import os
import sys
import json
import time
import datetime
import traceback
from pathlib import Path
from openai import OpenAI


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
MODEL = "mistralai/mistral-nemotron"
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_TOKENS = 16384
TEMPERATURE = 0.6
TOP_P = 0.7
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30

# JST offset (UTC+9) — all dates in the report are Japan time
JST_OFFSET = datetime.timedelta(hours=9)

REQUIRED_ENV_VARS = ["NVIDIA_API_KEY"]

# Non-retryable HTTP error codes — fail immediately, don't waste time
NON_RETRYABLE_MARKERS = ("401", "403", "invalid_api_key", "authentication")


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def jst_now() -> datetime.datetime:
    """Return the current time in JST (UTC+9)."""
    return datetime.datetime.utcnow() + JST_OFFSET


def jst_today() -> datetime.date:
    """Return today's date in JST (UTC+9)."""
    return jst_now().date()


def is_retryable(error: Exception) -> bool:
    """Auth and permission errors won't fix themselves — don't retry them."""
    error_str = str(error).lower()
    return not any(marker in error_str for marker in NON_RETRYABLE_MARKERS)


def resolve_repo_root() -> Path:
    """Find the repo root whether running locally or in GitHub Actions."""
    return Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parent.parent))


# ──────────────────────────────────────────────
# GUARDS
# ──────────────────────────────────────────────
def validate_environment():
    """Fail fast and clearly if any required secret is missing."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        print(f"❌ FATAL: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    print("✅ Environment validated.")


def resolve_prompt_path() -> Path:
    """Resolve SystemPrompt.md from the repo root."""
    prompt_path = resolve_repo_root() / "SystemPrompt.md"
    if not prompt_path.exists():
        print(f"❌ FATAL: SystemPrompt.md not found at {prompt_path}")
        sys.exit(1)
    return prompt_path


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

            if not content or len(content.strip()) < 200:
                raise ValueError(
                    f"API returned suspiciously short response "
                    f"({len(content) if content else 0} chars). Possible content filter."
                )

            print(f"✅ Report generated: {len(content):,} characters")
            return content

        except Exception as e:
            last_error = e
            print(f"⚠️ Attempt {attempt} failed: {type(e).__name__}: {e}")

            if not is_retryable(e):
                print("❌ FATAL: Non-retryable error (auth/permission). Aborting immediately.")
                sys.exit(1)

            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY_SECONDS * attempt
                print(f"   Retrying in {wait}s...")
                time.sleep(wait)

    print(f"❌ FATAL: All {MAX_RETRIES} attempts failed. Last error: {last_error}")
    sys.exit(1)


# ──────────────────────────────────────────────
# CORE: SAVE REPORT TO REPO
# ──────────────────────────────────────────────
def save_report(content: str) -> Path:
    """
    Save the report as a Markdown file in reports/YYYY-MM-DD.md.
    The GitHub Actions workflow commits this file back to the repo,
    making it readable by any other workflow via actions/checkout or raw GitHub URL.
    """
    repo_root = resolve_repo_root()
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(exist_ok=True)

    date_str = jst_today().strftime("%Y-%m-%d")
    report_path = reports_dir / f"{date_str}.md"

    # Build the full Markdown file with a header
    header = (
        f"# ACHILLES Market Intelligence Report\n"
        f"**Date:** {date_str} (JST)  \n"
        f"**Generated:** {jst_now().strftime('%Y-%m-%d %H:%M JST')}  \n"
        f"**Model:** {MODEL}  \n\n"
        f"---\n\n"
    )

    report_path.write_text(header + content, encoding="utf-8")
    print(f"💾 Report saved to: {report_path}")

    # Also write/update a latest.md symlink-style file so other workflows
    # can always read the most recent report at a fixed path
    latest_path = reports_dir / "latest.md"
    latest_path.write_text(header + content, encoding="utf-8")
    print(f"📌 Latest report also written to: {latest_path}")

    return report_path


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
        save_report(report)
        print("\n✅ Pipeline complete. Report saved to reports/ directory.")
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ UNHANDLED ERROR:\n{traceback.format_exc()}")
        sys.exit(1)
