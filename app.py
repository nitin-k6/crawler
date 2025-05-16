import sys
# Set Proactor event loop policy on Windows BEFORE any asyncio import
if sys.platform.startswith("win"):
    import asyncio
    if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import asyncio
import os
from urllib.parse import urljoin, urldefrag, urlparse
from playwright.async_api import async_playwright, Error as PWError
from bs4 import BeautifulSoup
import tldextract
import re
import json
import csv
from datetime import datetime
from typing import List, Dict, Optional, Set, Tuple, Any
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from uuid import uuid4
import uvicorn

app = FastAPI(title="Web Crawler API", description="API for crawling websites and saving the data")

# ───── MODELS ─────────────────────────────────────────────────────────────────
class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 100
    concurrency: int = 10
    max_depth: Optional[int] = None
    output_formats: List[str] = ["json", "txt", "csv"]

class CrawlResponse(BaseModel):
    job_id: str
    status: str
    start_url: str
    start_time: str
    message: str

class JobStatus(BaseModel):
    job_id: str
    status: str
    start_url: str
    start_time: str
    end_time: Optional[str] = None
    pages_crawled: int = 0
    error: Optional[str] = None
    output_files: Dict[str, str] = {}

# ───── GLOBALS ────────────────────────────────────────────────────────────────
JOBS: Dict[str, JobStatus] = {}
OUTPUT_DIR = "crawler_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ───── UTILITIES ───────────────────────────────────────────────────────────────
def normalize_url(u: str) -> str:
    parsed = urlparse(u)
    if not parsed.scheme:
        return 'https://' + u
    return u

def get_domain(u: str) -> str:
    parts = tldextract.extract(u)
    return f"{parts.domain}.{parts.suffix}"

def is_same_domain(start_url: str, other: str) -> bool:
    return get_domain(start_url) == get_domain(other)

def clean_text(t: str) -> str:
    return re.sub(r'\s+', ' ', t).strip()

async def safe_goto(page, url, retries=2, timeout=20000):
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, timeout=timeout)
            return True
        except Exception as e:
            print(f"    [!] Goto failed (attempt {attempt}): {e}")
            if attempt < retries:
                await asyncio.sleep(2)
    return False

async def fetch_and_parse(url: str, depth: int, context: Any, start_url: str,page_pool: asyncio.Queue) -> Tuple[Optional[Dict], Set[str]]:
    try:
        print(f"  → Fetching [{depth}] {url}")
        page = await page_pool.get()
        try:
            success = await safe_goto(page, url)
            if not success:
                return None, set()
            await page.wait_for_load_state('domcontentloaded')
            html = await page.content()
        finally:
            await page_pool.put(page)
    except Exception as e:
        print(f"    [!] Error: {e}")
        return None, set()

    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.title.string) if soup.title and soup.title.string else ""
    desc = soup.find('meta', {'name': 'description'})
    meta = clean_text(desc['content']) if desc and desc.has_attr('content') else ""
    content_blocks = [clean_text(tag.get_text(' ', strip=True))
                      for tag in soup.find_all(['h1','h2','h3','p','li','blockquote'])
                      if len(tag.get_text(' ', strip=True)) > 30]

    data = {'url': url, 'title': title, 'meta_description': meta, 'content': '\n'.join(content_blocks)}

    children: Set[str] = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('mailto:','tel:','javascript:','#')):
            continue
        href = urljoin(url, href)
        href, _ = urldefrag(href)
        href = normalize_url(href)
        if href.startswith(('http://','https://')) and is_same_domain(start_url, href):
            children.add(href)

    return data, children

