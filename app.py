import sys
if sys.platform.startswith("win"):
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import asyncio
import os
import time
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
import logging

# Create logs directory
os.makedirs("logs", exist_ok=True)

# Configure logging with timestamp in filename
log_filename = f"logs/crawler_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Web Crawler API", description="API for crawling websites and saving the data")

# ───── MODELS ─────────────────────────────────────────────────────────────────
class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 1000
    concurrency: int = 20
    max_depth: Optional[int] = None
    output_formats: List[str] = ["json", "txt", "csv"]
    debug_mode: bool = False

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
    # Added debug stats
    debug_stats: Dict[str, Any] = {}

# ───── GLOBALS ────────────────────────────────────────────────────────────────
JOBS: Dict[str, JobStatus] = {}
OUTPUT_DIR = "crawler_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ───── UTILITIES ───────────────────────────────────────────────────────────────
def get_domain(u: str) -> str:
    """Extract domain from URL using tldextract"""
    parts = tldextract.extract(u)
    result = f"{parts.domain}.{parts.suffix}"
    logger.debug(f"get_domain: {u} -> {result}")
    return result

def is_same_domain(domain: str, u: str) -> bool:
    """Check if two URLs belong to same domain"""
    domain_a = get_domain(domain)
    domain_b = get_domain(u)
    result = domain_a == domain_b
    logger.debug(f"is_same_domain: {domain_a} vs {domain_b} -> {result}")
    return result

def clean_text(t: str) -> str:
    """Clean and normalize text"""
    return re.sub(r'\s+', ' ', t).strip()

async def fetch_and_parse(url: str, depth: int, context: Any, start_url: str, semaphore: asyncio.Semaphore, debug_mode: bool = False) -> Tuple[Optional[Dict], Set[str]]:
    """Fetch a page and extract content and links"""
    fetch_start = time.time()
    
    async with semaphore:
        logger.debug(f"Fetching [{depth}] {url}")
        try:
            page = await context.new_page()
            
            # Debug for original script compatibility
            if debug_mode:
                logger.debug(f"Page user agent: {await page.evaluate('navigator.userAgent')}")
                
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state('networkidle')
            html = await page.content()
            await page.close()
        except Exception as e:
            logger.error(f"Error fetching {url}: {type(e).__name__}: {e}")
            return None, set()

    parse_start = time.time()
    logger.debug(f"Fetch completed in {parse_start - fetch_start:.2f}s: {url}")
    
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.title.string) if soup.title and soup.title.string else ""
    desc_tag = soup.find('meta', {'name':'description'})
    meta = clean_text(desc_tag['content']) if desc_tag and desc_tag.has_attr('content') else ""
    blocks = []
    for tag in soup.find_all(['h1','h2','h3','p','li','blockquote']):
        txt = tag.get_text(" ", strip=True)
        if len(txt) > 30:
            blocks.append(clean_text(txt))

    data = {
        'url': url,
        'title': title,
        'meta_description': meta,
        'content': "\n".join(blocks)
    }

    content_length = len(data['content'])
    links_start = time.time()
    logger.debug(f"Content extraction completed in {links_start - parse_start:.2f}s: {url} ({content_length} chars)")
    
    # Extract links
    children = set()
    all_links = 0
    same_domain_links = 0
    
    # Debug comparison between start URL format and get_domain results
    start_domain_from_url = get_domain(start_url)
    logger.debug(f"Start URL: {start_url}, Domain: {start_domain_from_url}")

    start_time_links = time.time()
    for a in soup.find_all('a', href=True):
        all_links += 1
        href = a['href'].strip()
        if href.startswith(("mailto:","tel:","javascript:","#")):
            continue
        href = urljoin(url, href)
        href, _ = urldefrag(href)
        parsed = urlparse(href)
        if parsed.scheme.startswith('http'):
            # IMPORTANT: We'll try both ways to ensure consistency with script version
            is_domain_match = is_same_domain(start_url, href)
            if is_domain_match:
                children.add(href)
                same_domain_links += 1
                if debug_mode and same_domain_links <= 5:  # Log first few links for debugging
                    logger.debug(f"Added link: {href}")
    
    end_time = time.time()
    logger.debug(f"Links extraction completed in {end_time - start_time_links:.2f}s: Found {all_links} links, {same_domain_links} same domain")
    logger.debug(f"Total processing time for {url}: {end_time - fetch_start:.2f}s")
    
    return data, children

