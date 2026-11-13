import re
import json
import uuid
import shutil
import requests
from urllib.parse import urlparse
import os

MAX_FILE_SIZE_BYTES = 1024 * 1024 * 1024  # 1GB limit
STREAM_CHUNK_SIZE = 65536  # 64KB streaming chunks (keeps memory low)

def _get_ytdlp():
    """Lazy-load yt_dlp only when needed to keep idle RAM low."""
    import yt_dlp
    return yt_dlp

def parse_html(html_text):
    """Lazy-load BeautifulSoup only when HTML scraping is needed."""
    from bs4 import BeautifulSoup
    return BeautifulSoup(html_text, "html.parser")

def has_ffmpeg():
    """Return True if native ffmpeg binary is available on PATH."""
    return bool(shutil.which("ffmpeg"))

def get_ytdl_format(format_id=None):
    """Select yt-dlp format string cleanly based on ffmpeg availability."""
    if format_id and format_id != 'default':
        return format_id
    if has_ffmpeg():
        return 'bestvideo[vcodec!^=av01][vcodec!^=vp09][vcodec!^=vp9]+bestaudio/best[vcodec!^=av01][vcodec!^=vp09][vcodec!^=vp9]/best'
    return 'best[vcodec!^=av01][vcodec!^=vp09][vcodec!^=vp9][ext=mp4]/best[ext=mp4]/best'

def check_free_disk(required_bytes, target_dir):
    """Raise a clear error if available disk space is smaller than required file size."""
    if not required_bytes or required_bytes <= 0:
        return
    try:
        free_bytes = shutil.disk_usage(target_dir).free
        if required_bytes > free_bytes * 0.92:
            req_mb = required_bytes / (1024 * 1024)
            free_mb = free_bytes / (1024 * 1024)
            raise Exception(f"Not enough server disk space ({req_mb:.1f}MB needed, {free_mb:.1f}MB free).")
    except OSError:
        pass

class DownloadCancelledException(Exception):
    pass

# Create temporary directory for downloads
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

def resolve_url(url):
    """Resolve shortened URLs (like xhslink.com or v.kuaishou.com)"""
    if "xhslink.com" in url or "v.kuaishou.com" in url or "v.douyin.com" in url or "vt.tiktok.com" in url:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.head(url, allow_redirects=True, headers=headers, timeout=10)
        return res.url
    return url

def extract_xiaohongshu(url):
    """Custom parser for Xiaohongshu (Rednote) if yt-dlp fails"""
    url = resolve_url(url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.xiaohongshu.com/'
    }
    res = requests.get(url, headers=headers, timeout=15)
    if res.status_code != 200:
        raise Exception(f"Failed to fetch Xiaohongshu page: status {res.status_code}")
    
    soup = parse_html(res.text)
    script = soup.find("script", string=re.compile("window.__INITIAL_STATE__"))
    if not script:
        raise Exception("Could not find initial state in Xiaohongshu page")
    
    script_text = script.string
    json_text = script_text.split("window.__INITIAL_STATE__=")[1].strip()
    json_text = json_text.replace("undefined", "null")
    if json_text.endswith(";"):
        json_text = json_text[:-1]
        
    state = json.loads(json_text)
    
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    note_id = None
    if "explore" in parts:
        note_id = parts[parts.index("explore") + 1]
    
    note_data = None
    if 'note' in state and 'noteDetailMap' in state['note']:
        detail_map = state['note']['noteDetailMap']
        if note_id and note_id in detail_map:
            note_data = detail_map[note_id]
        else:
            note_data = list(detail_map.values())[0]
    else:
        note_data = state.get('note', {}).get('note', {})

    if not note_data:
        raise Exception("Failed to extract note data from initial state")

    note_card = note_data.get('note', {}) or note_data
    video_info = note_card.get('video', {})
    
    title = note_card.get('title') or note_card.get('desc', '')[:50] or "Xiaohongshu Video"
    thumbnail = note_card.get('imageList', [{}])[0].get('url') or video_info.get('cover', {}).get('url')
    
    video_url = None
    if video_info:
        media = video_info.get('media', {})
        stream = media.get('stream', {})
        for codec in ['h264', 'h265']:
            if codec in stream and stream[codec]:
                formats = stream[codec]
                if isinstance(formats, list) and formats:
                    video_url = formats[0].get('masterUrl')
                    if video_url:
                        break
    
    if not video_url:
        raise Exception("No video found in Xiaohongshu note. This might be an image post.")
        
    return {
        "title": title,
        "thumbnail": thumbnail,
        "video_url": video_url,
        "platform": "Xiaohongshu",
        "duration": video_info.get('duration', 0) / 1000.0 if video_info.get('duration') else 0,
        "formats": [{
            "format_id": "default",
            "url": video_url,
            "ext": "mp4",
            "resolution": "default",
            "filesize": None
        }]
    }

