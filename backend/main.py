from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import os
import requests
import urllib.parse
import re

# Add backend directory to sys.path to allow sibling imports
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from downloader import get_video_info

app = FastAPI(title="Telegram Video Downloader API")

# Enable CORS to allow local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    index_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "ok", "message": "Telegram Video Downloader Bot is running"}

@app.get("/healthz")
def healthz():
    return {"status": "healthy"}

# API Endpoint: Extract video metadata
@app.get("/api/extract")
async def extract_video(url: str = Query(..., description="The social media video URL")):
    try:
        info = get_video_info(url)
        return {
            "success": True,
            "title": info.get("title", "Video"),
            "thumbnail": info.get("thumbnail"),
            "platform": info.get("platform", "Unknown"),
            "duration": info.get("duration", 0),
            "formats": info.get("formats", [])
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

# API Endpoint: Proxy/stream the video file to bypass CORS and trigger direct download
@app.get("/api/download")
async def download_proxy(
    url: str = Query(..., description="The direct video stream URL"),
    title: str = Query("video", description="The title of the video file")
):
    try:
        # Resolve URL decoding if double encoded
        dec_url = urllib.parse.unquote(url)
        
        # Clean title for content disposition header (ASCII only or encoded UTF-8)
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
        # Fallback to simple name if title is empty
        if not clean_title:
            clean_title = "downloaded_video"
            
        # Headers for streaming request
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        req = requests.get(dec_url, headers=headers, stream=True, timeout=(15, 60))
        req.raise_for_status()
        
        # Determine content type and length if available
        content_type = req.headers.get("content-type", "video/mp4")
        content_length = req.headers.get("content-length")
        
        def iter_file():
            try:
                for chunk in req.iter_content(chunk_size=65536):  # 64KB streaming chunks
                    if chunk:
                        yield chunk
            finally:
                req.close()
                    
        response_headers = {
            "Content-Disposition": f'attachment; filename="{clean_title}.mp4"'
        }
        if content_length:
            response_headers["Content-Length"] = content_length
            
        return StreamingResponse(
            iter_file(),
            media_type=content_type,
            headers=response_headers
        )
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to stream video: {str(e)}")

# Serve frontend static files
# Make sure frontend folder exists
os.makedirs("frontend", exist_ok=True)

# Mount the static files at "/" so it serves index.html and other assets
# This is added at the end so it doesn't override /api paths
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
