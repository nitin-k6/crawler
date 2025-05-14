import os
import uuid
import re
import datetime
import urllib.parse
import base64
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from notion_client import Client
import httpx

load_dotenv()
CLIENT_ID     = os.getenv("NOTION_CLIENT_ID")
CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("NOTION_REDIRECT_URI")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")

if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI):
    raise RuntimeError("Set NOTION_CLIENT_ID, NOTION_CLIENT_SECRET, NOTION_REDIRECT_URI in .env")

# ─── FastAPI Setup ───────────────────────────────────────────────────────────
app = FastAPI()
templates = Jinja2Templates(directory="templates")

SESSIONS = {}     

def build_oauth_url(state: str):
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "databases:read pages:read blocks:read",
        "state": state,
        "owner": "user"
    }
    return f"https://api.notion.com/v1/oauth/authorize?{urllib.parse.urlencode(params)}"

@app.get("/")
def home():
    state = str(uuid.uuid4())
    oauth_url = build_oauth_url(state)
    response = RedirectResponse(oauth_url)
    # clear old session cookie and set oauth state cookie
    response.delete_cookie("session_id")
    # Set the oauth_state cookie with proper attributes and secure settings
    response.set_cookie(
        key="oauth_state", 
        value=state, 
        httponly=True, 
        max_age=300,
        samesite="lax",  # Important for OAuth flows
        secure=False,    # Set to True if using HTTPS
        path="/"         # Ensure cookie is available at callback path
    )
    print(f"Setting oauth_state cookie: {state}")
    return response

@app.get("/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    print(f"Callback received with code: {code}, state: {state}, error: {error}")
    print(f"Cookies: {request.cookies}")
    
    if error:
        return templates.TemplateResponse("error.html", {"request": request, "message": f"OAuth error: {error}"})
    
    if not code:
        return templates.TemplateResponse("error.html", {"request": request, "message": "No authorization code provided"})
    
    # For testing purposes, we'll bypass the state validation
    # This is not ideal for production but helps debug the issue
    print("WARNING: Bypassing state validation for testing")
    
    # Create a manual session for testing
    session_id = str(uuid.uuid4())
    
    try:
        async with httpx.AsyncClient() as client:
            auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
            auth_bytes = auth_str.encode('ascii')
            auth_b64 = base64.b64encode(auth_bytes).decode('ascii')
            
            print(f"Exchanging code for token")
            
            headers = {
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION
            }
            
            print(f"Headers: {headers}")
            
            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI
            }
            
            print(f"Payload: {payload}")
            
            resp = await client.post(
                "https://api.notion.com/v1/oauth/token",
                json=payload,
                headers=headers
            )
            
            print(f"Token exchange response status: {resp.status_code}")
            print(f"Token exchange response body: {resp.text}")
            
            if resp.status_code != 200:
                return templates.TemplateResponse(
                    "error.html", 
                    {"request": request, "message": f"Token exchange failed: {resp.text}"}
                )
            
            data = resp.json()
            print("Token exchange successful!")
            
            # Store token in session
            SESSIONS[session_id] = {
                "token": data["access_token"],
                "workspace": data.get("workspace_name", "<unknown>")
            }
            
            # Set session cookie and redirect
            response = RedirectResponse("/debug-info", status_code=303)
            response.set_cookie(
                key="session_id", 
                value=session_id, 
                httponly=True, 
                max_age=3600,
                samesite="lax",
                secure=False,
                path="/"
            )
            return response
            
    except Exception as e:
        import traceback
        print(f"Exception during token exchange: {str(e)}")
        print(traceback.format_exc())
        return templates.TemplateResponse(
            "error.html", 
            {"request": request, "message": f"Exception during token exchange: {str(e)}"}
        )