def extract_kuaishou(url):
    """Custom parser for Kuaishou ensuring full-length video extraction"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
        'Referer': 'https://v.kuaishou.com/'
    }
    
    # 1. First probe redirect without following to avoid SSL timeouts on expired links
    target_url = url
    try:
        init_res = requests.get(url, headers=headers, allow_redirects=False, timeout=10)
        if init_res.status_code in (301, 302, 303, 307, 308):
            loc = init_res.headers.get("Location", "")
            parsed_loc = urlparse(loc)
            if not parsed_loc.path.strip("/") and ("kuaishou.com" in parsed_loc.netloc.lower()):
                raise Exception("តំណភ្ជាប់ Kuaishou នេះផុតកំណត់ ឬត្រូវបានលុបចោលហើយ (Kuaishou link expired or deleted).")
            if loc:
                target_url = loc
    except Exception as e:
        if "ផុតកំណត់" in str(e):
            raise e

    # Request the full target page
    res = requests.get(target_url, headers=headers, allow_redirects=True, timeout=15)
    if res.status_code != 200:
        raise Exception(f"Failed to fetch Kuaishou page: status {res.status_code}")
    final_path = urlparse(res.url).path.strip("/")
    if not final_path and ("kuaishou.com" in res.url):
        raise Exception("តំណភ្ជាប់ Kuaishou នេះផុតកំណត់ ឬត្រូវបានលុបចោលហើយ (Kuaishou link expired or deleted).")
        
    soup = parse_html(res.text)
    video_url = None
    
    # 1. Search for direct video tag src (ensure it is not a partial .ts segment)
    video_tag = soup.find("video")
    if video_tag and video_tag.get("src"):
        src = video_tag.get("src")
        if not (".ts?" in src or ".ts\"" in src or ".0000" in src or "hls-ts" in src):
            video_url = src
        
    # 2. Try regex search for srcNoMark or src
    if not video_url:
        match = re.search(r'"srcNoMark"\s*:\s*"([^"]+)"', res.text)
        if match:
            candidate = match.group(1).replace("\\u002F", "/").replace("\\/", "/")
            if not (".ts?" in candidate or ".0000" in candidate or "hls-ts" in candidate or "REALTIME_REMUX" in candidate):
                video_url = candidate
        else:
            match = re.search(r'"src"\s*:\s*"([^"]+)"', res.text)
            if match:
                candidate = match.group(1).replace("\\u002F", "/").replace("\\/", "/")
                if not (".ts?" in candidate or ".0000" in candidate or "hls-ts" in candidate or "REALTIME_REMUX" in candidate):
                    video_url = candidate
                
    # 3. Fallback: Search for all true full MP4 URLs in the response text
    if not video_url:
        all_mp4 = re.findall(r'https?://[^\s"\'`<>\\}]+?\.mp4[^\s"\'`<>\\}]*', res.text)
        valid_mp4s = []
        for l in all_mp4:
            clean_l = l.replace("\\u002F", "/").replace("\\/", "/")
            if ".ts?" in clean_l or ".ts\"" in clean_l or ".m3u8" in clean_l or ".0000" in clean_l or "hls-ts" in clean_l or "REALTIME_REMUX" in clean_l:
                continue
            valid_mp4s.append(clean_l)
        if valid_mp4s:
            video_url = valid_mp4s[0]
            
    # 4. If only m3u8 is present, fallback to the full m3u8 playlist URL
    if not video_url:
        m3u8_links = re.findall(r'https?://[^\s"\'`<>\\}]+?\.m3u8[^\s"\'`<>\\}]*', res.text)
        if m3u8_links:
            video_url = m3u8_links[0].replace("\\u002F", "/").replace("\\/", "/")
            
    if not video_url:
        raise Exception("Failed to extract full Kuaishou video URL")
        
    # Extract caption & author for title
    caption_match = re.search(r'"caption"\s*:\s*"([^"]+)"', res.text)
    user_match = re.search(r'"userName"\s*:\s*"([^"]+)"', res.text)
    caption = ""
    if caption_match:
        try:
            caption = json.loads(f'"{caption_match.group(1)}"')
        except Exception:
            caption = caption_match.group(1).encode('utf-8').decode('unicode_escape', errors='ignore')
            
    author = ""
    if user_match:
        try:
            author = json.loads(f'"{user_match.group(1)}"')
        except Exception:
            author = user_match.group(1).encode('utf-8').decode('unicode_escape', errors='ignore')

    title = ""
    if caption and author:
        title = f"{caption} - {author}"
    elif caption:
        title = caption
    elif author:
        title = f"Kuaishou Video by {author}"
        
    if not title:
        title_tag = soup.find("title")
        title = title_tag.text.split("- 快手")[0].strip() if title_tag else "Kuaishou Video"
    if not title or title == "快手":
        title = "Kuaishou Video"
    
    # Extract duration, width, height from photo metadata
    duration = 0
    width = 0
    height = 0
    dur_match = re.search(r'"duration"\s*:\s*([0-9]+)\s*,\s*"likeCount"', res.text)
    if not dur_match:
        dur_match = re.search(r'"adaptationSet"\s*:\s*\[\{"duration"\s*:\s*([0-9]+)', res.text)
    if dur_match:
        duration = int(int(dur_match.group(1)) / 1000)
    else:
        all_durs = [int(x) for x in re.findall(r'"duration"\s*:\s*([0-9]+)', res.text) if int(x) > 1000 and int(x) != 30000]
        if all_durs:
            duration = int(max(all_durs) / 1000)

    w_match = re.search(r'"width"\s*:\s*([0-9]+)', res.text)
    h_match = re.search(r'"height"\s*:\s*([0-9]+)', res.text)
    if w_match:
        width = int(w_match.group(1))
    if h_match:
        height = int(h_match.group(1))
    
    # Extract cover/thumbnail
    thumbnail = None
    covers = re.search(r'"coverUrls"\s*:\s*\[([^\]]+)\]', res.text)
    if covers:
        c_match = re.search(r'"url"\s*:\s*"([^"]+)"', covers.group(1))
        if c_match:
            thumbnail = c_match.group(1).replace("\\u002F", "/").replace("\\/", "/")
            
    if not thumbnail:
        cover_match = re.search(r'"poster"\s*:\s*"([^"]+)"', res.text)
        if cover_match:
            thumbnail = cover_match.group(1).replace("\\u002F", "/").replace("\\/", "/")
        else:
            imgs = re.findall(r'https?://[^\s"\'`<>\\}]+?\.(?:jpe?g|webp|png)[^\s"\'`<>\\}]*', res.text)
            if imgs:
                thumbnail = imgs[0].replace("\\u002F", "/").replace("\\/", "/")
            
    return {
        "title": title,
        "thumbnail": thumbnail,
        "video_url": video_url,
        "platform": "Kuaishou",
        "duration": duration,
        "width": width,
        "height": height,
        "formats": [{
            "format_id": "default",
            "url": video_url,
            "ext": "mp4",
            "resolution": f"{width}x{height}" if width and height else "default",
        }]
    }



def extract_dramabox(url):
    """Custom logic/fallback for DramaBox"""
    # DramaBox has a web portal at dramaboxdb.com or dramabox.com
    # Due to DRM, direct downloads of paid/tokenized content are blocked
    # We will search the page to see if there's any public video URL (free episodes)
    url = resolve_url(url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            soup = parse_html(res.text)
            # Look for video or m3u8 source
            video_tag = soup.find("video")
            video_url = None
            if video_tag:
                source = video_tag.find("source")
                if source and source.get("src"):
                    video_url = source.get("src")
                elif video_tag.get("src"):
                    video_url = video_tag.get("src")
            
            if not video_url:
                # Look for m3u8 in script tags
                matches = re.findall(r'https?://[^\s"\'`]+\.m3u8[^\s"\'`]*', res.text)
                if matches:
                    video_url = matches[0]
            
            if video_url:
                title = "DramaBox Episode"
                title_tag = soup.find("title") or soup.find("h1")
                if title_tag:
                    title = title_tag.text.strip()
                return {
                    "title": title,
                    "thumbnail": None,
                    "video_url": video_url,
                    "platform": "DramaBox",
                    "duration": 0,
                    "formats": [{
                        "format_id": "default",
                        "url": video_url,
                        "ext": "mp4" if ".mp4" in video_url else "m3u8",
                        "resolution": "default",
                        "filesize": None
                    }]
                }
    except Exception as e:
        print(f"DramaBox extraction error: {e}")
        
    raise Exception(
        "DramaBox videos are protected by Digital Rights Management (DRM) and cannot be downloaded directly. "
        "Please use the official DramaBox app to watch this episode."
    )

def extract_tiktok(url):
    """Custom multi-engine parser for TikTok (TikWM + MusicalDown), bypassing IP blocks and watermarks"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
    }
    
    # Engine 1: TikWM API (Fastest & most reliable without watermark)
    try:
        res = requests.post("https://www.tikwm.com/api/", data={"url": url, "hd": 1}, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == 0:
                d = data.get("data", {})
                title = d.get("title") or "TikTok Video"
                thumbnail = d.get("cover")
                
                # Photo slideshow / album
                images = d.get("images")
                if images and isinstance(images, list) and len(images) > 0:
                    music = d.get("music") or d.get("music_info", {}).get("play")
                    return {
                        "title": title,
                        "thumbnail": thumbnail or images[0],
                        "images": images,
                        "audio_url": music,
                        "platform": "TikTok",
                        "is_slideshow": True,
                        "duration": d.get("duration", 0),
                        "formats": []
                    }
                    
                # Video file
                video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                if video_url:
                    if not video_url.startswith("http"):
                        video_url = "https://www.tikwm.com" + video_url
                    return {
                        "title": title,
                        "thumbnail": thumbnail,
                        "video_url": video_url,
                        "platform": "TikTok",
                        "is_slideshow": False,
                        "duration": d.get("duration", 0),
                        "formats": [{
                            "format_id": "default",
                            "url": video_url,
                            "ext": "mp4",
                            "resolution": "Watermark-Free",
                            "filesize": d.get("size")
                        }]
                    }
    except Exception as e:
        print(f"[TikWM] parser failed: {e}")

    # Engine 2: MusicalDown scraper fallback
    try:
        resolved = resolve_url(url)
        session = requests.Session()
        session.verify = False
        res = session.get("https://musicaldown.com/en", headers=headers, timeout=15)
        if res.status_code == 200:
            soup = parse_html(res.text)
            form = soup.find("form", action=lambda x: x and "/download" in x)
            if form:
                inputs = form.find_all("input")
                post_data = {}
                for inp in inputs:
                    name = inp.get("name")
                    val = inp.get("value", "")
                    type_ = inp.get("type", "")
                    if type_ == "text":
                        post_data[name] = resolved
                    elif name:
                        post_data[name] = val
                action_url = form.get("action")
                if not action_url.startswith("http"):
                    action_url = "https://musicaldown.com" + action_url
                res_down = session.post(action_url, data=post_data, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://musicaldown.com/en'
                }, timeout=20)
                if res_down.status_code == 200:
                    soup_down = parse_html(res_down.text)
                    title = "TikTok Video"
                    title_el = soup_down.find("h2", class_="white-text") or soup_down.find("h2")
                    if title_el:
                        t_text = title_el.text.strip()
                        title = t_text.split(":", 1)[1].strip() if ":" in t_text else t_text
                    
                    video_url = None
                    links = soup_down.find_all("a", href=True)
                    for link in links:
                        href = link.get("href")
                        text = link.text.strip().lower()
                        if "muscdn.app" in href and "hd" in text:
                            video_url = href
                            break
                        elif "muscdn.app" in href and "download" in text and not video_url:
                            video_url = href
                    if video_url:
                        return {
                            "title": title,
                            "thumbnail": None,
                            "video_url": video_url,
                            "platform": "TikTok",
                            "is_slideshow": False,
                            "duration": 0,
                            "formats": [{
                                "format_id": "default",
                                "url": video_url,
                                "ext": "mp4",
                                "resolution": "Watermark-Free",
                                "filesize": None
                            }]
                        }
    except Exception as e:
        print(f"[MusicalDown] parser failed: {e}")

    raise Exception("Unable to extract TikTok video. The video may be private, age-restricted, or removed.")