async def crawl_site(job_id: str, params: CrawlRequest):
    start_time = datetime.now()
    norm_start = normalize_url(params.start_url)
    JOBS[job_id] = JobStatus(job_id=job_id, status='running', start_url=norm_start, start_time=start_time.isoformat())

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put((norm_start, 0))
    visited: Set[str] = set()
    results: List[Dict] = []

    try:
        playwright = await async_playwright().start()
    except PWError as e:
        JOBS[job_id].status = 'failed'
        JOBS[job_id].error = str(e)
        JOBS[job_id].end_time = datetime.now().isoformat()
        return

    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    await context.route('**/*', lambda r, req: r.abort() if req.resource_type in ['image','media','font','stylesheet','video'] else r.continue_())

    page_pool = asyncio.Queue()
    for _ in range(min(params.concurrency, 10)):
        page = await context.new_page()
        await page_pool.put(page)

    while not queue.empty() and len(results) < params.max_pages:
        JOBS[job_id].pages_crawled = len(results)
        batch = []
        for _ in range(min(queue.qsize(), params.concurrency, params.max_pages - len(results))):
            url, depth = await queue.get()
            if url not in visited:
                batch.append((url, depth))

        tasks = [fetch_and_parse(url, depth, context, normalize_url(params.start_url), page_pool) for url, depth in batch]
        for coro in asyncio.as_completed(tasks):
            try:
                data, children = await coro
                if not data:
                    continue
                results.append(data)
                visited.add(data['url'])  
                next_depth = next((d for u, d in batch if u == data['url']), 0) + 1
                if params.max_depth is None or next_depth <= params.max_depth:
                    for c in children:
                        if c not in visited:
                            await queue.put((c, next_depth))
            except Exception as e:
                print(f"    [!] Task error: {e}")

    # Cleanup reusable pages
    while not page_pool.empty():
        page = await page_pool.get()
        await page.close()

    await context.close()
    await browser.close()
    await playwright.stop()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = f"{get_domain(norm_start).replace('.', '_')}_{timestamp}"
    out: Dict[str, str] = {}
    if results:
        if 'json' in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base}.json")
            with open(path, 'w', encoding='utf-8') as f: json.dump(results, f, indent=2, ensure_ascii=False)
            out['json'] = path
        if 'txt' in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base}.txt")
            with open(path, 'w', encoding='utf-8') as f:
                for p in results:
                    f.write(f"## {p['url']}\nTitle: {p['title']}\nDescription: {p['meta_description']}\n{p['content']}\n\n")
            out['txt'] = path
        if 'csv' in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base}.csv")
            with open(path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['url','title','meta_description','content'])
                writer.writeheader(); writer.writerows(results)
            out['csv'] = path

    JOBS[job_id].status = 'completed'
    JOBS[job_id].pages_crawled = len(results)
    JOBS[job_id].end_time = datetime.now().isoformat()
    JOBS[job_id].output_files = out

# ───── API ENDPOINTS ─────────────────────────────────────────────────────────
@app.get('/')
async def root(): return {'message':'Welcome to Web Crawler API','endpoints':['/crawl','/jobs','/download']}

@app.post('/crawl', response_model=CrawlResponse)
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid4())
    background_tasks.add_task(crawl_site, job_id, request)
    return CrawlResponse(job_id=job_id, status='queued', start_url=request.start_url, start_time=datetime.now().isoformat(), message='Crawl initiated')

@app.get('/jobs', response_model=List[JobStatus])
async def list_jobs(limit: int = Query(10,ge=1,le=100)):
    return sorted(JOBS.values(), key=lambda j: j.start_time, reverse=True)[:limit]

@app.get('/jobs/{job_id}', response_model=JobStatus)
async def get_job(job_id: str):
    if job_id not in JOBS: raise HTTPException(404,'Job not found')
    return JOBS[job_id]

@app.get('/download/{job_id}')
async def download(job_id: str, format: str = Query(...,pattern='^(json|txt|csv)$')):
    if job_id not in JOBS: raise HTTPException(404,'Job not found')
    job = JOBS[job_id]
    if job.status!='completed': raise HTTPException(400,f'Job {job.status}')
    if format not in job.output_files: raise HTTPException(404,f'No {format}')
    return FileResponse(job.output_files[format],filename=os.path.basename(job.output_files[format]))

@app.delete('/jobs/{job_id}')
async def delete_job(job_id: str):
    if job_id not in JOBS: raise HTTPException(404,'Job not found')
    for f in JOBS[job_id].output_files.values():
        try: os.remove(f)
        except: pass
    del JOBS[job_id]
    return {'message':f'Job {job_id} deleted'}

if __name__=='__main__':
    uvicorn.run('app:app',host='0.0.0.0',port=8000,reload=False)

