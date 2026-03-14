import os
import re
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Scopes needed for creating and writing Docs + placing in Drive folders
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = "token.json"
CREDENTIALS_PATH = "credentials.json"


def get_google_credentials():
    """Handles OAuth2 flow. Opens browser on first run, uses saved token after."""
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"'{CREDENTIALS_PATH}' not found. Download it from Google Cloud Console "
                    "(APIs & Services > Credentials > OAuth 2.0 Client ID > Desktop App)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_or_create_folder(drive_service, folder_name: str) -> str:
    """Find or create an 'ORACLE Reports' folder in Drive. Returns folder ID."""
    query = (
        f"name = '{folder_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        folder_id = files[0]["id"]
        print(f"    -> Found existing Drive folder '{folder_name}' (id: {folder_id})")
        return folder_id
    else:
        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = drive_service.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]
        print(f"    -> Created Drive folder '{folder_name}' (id: {folder_id})")
        return folder_id


def parse_report_into_sections(report_text: str) -> list:
    """
    Parse the markdown report into a list of (type, text) tuples.
    Types: 'title', 'heading1', 'heading2', 'body', 'meta', 'divider'
    """
    elements = []
    lines = report_text.split("\n")

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue
        elif stripped.startswith("# "):
            elements.append(("title", stripped[2:].strip()))
        elif stripped.startswith("## "):
            elements.append(("heading1", stripped[3:].strip()))
        elif stripped.startswith("### "):
            elements.append(("heading2", stripped[4:].strip()))
        elif stripped == "---":
            elements.append(("divider", ""))
        elif stripped.startswith("*") and stripped.endswith("*") and not stripped.startswith("**"):
            elements.append(("meta", stripped.strip("*").strip()))
        elif stripped.startswith("**") and stripped.endswith("**"):
            elements.append(("bold_body", stripped.strip("*").strip()))
        else:
            elements.append(("body", stripped))

    return elements


def build_requests(elements: list) -> list:
    """
    Convert parsed elements into Google Docs API batchUpdate requests.
    Builds the document content with proper styles.
    """
    requests = []
    # We insert at index 1 (after the implicit empty paragraph at start of new doc)
    # and track current insertion index
    insert_index = 1

    # Style config
    TITLE_STYLE = {
        "fontSize": {"magnitude": 24, "unit": "PT"},
        "bold": True,
        "foregroundColor": {"color": {"rgbColor": {"red": 0.1, "green": 0.1, "blue": 0.1}}},
    }
    H1_STYLE = {
        "fontSize": {"magnitude": 14, "unit": "PT"},
        "bold": True,
        "foregroundColor": {"color": {"rgbColor": {"red": 0.15, "green": 0.25, "blue": 0.45}}},
    }
    H2_STYLE = {
        "fontSize": {"magnitude": 12, "unit": "PT"},
        "bold": True,
        "foregroundColor": {"color": {"rgbColor": {"red": 0.3, "green": 0.3, "blue": 0.3}}},
    }
    META_STYLE = {
        "fontSize": {"magnitude": 9, "unit": "PT"},
        "italic": True,
        "foregroundColor": {"color": {"rgbColor": {"red": 0.5, "green": 0.5, "blue": 0.5}}},
    }
    BODY_STYLE = {
        "fontSize": {"magnitude": 10, "unit": "PT"},
    }
    BOLD_BODY_STYLE = {
        "fontSize": {"magnitude": 10, "unit": "PT"},
        "bold": True,
    }

    def insert_text(text, style, paragraph_style="NORMAL_TEXT", add_newline=True):
        nonlocal insert_index
        content = text + ("\n" if add_newline else "")
        length = len(content)

        # Insert the text
        requests.append({
            "insertText": {
                "location": {"index": insert_index},
                "text": content,
            }
        })

        # Apply paragraph style
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": insert_index, "endIndex": insert_index + length},
                "paragraphStyle": {"namedStyleType": paragraph_style},
                "fields": "namedStyleType",
            }
        })

        # Apply text style
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": insert_index, "endIndex": insert_index + length - 1},
                "textStyle": style,
                "fields": ",".join(style.keys()),
            }
        })

        insert_index += length

    def insert_divider():
        nonlocal insert_index
        # Insert a horizontal rule via a styled paragraph
        content = "-" * 60 + "\n"
        length = len(content)
        requests.append({
            "insertText": {
                "location": {"index": insert_index},
                "text": content,
            }
        })
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": insert_index, "endIndex": insert_index + length - 1},
                "textStyle": {
                    "fontSize": {"magnitude": 8, "unit": "PT"},
                    "foregroundColor": {"color": {"rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}}},
                },
                "fields": "fontSize,foregroundColor",
            }
        })
        insert_index += length

    for elem_type, text in elements:
        if elem_type == "title":
            insert_text(text, TITLE_STYLE, "TITLE")
            insert_text("", BODY_STYLE)  # spacing
        elif elem_type == "heading1":
            insert_text("", BODY_STYLE)  # spacing before heading
            insert_text(text, H1_STYLE, "HEADING_1")
        elif elem_type == "heading2":
            insert_text(text, H2_STYLE, "HEADING_2")
        elif elem_type == "meta":
            insert_text(text, META_STYLE)
        elif elem_type == "divider":
            insert_divider()
        elif elem_type == "bold_body":
            insert_text(text, BOLD_BODY_STYLE)
        elif elem_type == "body":
            insert_text(text, BODY_STYLE)

    return requests


def save_to_google_docs(report_text: str, subject: str, folder_name: str = "ORACLE Reports") -> str:
    """
    Creates a formatted Google Doc from the report text.
    Returns the URL of the created document.
    """
    print(f"\n[*] GOOGLE DOCS: Authenticating...")
    creds = get_google_credentials()

    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    # Get or create the ORACLE Reports folder
    print(f"[*] GOOGLE DOCS: Locating Drive folder '{folder_name}'...")
    folder_id = get_or_create_folder(drive_service, folder_name)

    # Create a blank doc in the folder
    date_str = datetime.now().strftime("%Y-%m-%d")
    doc_title = f"Clinical Profile — {subject} — {date_str}"

    print(f"[*] GOOGLE DOCS: Creating document '{doc_title}'...")
    doc = docs_service.documents().create(body={"title": doc_title}).execute()
    doc_id = doc["documentId"]

    # Move it into the ORACLE Reports folder
    drive_service.files().update(
        fileId=doc_id,
        addParents=folder_id,
        removeParents="root",
        fields="id, parents",
    ).execute()

    # Parse report and build style requests
    print(f"[*] GOOGLE DOCS: Formatting and writing content...")
    elements = parse_report_into_sections(report_text)
    requests = build_requests(elements)

    # Send all formatting in one batch
    if requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    print(f"[+] GOOGLE DOCS: Document created successfully.")
    print(f"    -> URL: {doc_url}")

    return doc_url