def get_cookies_path():
    possible_paths = [
        '/app/data/cookies.txt',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cookies.txt')
    ]
    for p in possible_paths:
        if os.path.exists(p):
            return p
    return None

def extract_instagram(url):
    """Custom parser for Instagram Reels and Posts"""
    clean_url = url.strip()
    ydl_opts = {
        'no_warnings': True,
        'quiet': True,
        'socket_timeout': 20,
        'format': get_ytdl_format(),
        'format_sort': ['vcodec:h264', 'acodec:aac'],
    }
    if has_ffmpeg():
        ydl_opts['merge_output_format'] = 'mp4'
    cookies_path = get_cookies_path()
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path

    try:
        yt_dlp = _get_ytdlp()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            title = info.get('title') or info.get('description') or "Instagram Video"
            if len(title) > 80:
                title = title[:80] + "..."
            return {
                "title": title,
                "thumbnail": info.get('thumbnail'),
                "video_url": info.get('url'),
                "platform": "Instagram",
                "duration": info.get('duration', 0),
                "formats": [{
                    "format_id": "default",
                    "url": info.get('url'),
                    "ext": "mp4",
                    "resolution": "default",
                }]
            }
    except Exception as e:
        safe_msg = str(e)
        if "empty media response" in safe_msg or "login" in safe_msg.lower():
            raise Exception("មិនអាចទាញយកវីដេអូ Instagram នេះបានទេ។ វីដេអូនេះអាចជាឯកជន (Private) ឬ Instagram តម្រូវឱ្យ Login គណនី។")
        raise Exception(f"មិនអាចទាញយកវីដេអូ Instagram នេះបានទេ៖ {safe_msg[:80]}")