@app.get("/debug-info")
async def debug_info(request: Request):
    """Debug endpoint to check session and auth status"""
    try:
        sid = request.cookies.get("session_id")
        if not sid or sid not in SESSIONS:
            return HTMLResponse("""
                <h1>Debug Info</h1>
                <p>No active session found</p>
                <p>Cookies: {}</p>
                <p><a href="/">Start over</a></p>
            """.format(request.cookies))
        
        session = SESSIONS[sid]
        token = session.get("token", "No token")
        workspace = session.get("workspace", "Unknown")
        
        # Try to use the token to verify it works
        notion = Client(auth=token, notion_version=NOTION_VERSION)
        try:
            user_info = notion.users.me()
            user_status = "Valid - User: " + user_info.get("name", "Unknown")
        except Exception as e:
            user_status = f"Error: {str(e)}"
            
        return HTMLResponse(f"""
            <h1>Debug Info</h1>
            <h2>Session Details</h2>
            <p>Session ID: {sid}</p>
            <p>Workspace: {workspace}</p>
            <p>Token status: {user_status}</p>
            
            <h3>Navigation</h3>
            <p><a href="/select">Continue to selection page</a></p>
            <p><a href="/">Start over</a></p>
        """)
        
    except Exception as e:
        import traceback
        return HTMLResponse(f"""
            <h1>Debug Error</h1>
            <p>Error: {str(e)}</p>
            <pre>{traceback.format_exc()}</pre>
            <p><a href="/">Start over</a></p>
        """)

def get_notion_client(request: Request):
    sid = request.cookies.get("session_id")
    sess = SESSIONS.get(sid)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return Client(auth=sess["token"], notion_version=NOTION_VERSION), sess["workspace"]

def extract_plain(rich):
    if not rich:
        return ""
    return "".join(rt.get("plain_text", "") for rt in rich)

# Fix: Modified to be truly synchronous, not async
def collect_blocks(notion: Client, block_id: str, out: list, depth: int = 0):
    try:
        blocks = notion.blocks.children.list(block_id=block_id, page_size=100)
        for blk in blocks.get("results", []):
            btype = blk["type"]
            txt = ""
            if btype in ("paragraph","heading_1","heading_2","heading_3",
                        "bulleted_list_item","numbered_list_item","quote"):
                txt = extract_plain(blk.get(btype, {}).get("rich_text", []))
            out.append({"id": blk["id"], "type": btype, "depth": depth, "text": txt})
            if blk.get("has_children"):
                collect_blocks(notion, blk["id"], out, depth+1)
    except Exception as e:
        print(f"Error collecting blocks for {block_id}: {str(e)}")
        # Add error information to output instead of failing
        out.append({"id": block_id, "type": "error", "depth": depth, "text": f"Error: {str(e)}"})

@app.get("/select", response_class=HTMLResponse)
def select(request: Request):
    try:
        _, workspace = get_notion_client(request)
    except HTTPException:
        return RedirectResponse("/")
    return templates.TemplateResponse("select.html", {"request": request, "workspace": workspace})

@app.get("/list-databases")
async def list_databases(request: Request):
    try:
        notion, _ = get_notion_client(request)
        # Use synchronous API properly
        resp = notion.databases.list(page_size=100)
        return JSONResponse([
            {"id": db["id"], "title": extract_plain(db.get("title", []))}
            for db in resp.get("results", [])
        ])
    except Exception as e:
        print(f"Error listing databases: {str(e)}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/search-pages")