async def crawl_site(job_id: str, params: CrawlRequest):
    logger.info(f"[+] Starting crawl job {job_id} at {params.start_url}")
    start_time = datetime.now()
    JOBS[job_id] = JobStatus(
        job_id=job_id,
        status="running",
        start_url=params.start_url,
        start_time=start_time.isoformat(),
        debug_stats={
            "crawl_start_time": time.time(),
            "links_found_total": 0,
            "same_domain_links_total": 0,
            "pages_attempted": 0,
            "pages_successful": 0,
            "queue_max_size": 0,
            "domain": get_domain(params.start_url)
        }
    )
    
    # Extract domain for logging
    start_domain = get_domain(params.start_url)
    logger.info(f"Start domain: {start_domain}")
    
    # Create and configure the queue exactly like the script version
    queue = asyncio.Queue()
    await queue.put((params.start_url, 0))
    visited = set()
    results = []
    semaphore = asyncio.Semaphore(params.concurrency)

    try:
        playwright = await async_playwright().start()
    except PWError as e:
        logger.error(f"[!] Playwright failed to start: {e}")
        JOBS[job_id].status = "failed"
        JOBS[job_id].error = str(e)
        JOBS[job_id].end_time = datetime.now().isoformat()
        return

    browser = await playwright.chromium.launch(headless=True)
    
    # Create context with the same settings as the script
    context = await browser.new_context()
    
    # Block the same resource types as the script
    await context.route("**/*", lambda r, req:
        r.abort() if req.resource_type in ["image","media","font","stylesheet","video"]
        else r.continue_()
    )

    batch_count = 0
    
    while not queue.empty() and len(results) < params.max_pages:
        qsize = queue.qsize()
        
        # Update stats
        JOBS[job_id].debug_stats["queue_max_size"] = max(JOBS[job_id].debug_stats["queue_max_size"], qsize)
        
        batch_count += 1
        logger.info(f"[i] Batch {batch_count}: Queue size: {qsize}, fetched: {len(results)}")
        JOBS[job_id].pages_crawled = len(results)
        
        # Process batch identical to script version
        batch = []
        for _ in range(min(qsize, params.concurrency, params.max_pages - len(results))):
            url, depth = await queue.get()
            if url in visited:
                queue.task_done()
                continue
            visited.add(url)
            batch.append((url, depth))

        if not batch:
            logger.info("No new URLs to process, stopping")
            break

        logger.info(f"Processing batch of {len(batch)} URLs")
        JOBS[job_id].debug_stats["pages_attempted"] += len(batch)
        
        # Create tasks exactly as in script version
        tasks = [fetch_and_parse(url, depth, context, params.start_url, semaphore, params.debug_mode) 
                for url, depth in batch]
        
        batch_links_found = 0
        batch_same_domain_links = 0
        batch_successful = 0
        
        for coro in asyncio.as_completed(tasks):
            data, children = await coro
            if not data:
                continue
                
            results.append(data)
            batch_successful += 1
            
            current_url = data['url']
            current_depth = next(d for u, d in batch if u == current_url)
            
            # Count links for stats
            batch_links_found += len(children)
            
            if (params.max_depth is None or current_depth < params.max_depth):
                new_urls_for_page = 0
                for c in children:
                    if c not in visited:
                        await queue.put((c, current_depth + 1))
                        new_urls_for_page += 1
                        batch_same_domain_links += 1
                    
            queue.task_done()
        
        # Update stats
        JOBS[job_id].debug_stats["links_found_total"] += batch_links_found
        JOBS[job_id].debug_stats["same_domain_links_total"] += batch_same_domain_links
        JOBS[job_id].debug_stats["pages_successful"] += batch_successful
        
        logger.info(f"Batch complete: {batch_successful} pages fetched, {batch_same_domain_links} new URLs added to queue")

    await context.close()
    await browser.close()
    await playwright.stop()
    
    # Record crawl end time
    JOBS[job_id].debug_stats["crawl_end_time"] = time.time()
    JOBS[job_id].debug_stats["crawl_duration"] = JOBS[job_id].debug_stats["crawl_end_time"] - JOBS[job_id].debug_stats["crawl_start_time"]
    
    logger.info(f"[+] Crawl finished. Total pages: {len(results)}")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    domain_key = start_domain.replace(".", "_")
    base_name = f"{domain_key}_{timestamp}"
    out_files = {}

    if results:
        if "txt" in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")
            with open(path, 'w', encoding='utf-8') as f:
                for p in results:
                    f.write(f"## {p['url']}\n")
                    if p['title']:
                        f.write(f"Title: {p['title']}\n")
                    if p['meta_description']:
                        f.write(f"Description: {p['meta_description']}\n\n")
                    f.write(p['content'] + "\n\n")
            out_files['txt'] = path
            logger.info(f"✅ {len(results)} pages → {path}")
            
            # Debug: Add file size to stats
            JOBS[job_id].debug_stats["txt_file_size"] = os.path.getsize(path)

        if "json" in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base_name}.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            out_files['json'] = path
            logger.info(f"✅ {len(results)} pages → {path}")
            
            # Debug: Add file size to stats
            JOBS[job_id].debug_stats["json_file_size"] = os.path.getsize(path)

        if "csv" in params.output_formats:
            path = os.path.join(OUTPUT_DIR, f"{base_name}.csv")
            keys = ['url','title','meta_description','content']
            with open(path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                for row in results:
                    writer.writerow(row)
            out_files['csv'] = path
            logger.info(f"✅ {len(results)} pages → {path}")
            
            # Debug: Add file size to stats
            JOBS[job_id].debug_stats["csv_file_size"] = os.path.getsize(path)
            
        # Calculate total content size for all pages
        total_content_size = sum(len(p['content']) for p in results)
        JOBS[job_id].debug_stats["total_content_chars"] = total_content_size
        JOBS[job_id].debug_stats["avg_content_chars_per_page"] = total_content_size / len(results) if results else 0
    else:
        logger.warning("⚠️ No pages were fetched.")

    JOBS[job_id].status = "completed"
    JOBS[job_id].pages_crawled = len(results)
    JOBS[job_id].end_time = datetime.now().isoformat()
    JOBS[job_id].output_files = out_files

# ───── Create a direct emulation of script version ───────────────────────────
async def direct_crawl_script_emulation(start_url, max_pages=1000, concurrency=20, max_depth=None):
    """Function that directly emulates the original script's behavior without FastAPI overhead"""
    logger.info(f"[+] Starting direct script emulation crawl at {start_url}")
    start_time = time.time()
    
    visited = set()
    queue = asyncio.Queue()
    await queue.put((start_url, 0))
    results = []
    semaphore = asyncio.Semaphore(concurrency)
    
    try:
        playwright = await async_playwright().start()
    except PWError as e:
        logger.error(f"[!] Playwright failed to start: {e}")
        return []

    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    # Block the same resource types
    await context.route("**/*", lambda r, req:
        r.abort() if req.resource_type in ["image","media","font","stylesheet","video"]
        else r.continue_()
    )

    while not queue.empty() and len(results) < max_pages:
        qsize = queue.qsize()
        logger.info(f"[i] Queue size: {qsize}, fetched: {len(results)}")
        
        batch = []
        for _ in range(min(qsize, concurrency, max_pages - len(results))):
            url, depth = await queue.get()
            if url in visited:
                queue.task_done()
                continue
            visited.add(url)
            batch.append((url, depth))

        if not batch:
            break

        # EXACT structure from original script
        tasks = [fetch_and_parse(url, depth, context, start_url, semaphore, True) 
                for url, depth in batch]
        
        for coro in asyncio.as_completed(tasks):
            data, children = await coro
            if not data:
                continue
                
            results.append(data)
            
            current_depth = next(d for u, d in batch if u == data['url'])
            if (max_depth is None or current_depth < max_depth):
                for c in children:
                    if c not in visited:
                        await queue.put((c, current_depth + 1))
            queue.task_done()

    await context.close()
    await browser.close()
    await playwright.stop()
    logger.info(f"[+] Script emulation crawl finished. Total pages: {len(results)}")
    end_time = time.time()
    
    # Calculate stats
    stats = {
        "total_pages": len(results),
        "crawl_duration": end_time - start_time,
        "total_content_chars": sum(len(p['content']) for p in results),
        "avg_content_chars_per_page": sum(len(p['content']) for p in results) / len(results) if results else 0
    }
    
    logger.info(f"Script crawl stats: {json.dumps(stats)}")
    
    # Save outputs like the original script
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    domain_key = get_domain(start_url).replace(".", "_")
    base_name = f"script_{domain_key}_{timestamp}"
    
    if results:
        path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            for p in results:
                f.write(f"## {p['url']}\n")
                if p['title']:
                    f.write(f"Title: {p['title']}\n")
                if p['meta_description']:
                    f.write(f"Description: {p['meta_description']}\n\n")
                f.write(p['content'] + "\n\n")
        logger.info(f"✅ {len(results)} pages → {path}")
        
        path = os.path.join(OUTPUT_DIR, f"{base_name}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        logger.info(f"✅ {len(results)} pages → {path}")
        
        path = os.path.join(OUTPUT_DIR, f"{base_name}.csv")
        keys = ['url','title','meta_description','content']
        with open(path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            for row in results:
                writer.writerow(row)
        logger.info(f"✅ {len(results)} pages → {path}")
    
    return results, stats

# ───── API ENDPOINTS ─────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Web Crawler API with Debug Mode", 
            "endpoints": ["/crawl", "/jobs", "/download", "/debug_crawl"]}

@app.post("/crawl", response_model=CrawlResponse)
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid4())
    logger.info(f"Creating new crawl job: {job_id} for {request.start_url}")
    JOBS[job_id] = JobStatus(
        job_id=job_id, 
        status="queued", 
        start_url=request.start_url, 
        start_time=datetime.now().isoformat()
    )
    background_tasks.add_task(crawl_site, job_id, request)
    return CrawlResponse(
        job_id=job_id, 
        status="queued", 
        start_url=request.start_url, 
        start_time=datetime.now().isoformat(), 
        message="Crawl job started. Check status with /jobs/{job_id}"
    )

@app.get("/debug_crawl")
async def debug_crawl(start_url: str = Query(..., description="URL to crawl"),
                    max_pages: int = Query(100, description="Maximum number of pages to crawl")):
    """Run both API and script versions in parallel for debugging purposes"""
    # Run script-like version
    logger.info(f"Starting debug crawl comparison for {start_url}")
    
    # First run script version
    script_results, script_stats = await direct_crawl_script_emulation(start_url, max_pages)
    
    # Then run API version through normal flow
    job_id = str(uuid4())
    request = CrawlRequest(start_url=start_url, max_pages=max_pages, debug_mode=True)
    JOBS[job_id] = JobStatus(
        job_id=job_id, 
        status="queued", 
        start_url=start_url, 
        start_time=datetime.now().isoformat()
    )
    
    await crawl_site(job_id, request)
    
    # Compare results
    api_stats = JOBS[job_id].debug_stats
    
    comparison = {
        "script_version": script_stats,
        "api_version": api_stats,
        "difference": {
            "pages": script_stats["total_pages"] - api_stats["pages_successful"],
            "content_chars": script_stats["total_content_chars"] - api_stats["total_content_chars"],
            "crawl_time_diff": script_stats["crawl_duration"] - api_stats["crawl_duration"]
        }
    }
    
    return {
        "comparison": comparison,
        "job_id": job_id
    }

@app.get("/jobs", response_model=List[JobStatus])
async def list_jobs(limit: int = Query(10, ge=1, le=100)):
    return sorted(JOBS.values(), key=lambda j: j.start_time, reverse=True)[:limit]

@app.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]

@app.get("/download/{job_id}")
async def download_results(job_id: str, format: str = Query(..., pattern="^(txt|json|csv)$")):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    job = JOBS[job_id]
    if job.status != "completed":
        raise HTTPException(status_code=400, detail=f"Job not completed (status: {job.status})")
    if format not in job.output_files:
        raise HTTPException(status_code=404, detail=f"No {format} output available")
    path = job.output_files[format]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path, 
        filename=os.path.basename(path), 
        media_type=('application/json' if format=='json' else 'text/plain')
    )

@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    for f in JOBS[job_id].output_files.values():
        if os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass
    del JOBS[job_id]
    return {"message": f"Job {job_id} and files deleted"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)