def get_video_info(url):
    """Main extraction entry point. Tries custom parsers then falls back to yt-dlp."""
    clean_url = url.strip()
    
    # Identify platform
    if "instagram.com" in clean_url or "instagr.am" in clean_url:
        return extract_instagram(clean_url)

    if "tiktok.com" in clean_url or "douyin.com" in clean_url:
        return extract_tiktok(clean_url)

    if "xiaohongshu.com" in clean_url or "xhslink.com" in clean_url:
        try:
            return extract_xiaohongshu(clean_url)
        except Exception as e:
            print(f"XHS custom parser failed: {e}. Trying yt-dlp...")
            
    if "kuaishou.com" in clean_url or "v.kuaishou.com" in clean_url:
        try:
            return extract_kuaishou(clean_url)
        except Exception as e:
            print(f"Kuaishou custom parser failed: {e}. Trying yt-dlp...")

    if "dramabox.com" in clean_url or "dramaboxdb.com" in clean_url:
        return extract_dramabox(clean_url)
        
    # Standard yt-dlp extraction
    ydl_opts = {
        'no_warnings': True,
        'quiet': True,
        'socket_timeout': 20,
        'format': get_ytdl_format(),
        'format_sort': ['vcodec:h264', 'acodec:aac'],
    }
    if has_ffmpeg():
        ydl_opts['merge_output_format'] = 'mp4'
    
    cookies_path = get_cookies_path()
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path
        
    try:
        yt_dlp = _get_ytdlp()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            
            # Format outputs
            formats = []
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                        formats.append({
                            "format_id": f.get('format_id'),
                            "url": f.get('url'),
                            "ext": f.get('ext', 'mp4'),
                            "resolution": f.get('resolution') or f.get('format_note') or f.get('height'),
                            "filesize": f.get('filesize') or f.get('filesize_approx')
                        })
            else:
                formats.append({
                    "format_id": "best",
                    "url": info.get('url'),
                    "ext": info.get('ext', 'mp4'),
                    "resolution": "best",
                    "filesize": info.get('filesize')
                })
                
            return {
                "title": info.get('title', 'Social Media Video'),
                "thumbnail": info.get('thumbnail'),
                "video_url": info.get('url'),
                "platform": info.get('extractor_key', 'Unknown'),
                "duration": info.get('duration', 0),
                "formats": formats,
                "filesize": info.get('filesize') or info.get('filesize_approx')
            }
    except Exception as e:
        print(f"yt-dlp extraction failed: {e}")
        try:
            res = requests.get(clean_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }, timeout=10)
            soup = parse_html(res.text)
            video = soup.find("video")
            if video and video.get("src"):
                return {
                    "title": "Extracted Video",
                    "thumbnail": None,
                    "video_url": video.get("src"),
                    "platform": "Web Direct",
                    "duration": 0,
                    "formats": [{
                        "format_id": "default",
                        "url": video.get("src"),
                        "ext": "mp4",
                        "resolution": "default",
                        "filesize": None
                    }]
                }
        except:
            pass
            
        raise Exception(f"Unable to extract video. Please double check the link or try another. Error: {str(e)[:100]}")