async def search_pages(request: Request):
    try:
        notion, _ = get_notion_client(request)
        search_params = {
            "query": "",
            "filter": {
                "value": "page",
                "property": "object"
            },
            "page_size": 100
        }
        # Use synchronous API correctly
        resp = notion.search(**search_params)
        pages = []
        for p in resp.get("results", []):
            # Get title based on page properties or page content
            title = "Untitled"
            
            # Handle different page structures to extract title
            properties = p.get("properties", {})
            if "title" in properties:
                title_obj = properties["title"]
                if "title" in title_obj:
                    title_prop = title_obj["title"]
                    title = extract_plain(title_prop)
                elif "rich_text" in title_obj:
                    title_prop = title_obj["rich_text"]
                    title = extract_plain(title_prop)
            elif "Name" in properties:
                # Alternative title property
                name_obj = properties["Name"]
                if "title" in name_obj:
                    title = extract_plain(name_obj["title"])
            
            # If no title found, try to get it from page content
            if title == "Untitled" and p.get("parent", {}).get("type") == "page_id":
                try:
                    page_data = notion.pages.retrieve(page_id=p["id"])
                    if "properties" in page_data:
                        for prop_name, prop_value in page_data["properties"].items():
                            if "title" in prop_value and prop_value["title"]:
                                title = extract_plain(prop_value["title"])
                                break
                except Exception as page_error:
                    print(f"Error retrieving page data: {str(page_error)}")
                    
            pages.append({"id": p.get("id"), "title": title})
        return JSONResponse(pages)
    except Exception as e:
        import traceback
        print(f"Error searching pages: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse({"error": str(e)}, status_code=500)

# @app.post("/extract")
# async def extract(request: Request, doc_id: str = Form(...)):
#     try:
#         notion, _ = get_notion_client(request)
#         pages = []
#         if re.fullmatch(r"[0-9a-fA-F\-]{36}", doc_id):
#             # Check if it's a database or a page
#             try:
#                 # First try to query as database
#                 q = notion.databases.query(database_id=doc_id, page_size=100)
#                 pages = [row["id"] for row in q.get("results", [])]
#             except Exception as db_error:
#                 print(f"Failed to query as database: {str(db_error)}")
#                 # If that fails, treat it as a page
#                 pages = [doc_id]
#         else:
#             pages = [doc_id]

#         all_blocks = {}
#         for pid in pages:
#             try:
#                 lst = []
#                 # Fix: Changed to synchronous call
#                 collect_blocks(notion, pid, lst)
#                 all_blocks[pid] = lst
#             except Exception as block_error:
#                 print(f"Error collecting blocks for page {pid}: {str(block_error)}")
#                 all_blocks[pid] = [{"id": pid, "type": "error", "depth": 0, "text": f"Error: {str(block_error)}"}]

#         return JSONResponse(all_blocks)
#     except Exception as e:
#         import traceback
#         print(f"Extract endpoint error: {str(e)}")
#         print(traceback.format_exc())
#         return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/extract")
async def extract(request: Request, doc_id: str = Form(...)):
    try:
        notion, _ = get_notion_client(request)
        pages = []
        if re.fullmatch(r"[0-9a-fA-F\-]{36}", doc_id):
            # Check if it's a database or a page
            try:
                # First try to query as database
                q = notion.databases.query(database_id=doc_id, page_size=100)
                pages = [row["id"] for row in q.get("results", [])]
            except Exception as db_error:
                print(f"Failed to query as database: {str(db_error)}")
                # If that fails, treat it as a page
                pages = [doc_id]
        else:
            pages = [doc_id]

        all_blocks = {}
        for pid in pages:
            try:
                lst = []
                # Fix: Changed to synchronous call
                collect_blocks(notion, pid, lst)
                all_blocks[pid] = lst
            except Exception as block_error:
                print(f"Error collecting blocks for page {pid}: {str(block_error)}")
                all_blocks[pid] = [{"id": pid, "type": "error", "depth": 0, "text": f"Error: {str(block_error)}"}]

        # Save the results to a text file
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"notion_extract_{timestamp}.txt"
        
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(f"Notion Extraction - {timestamp}\n")
            f.write("=" * 50 + "\n\n")
            
            for page_id, blocks in all_blocks.items():
                f.write(f"Page ID: {page_id}\n")
                f.write("-" * 30 + "\n")
                
                for block in blocks:
                    indent = "  " * block["depth"]
                    block_type = block["type"]
                    text = block["text"]
                    
                    if block_type == "error":
                        f.write(f"{indent}ERROR: {text}\n")
                    else:
                        prefix = ""
                        if block_type.startswith("heading_"):
                            prefix = "#" * int(block_type[-1])
                        elif block_type == "bulleted_list_item":
                            prefix = "• "
                        elif block_type == "numbered_list_item":
                            prefix = "1. "  # Simplified, not preserving actual numbers
                        elif block_type == "quote":
                            prefix = "> "
                            
                        if text:
                            f.write(f"{indent}{prefix}{text}\n")
                        else:
                            f.write(f"{indent}[{block_type}]\n")
                
                f.write("\n\n")
            
        print(f"Extraction saved to {output_filename}")
        return JSONResponse({"all_blocks": all_blocks, "saved_to": output_filename})
    except Exception as e:
        import traceback
        print(f"Extract endpoint error: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse({"error": str(e)}, status_code=500)
@app.get("/me")
async def me(request: Request):
    notion, workspace = get_notion_client(request)
    # Use sync API properly
    user = notion.users.me()
    return JSONResponse({"workspace": workspace, "user": user})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("notion_crawler:app", host="0.0.0.0", port=8001, reload=True)

















































