def download_video_to_temp(url, format_id=None, progress_callback=None, cancel_check=None, output_dir=None, extracted_info=None):
    """Stream a video to a temporary file on disk without loading it into memory. Returns local file path."""
    target_dir = output_dir or TEMP_DIR
    os.makedirs(target_dir, exist_ok=True)

    # Reuse already-extracted direct stream metadata when available (Kuaishou, TikTok, Xiaohongshu, Web Direct)
    direct_platforms = {"Kuaishou", "TikTok", "Xiaohongshu", "Web Direct"}
    if extracted_info and extracted_info.get("platform") in direct_platforms:
        direct_url = extracted_info.get("video_url")
        if direct_url and ".m3u8" not in direct_url.lower():
            prefix = extracted_info["platform"].lower().replace(" ", "_")
            return download_direct_file(
                direct_url, f"{prefix}_video.mp4", progress_callback, cancel_check, output_dir=target_dir
            )

    # Check Kuaishou on raw URL to avoid desktop redirect captcha issues
    ytdl_target = None
    if not extracted_info and ("kuaishou.com" in url or "v.kuaishou.com" in url):
        try:
            info = extract_kuaishou(url)
            if ".m3u8" not in info['video_url'].lower():
                return download_direct_file(
                    info['video_url'], "kuaishou_video.mp4", progress_callback, cancel_check, output_dir=target_dir
                )
            ytdl_target = info['video_url']
        except Exception as e:
            print(f"Kuaishou direct download failed: {e}. Trying yt-dlp...")

    resolved = resolve_url(url) if not ytdl_target else ytdl_target

    if not extracted_info and not ytdl_target:
        # Custom downloads for custom scrapers
        if "tiktok.com" in resolved or "douyin.com" in resolved:
            info = extract_tiktok(resolved)
            return download_direct_file(
                info['video_url'], "tiktok_video.mp4", progress_callback, cancel_check, output_dir=target_dir
            )

        if "xiaohongshu.com" in resolved or "xhslink.com" in resolved:
            info = extract_xiaohongshu(resolved)
            return download_direct_file(
                info['video_url'], "xhs_video.mp4", progress_callback, cancel_check, output_dir=target_dir
            )

    # If caller passed extracted_info containing an m3u8 URL (e.g. Kuaishou HLS fallback), pass it directly to yt-dlp
    if extracted_info and extracted_info.get("platform") in direct_platforms and extracted_info.get("video_url"):
        resolved = extracted_info["video_url"]

    # yt-dlp streaming download directly to temp disk directory
    outtmpl = os.path.join(target_dir, 'video_%(id).40s.%(ext)s')

    def ytdl_progress_hook(d):
        if cancel_check and cancel_check():
            raise DownloadCancelledException("Cancelled by user")
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if downloaded > MAX_FILE_SIZE_BYTES:
                raise Exception("Video size exceeds 1GB limit.")
            if progress_callback and total > 0:
                progress_callback(downloaded, total)

    ydl_opts = {
        'outtmpl': outtmpl,
        'paths': {'home': target_dir, 'temp': target_dir},
        'format': get_ytdl_format(format_id),
        'no_warnings': True,
        'quiet': True,
        'socket_timeout': 20,
        'progress_hooks': [ytdl_progress_hook],
        'format_sort': ['vcodec:h264', 'acodec:aac'],
    }
    if has_ffmpeg():
        ydl_opts['merge_output_format'] = 'mp4'

    cookies_path = get_cookies_path()
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path

    yt_dlp = _get_ytdlp()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(resolved, download=True)
        filename = ydl.prepare_filename(info)
        if os.path.exists(filename):
            return filename

        base, _ = os.path.splitext(filename)
        for ext in ['.mp4', '.mkv', '.webm', '.3gp', '.mov']:
            if os.path.exists(base + ext):
                return base + ext

        files = [
            os.path.join(target_dir, f)
            for f in os.listdir(target_dir)
            if os.path.isfile(os.path.join(target_dir, f)) and not f.endswith(('.part', '.ytdl'))
        ]
        if files:
            return max(files, key=os.path.getmtime)

        raise Exception("Failed to locate downloaded file")

def download_direct_file(url, default_name, progress_callback=None, cancel_check=None, output_dir=None):
    """Stream a file in small chunks directly from a URL to a temp file without loading into memory."""
    target_dir = output_dir or TEMP_DIR
    os.makedirs(target_dir, exist_ok=True)

    # Sanitize default name and ensure unique filename when sharing TEMP_DIR
    clean_name = re.sub(r'[\\/*?:"<>|]', "", default_name) or "video.mp4"
    if output_dir:
        filepath = os.path.join(target_dir, clean_name)
    else:
        base, ext = os.path.splitext(clean_name)
        filepath = os.path.join(target_dir, f"{base}_{uuid.uuid4().hex[:8]}{ext or '.mp4'}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        with requests.get(url, stream=True, headers=headers, timeout=(15, 60)) as res:
            res.raise_for_status()

            total_size = int(res.headers.get('content-length') or 0)
            if total_size > MAX_FILE_SIZE_BYTES:
                raise Exception("Video size exceeds 1GB limit.")
            check_free_disk(total_size, target_dir)
            downloaded = 0

            with open(filepath, 'wb') as f:
                for chunk in res.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if cancel_check and cancel_check():
                        raise DownloadCancelledException("Cancelled by user")
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE_BYTES:
                        raise Exception("Video size exceeds 1GB limit.")
                    if progress_callback and total_size > 0:
                        progress_callback(downloaded, total_size)

        return filepath
    except Exception:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass
        raise
