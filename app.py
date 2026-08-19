import os
import re
import json
import uuid
import time
import shutil
import sqlite3
import base64
import ctypes
import ctypes.wintypes
import requests
import subprocess
import threading
import urllib3
import zipfile
import io
from urllib.parse import urljoin, urlparse
from flask import Flask, render_template, request, jsonify, send_file

# 禁用不安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
tasks = {}
batches = {}

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "cookie": "", 
        "ffmpeg_path": "", 
        "strategy": "traffic", 
        "time_max_size_mb": 1.0, 
        "is_unlimited": False,
        "use_proxy": False,
        "proxy_address": "127.0.0.1:1080"
    }

def save_config(cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited, use_proxy, proxy_address):
    try:
        config = load_config()
        config.update({
            'cookie': cookie,
            'ffmpeg_path': ffmpeg_path,
            'strategy': strategy,
            'time_max_size_mb': time_max_size_mb,
            'is_unlimited': is_unlimited,
            'use_proxy': use_proxy,
            'proxy_address': proxy_address
        })
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存配置失败: {e}")

# ================= 智能番号格式化引擎 =================
def format_video_name(title):
    if not title:
        return title
        
    clean_title = title
    # 去除常见的无用视频后缀
    if clean_title.lower().endswith(('.mp4', '.avi', '.mkv', '.rmvb', '.flv', '.wmv', '.mov', '.ts', '.m2ts', '.webm')):
        clean_title = clean_title.rsplit('.', 1)[0]
        
    # 清除中括号、方括号内的无用修饰符
    clean_title = re.sub(r'\[.*?\]|【.*?】', ' ', clean_title)
    
    # 规则 1: FC2-PPV 系列 (提取出纯粹的 FC2-PPV-12345)
    m = re.search(r'fc2[-_ ]*ppv[-_ ]*(\d{5,7})', clean_title, re.IGNORECASE)
    if m: return f"FC2-PPV-{m.group(1)}"
    
    # 规则 2: 日期编号型 (如 Caribbean/1Pondo: 081926-123)
    m = re.search(r'\b(\d{6})[-_ ]+(\d{3})\b', clean_title)
    if m: return f"{m.group(1)}-{m.group(2)}"
    
    # 规则 3: HEYZO 系列
    m = re.search(r'heyzo[-_ ]*(\d{4})', clean_title, re.IGNORECASE)
    if m: return f"HEYZO-{m.group(1)}"
    
    # 规则 4: Tokyo Hot 及单字母厂牌 (k1234, n1234 等)
    m = re.search(r'\b([a-zA-Z])[-_ ]*(\d{3,5})\b', clean_title, re.IGNORECASE)
    if m and m.group(1).lower() in ['k', 'n', 'd', 'cz']:
        return f"{m.group(1).lower()}{m.group(2)}"
        
    # 规则 5: 标准字母+数字的番号 (ABC-123, ABCD-123)
    m = re.search(r'\b([a-zA-Z]{2,5})[-_ ]*(\d{2,5})\b', clean_title)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
        
    # 兜底方案：仅仅去除非法文件路径字符
    clean_title = re.sub(r'[\\/*?:"<>|]', '_', title)
    if clean_title.lower().endswith(('.mp4', '.avi', '.mkv', '.rmvb', '.flv', '.wmv', '.mov', '.ts', '.m2ts', '.webm')):
        clean_title = clean_title.rsplit('.', 1)[0]
    return clean_title.strip()


# ================= Windows DPAPI & Chromium 解密 115 Cookie =================
class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_char))]

def dpapi_decrypt(encrypted_bytes):
    blob_in = DATA_BLOB(len(encrypted_bytes), ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return decrypted
    return None

def get_115_master_key(local_state_path):
    if not os.path.exists(local_state_path):
        return None
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state['os_crypt']['encrypted_key'])
        encrypted_key = encrypted_key[5:]  
        return dpapi_decrypt(encrypted_key)
    except Exception as e:
        return None

def decrypt_chromium_cookie_value(encrypted_val, master_key):
    if not encrypted_val:
        return ""
    try:
        if encrypted_val[:3] in (b'v10', b'v11'):
            if not master_key:
                return ""
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = encrypted_val[3:15]
            ciphertext = encrypted_val[15:]
            aesgcm = AESGCM(master_key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted.decode('utf-8', errors='ignore')
        else:
            decrypted = dpapi_decrypt(encrypted_val)
            return decrypted.decode('utf-8', errors='ignore') if decrypted else ""
    except Exception:
        return ""

def extract_115_cookies_from_browser():
    possible_user_data = [
        os.path.expandvars(r"%LOCALAPPDATA%\115Chrome\User Data"),
        os.path.expandvars(r"%APPDATA%\115Chrome\User Data"),
        os.path.expandvars(r"%LOCALAPPDATA%\115\User Data"),
        os.path.expandvars(r"%APPDATA%\115\User Data")
    ]
    for udata in possible_user_data:
        if not os.path.exists(udata):
            continue
        local_state_file = os.path.join(udata, "Local State")
        master_key = get_115_master_key(local_state_file)
        possible_cookie_dbs = [
            os.path.join(udata, "Default", "Network", "Cookies"),
            os.path.join(udata, "Default", "Cookies"),
        ]
        for db_path in possible_cookie_dbs:
            if not os.path.exists(db_path):
                continue
            temp_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_115_cookie.db")
            try:
                with open(db_path, 'rb') as f_in:
                    db_data = f_in.read()
                with open(temp_db, 'wb') as f_out:
                    f_out.write(db_data)
                conn = sqlite3.connect(temp_db)
                cursor = conn.cursor()
                cursor.execute("SELECT name, encrypted_value, value, host_key FROM cookies WHERE host_key LIKE '%115.com%'")
                rows = cursor.fetchall()
                conn.close()
                if os.path.exists(temp_db):
                    os.remove(temp_db)
                cookies_dict = {}
                for name, encrypted_value, value, host_key in rows:
                    if value:
                        cookies_dict[name] = value
                    elif encrypted_value:
                        dec_val = decrypt_chromium_cookie_value(encrypted_value, master_key)
                        if dec_val:
                            cookies_dict[name] = dec_val
                if cookies_dict:
                    return "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            except Exception as e:
                err_msg = str(e)
                if isinstance(e, PermissionError) or getattr(e, 'winerror', 0) == 32 or "32" in err_msg or "正在使用" in err_msg or "Permission denied" in err_msg:
                    return "ERROR_LOCKED"
            finally:
                if os.path.exists(temp_db):
                    try:
                        os.remove(temp_db)
                    except:
                        pass
    return ""

# ================= 核心网络下载与抽帧逻辑 =================

def build_headers(m3u8_url, cookie=''):
    parsed = urlparse(m3u8_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 115Browser/36.0.0 Chromium/125.0",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": referer,
        "Origin": origin,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers

def build_request_kwargs(m3u8_url, cookie, use_proxy, proxy_address):
    headers = build_headers(m3u8_url, cookie)
    kwargs = {
        "headers": headers,
        "verify": False
    }
    if use_proxy and proxy_address:
        proxy_url = proxy_address if "://" in proxy_address else f"http://{proxy_address}"
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    else:
        kwargs["proxies"] = {"http": None, "https": None}
    return kwargs

def download_m3u8_text(m3u8_url, cookie='', use_proxy=False, proxy_address=''):
    kwargs = build_request_kwargs(m3u8_url, cookie, use_proxy, proxy_address)
    kwargs['timeout'] = 20
    try:
        resp = requests.get(m3u8_url, **kwargs)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise Exception(f"下载m3u8失败: {str(e)}")

def parse_master_m3u8(m3u8_content, base_url):
    media_urls = []
    lines = [line.strip() for line in m3u8_content.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF:"):
            i += 1
            if i < len(lines):
                media_url = urljoin(base_url, lines[i])
                media_urls.append(media_url)
        i += 1
    return media_urls

def parse_media_m3u8(m3u8_content, base_url):
    segments = []
    lines = [line.strip() for line in m3u8_content.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-KEY:"):
            pass  
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":")[1].split(",")[0])
            i += 1
            if i < len(lines):
                seg_url = urljoin(base_url, lines[i])
                segments.append({"duration": duration, "url": seg_url})
        i += 1
    return segments

def resolve_m3u8(m3u8_url, cookie='', use_proxy=False, proxy_address=''):
    base_url = m3u8_url.rsplit('/', 1)[0] + '/'
    content = download_m3u8_text(m3u8_url, cookie, use_proxy, proxy_address)
    media_urls = parse_master_m3u8(content, base_url)

    if media_urls:
        media_url = media_urls[0]
        media_base = media_url.rsplit('/', 1)[0] + '/'
        media_content = download_m3u8_text(media_url, cookie, use_proxy, proxy_address)
        segments = parse_media_m3u8(media_content, media_base)
    else:
        segments = parse_media_m3u8(content, base_url)

    total_duration = sum(s["duration"] for s in segments)
    return segments, total_duration

def extract_first_frame(ts_path, output_image_path, ffmpeg_path=''):
    ffmpeg_cmd = 'ffmpeg'
    if ffmpeg_path:
        if os.path.isdir(ffmpeg_path):
            ffmpeg_cmd = os.path.join(ffmpeg_path, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        else:
            ffmpeg_cmd = ffmpeg_path

    try:
        cmd = [
            ffmpeg_cmd, '-y',
            '-i', ts_path,
            '-ss', '0',
            '-vframes', '1',
            '-q:v', '2',
            '-f', 'image2',
            output_image_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 0:
            stderr = result.stderr.lower()
            if "concealing" in stderr or "error while decoding mb" in stderr:
                os.remove(output_image_path)
                return False  
            return True 
        return False
    except Exception:
        if os.path.exists(output_image_path):
            os.remove(output_image_path)
        return False

def smart_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1536*1024, ffmpeg_path='', use_proxy=False, proxy_address=''):
    kwargs_base = build_request_kwargs(m3u8_url, cookie, use_proxy, proxy_address)

    for retry in range(3):
        try:
            kwargs = kwargs_base.copy()
            kwargs["headers"] = kwargs_base["headers"].copy()
            kwargs["headers"]['Range'] = f'bytes=0-{max_size-1}'
            kwargs["timeout"] = 30
            kwargs["stream"] = True
            
            resp = requests.get(ts_url, **kwargs)
            resp.raise_for_status()

            original_size = 0
            cr = resp.headers.get('Content-Range')
            if cr and '/' in cr:
                try:
                    original_size = int(cr.split('/')[1])
                except:
                    pass
            if not original_size:
                original_size = int(resp.headers.get('Content-Length', 0))

            downloaded = 0
            buffer_size = 0
            check_interval = 128 * 1024  
            success = False

            with open(ts_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        buffer_size += len(chunk)

                        if buffer_size >= check_interval:
                            f.flush()  
                            if extract_first_frame(ts_path, img_path, ffmpeg_path):
                                resp.close()  
                                success = True
                                break
                            buffer_size = 0  

                        if downloaded >= max_size:
                            break
            
            if not success:
                if extract_first_frame(ts_path, img_path, ffmpeg_path):
                    success = True

            original_size = max(original_size, downloaded)
            if success:
                return True, original_size, downloaded

        except Exception:
            if retry == 2:
                return False, 0, 0
                
    return False, 0, 0

def fast_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1024*1024, ffmpeg_path='', use_proxy=False, proxy_address=''):
    kwargs_base = build_request_kwargs(m3u8_url, cookie, use_proxy, proxy_address)

    for retry in range(3):
        try:
            kwargs = kwargs_base.copy()
            kwargs["headers"] = kwargs_base["headers"].copy()
            if max_size > 0:
                kwargs["headers"]['Range'] = f'bytes=0-{max_size-1}'
            kwargs["timeout"] = 30
            kwargs["stream"] = True

            resp = requests.get(ts_url, **kwargs)
            resp.raise_for_status()

            original_size = 0
            cr = resp.headers.get('Content-Range')
            if cr and '/' in cr:
                try:
                    original_size = int(cr.split('/')[1])
                except:
                    pass
            if not original_size:
                original_size = int(resp.headers.get('Content-Length', 0))

            downloaded = 0
            with open(ts_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if max_size > 0 and downloaded >= max_size:
                            break
            
            original_size = max(original_size, downloaded)

            if extract_first_frame(ts_path, img_path, ffmpeg_path):
                return True, original_size, downloaded
            return False, original_size, downloaded

        except Exception:
            if retry == 2:
                return False, 0, 0
                
    return False, 0, 0

def process_task(task_id, m3u8_url, video_name, cookie=''):
    task = tasks[task_id]
    work_dir = task['work_dir']
    ffmpeg_path = task.get('ffmpeg_path', '')
    strategy = task.get('strategy', 'traffic')
    time_max_size = task.get('time_max_size', 1048576)
    use_proxy = task.get('use_proxy', False)
    proxy_address = task.get('proxy_address', '')

    # ================= 强力续传支持 =================
    # 如果指定了 archive_path 且 temp 文件夹还没建立，将其预拉取作为临时工作区
    archive_path = task.get('archive_path')
    if archive_path and os.path.exists(archive_path) and not os.path.exists(os.path.join(work_dir, 'frames.zip')):
        os.makedirs(work_dir, exist_ok=True)
        temp_zip = os.path.join(work_dir, 'frames.zip')
        shutil.copy2(archive_path, temp_zip)
        try:
            with zipfile.ZipFile(temp_zip, 'r') as zf:
                if 'metadata.json' in zf.namelist():
                    zf.extract('metadata.json', work_dir)
        except Exception:
            pass

    if not os.path.exists(work_dir):
        os.makedirs(work_dir, exist_ok=True)

    def save_metadata():
        if task.get('frames'):
            meta_path = os.path.join(work_dir, 'metadata.json')
            try:
                elapsed = task.get('elapsed_time', 0)
                if task.get('start_time'):
                    elapsed += time.time() - task['start_time']

                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'video_name': task.get('video_name', '未命名视频'),
                        'm3u8_url': task.get('m3u8_url', ''),
                        'cookie': task.get('cookie', ''),
                        'ffmpeg_path': task.get('ffmpeg_path', ''),
                        'strategy': task.get('strategy', 'traffic'),
                        'time_max_size_mb': task.get('time_max_size_mb', 1.0),
                        'is_unlimited': task.get('is_unlimited', False),
                        'use_proxy': task.get('use_proxy', False),
                        'proxy_address': task.get('proxy_address', ''),
                        'total_duration': task.get('total_duration', 0),
                        'total_segments': task.get('total_segments', 0),
                        'current': task.get('current', 0),                    
                        'failed_segments': task.get('failed_segments', []),   
                        'total_original_size': task.get('total_original_size', 0),
                        'total_traffic': task.get('total_traffic', 0),
                        'total_image_size': task.get('total_image_size', 0),
                        'elapsed_time': elapsed,
                        'acquisition_time': task.get('acquisition_time', ''),
                        'frames': task.get('frames', [])
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                pass

    task['start_time'] = time.time()
    task['cancelled'] = False
    task['session_start_current'] = task.get('current', 0)

    try:
        task['status'] = 'parsing'
        segments, total_duration = resolve_m3u8(m3u8_url, cookie, use_proxy, proxy_address)

        if not segments:
            task['status'] = 'error'
            task['error'] = '未找到ts切片'
            return

        task['total_segments'] = len(segments)
        task['total_duration'] = total_duration
        task['status'] = 'downloading'

        existing_frames = {f['index'] for f in task.get('frames', [])}

        for idx, seg in enumerate(segments):
            if task.get('cancelled'):
                task['status'] = 'stopped'
                break

            task['current'] = idx + 1

            if idx in existing_frames:
                continue

            ts_path = os.path.join(work_dir, f'seg_{idx:04d}.ts')
            img_path = os.path.join(work_dir, f'frame_{idx:04d}.jpg')

            if strategy == 'traffic':
                success, orig_size, traffic = smart_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, max_size=1536*1024, ffmpeg_path=ffmpeg_path, use_proxy=use_proxy, proxy_address=proxy_address)
            else:
                success, orig_size, traffic = fast_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, max_size=time_max_size, ffmpeg_path=ffmpeg_path, use_proxy=use_proxy, proxy_address=proxy_address)
            
            if not success:
                if idx not in task['failed_segments']:
                    task['failed_segments'].append(idx)
                if os.path.exists(ts_path):
                    os.remove(ts_path)
                if os.path.exists(img_path):
                    os.remove(img_path)
                continue

            if idx in task['failed_segments']:
                task['failed_segments'].remove(idx)

            task['total_original_size'] += orig_size
            task['total_traffic'] += traffic
            
            # 【完美解决 NTFS 簇臃肿】：图片一出，立刻装入临时的无压缩 ZIP，然后销毁原图！
            if os.path.exists(img_path):
                img_size = os.path.getsize(img_path)
                task['total_image_size'] += img_size
                
                zip_path = os.path.join(work_dir, 'frames.zip')
                img_filename = f'frame_{idx:04d}.jpg'
                try:
                    mode = 'a' if os.path.exists(zip_path) else 'w'
                    with zipfile.ZipFile(zip_path, mode, zipfile.ZIP_STORED) as zf:
                        if img_filename not in zf.namelist():
                            zf.write(img_path, arcname=img_filename)
                except Exception as e:
                    pass
                
                # 销毁散乱游离文件，节省成百上千倍的空间
                os.remove(img_path)

            task['frames'].append({
                'index': idx,
                'time': sum(s['duration'] for s in segments[:idx]),
                'duration': seg['duration'],
                'image': f'frame_{idx:04d}.jpg'
            })
            
            task['frames'] = sorted(task['frames'], key=lambda k: k['index'])
            existing_frames.add(idx)

            if os.path.exists(ts_path):
                os.remove(ts_path)
            
            if (idx + 1) % 10 == 0:
                save_metadata()

        else:
            if not task.get('cancelled'):
                task['status'] = 'completed'

    except Exception as e:
        task['status'] = 'error'
        task['error'] = str(e)
    finally:
        if task.get('start_time'):
            task['elapsed_time'] = task.get('elapsed_time', 0) + (time.time() - task['start_time'])
            task['start_time'] = None
        save_metadata()

        # 【任务完结打包】：将带有 metadata 的完整结构包装成最终的【番号.zip】
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            folder_name = re.sub(r'[\\/*?:"<>|]', '_', task['video_name'])
            final_zip_name = f"{folder_name}.zip"
            final_zip_path = os.path.join(base_dir, 'static', final_zip_name)
            
            frames_zip_path = os.path.join(work_dir, 'frames.zip')
            meta_path = os.path.join(work_dir, 'metadata.json')
            
            if os.path.exists(frames_zip_path):
                # 将元数据追加入包
                if os.path.exists(meta_path):
                    with zipfile.ZipFile(frames_zip_path, 'a', zipfile.ZIP_STORED) as zf:
                        zf.write(meta_path, arcname='metadata.json')
                
                if os.path.exists(final_zip_path):
                    os.remove(final_zip_path)
                shutil.move(frames_zip_path, final_zip_path)
                
                # 更新任务指针到最终的纯净版 ZIP
                task['archive_path'] = final_zip_path
                
                # 销毁临时沙盒文件夹
                if "_temp_" in work_dir:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    
        except Exception as e:
            print(f"最终封卷压缩失败: {e}")

# ================= 状态提取通用核心 =================

def get_task_status_dict(task_id):
    task = tasks.get(task_id)
    if not task:
        return None

    elapsed_time = task.get('elapsed_time', 0)
    remaining_time = 0
    completion_time = ""
    
    if task.get('start_time'):
        session_elapsed = time.time() - task['start_time']
        elapsed_time += session_elapsed
        
        if task['status'] == 'downloading' and task['total_segments'] > 0:
            session_processed = task['current'] - task.get('session_start_current', 0)
            if session_processed > 0:
                avg_time_per_seg = session_elapsed / session_processed
                remaining_segments = task['total_segments'] - task['current']
                remaining_time = avg_time_per_seg * remaining_segments
                completion_timestamp = time.time() + remaining_time
                completion_time = time.strftime("%H:%M:%S", time.localtime(completion_timestamp))

    return {
        'status': task['status'],
        'total_segments': task['total_segments'],
        'current': task['current'],
        'total_duration': task['total_duration'],
        'total_original_size': task.get('total_original_size', 0),
        'total_traffic': task.get('total_traffic', 0),
        'total_image_size': task.get('total_image_size', 0),
        'acquisition_time': task.get('acquisition_time', ''),
        'elapsed_time': elapsed_time,
        'remaining_time': remaining_time,
        'completion_time': completion_time,
        'frames': task['frames'],
        'failed_segments': task['failed_segments'],
        'error': task.get('error', ''),
        'work_dir': task['work_dir'],
        'video_name': task.get('video_name', '')
    }

# ================= 115 云盘 API 批量处理核心 =================

def batch_115_process(mode, filenames, cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited, use_proxy, proxy_address):
    headers = build_headers("https://webapi.115.com/", cookie)
    time_max_size = -1 if is_unlimited else int(time_max_size_mb * 1024 * 1024)

    try:
        root_url = "https://webapi.115.com/files?aid=1&cid=0&limit=1000&format=json"
        resp = requests.get(root_url, headers=headers, verify=False, timeout=15).json()
        
        m3u8_cid = None
        for item in resp.get('data', []):
            if item.get('n') == 'm3u8' and ('fid' not in item or not item.get('fid')):
                m3u8_cid = item.get('cid')
                break
        
        if not m3u8_cid:
            print("[115 API Error] 未在网盘根目录找到名为 'm3u8' 的文件夹！")
            return

        files_url = f"https://webapi.115.com/files?aid=1&cid={m3u8_cid}&limit=1000&format=json"
        files_resp = requests.get(files_url, headers=headers, verify=False, timeout=15).json()
        
        target_files = []
        for item in files_resp.get('data', []):
            if item.get('pc'):
                if mode == 'all':
                    target_files.append(item)
                else:
                    for name in filenames:
                        if name in item.get('n', ''):
                            target_files.append(item)
                            break
        
        if not target_files:
            return

        for tf in target_files:
            pc = tf['pc']
            raw_title = tf['n']
            
            # 使用新升级的番号自动清理及提炼引擎
            video_name = format_video_name(raw_title)
            
            m3u8_url = f"https://115.com/api/video/m3u8/{pc}.m3u8"
            
            task_id = str(uuid.uuid4())
            folder_name = re.sub(r'[\\/*?:"<>|]', '_', video_name)
            
            base_dir = os.path.dirname(os.path.abspath(__file__))
            # 采用带有 _temp_ 的工作临时目录
            work_dir = os.path.join(base_dir, 'static', f"_{folder_name}_temp_{task_id[:8]}")
            os.makedirs(work_dir, exist_ok=True)
            acquisition_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            
            tasks[task_id] = {
                'id': task_id,
                'video_name': video_name,
                'm3u8_url': m3u8_url,
                'cookie': cookie,
                'ffmpeg_path': ffmpeg_path,
                'strategy': strategy,
                'time_max_size': time_max_size,
                'time_max_size_mb': time_max_size_mb,
                'is_unlimited': is_unlimited,
                'use_proxy': use_proxy,
                'proxy_address': proxy_address,
                'work_dir': work_dir,
                'archive_path': None,
                'status': 'pending', 
                'start_time': None,
                'elapsed_time': 0,
                'total_segments': 0,
                'current': 0,
                'total_duration': 0,
                'total_original_size': 0,  
                'total_traffic': 0,
                'total_image_size': 0,
                'acquisition_time': acquisition_time,
                'frames': [],
                'failed_segments': [],
                'cancelled': False
            }
            
            process_task(task_id, m3u8_url, video_name, cookie)
            
    except Exception as e:
        print(f"[115 API Exception] {e}")

# ================= 路由与接口 =================

@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', 
                           default_cookie=config.get('cookie', ''), 
                           default_ffmpeg=config.get('ffmpeg_path', ''),
                           default_strategy=config.get('strategy', 'traffic'),
                           default_time_max_size_mb=config.get('time_max_size_mb', 1.0),
                           default_is_unlimited=config.get('is_unlimited', False),
                           default_use_proxy=config.get('use_proxy', False),
                           default_proxy_address=config.get('proxy_address', '127.0.0.1:1080'))

@app.route('/api/get_115_cookie', methods=['GET'])
def get_115_cookie():
    try:
        cookie_str = extract_115_cookies_from_browser()
        if cookie_str == "ERROR_LOCKED":
            err_msg = "检测到 115 浏览器正在运行，Cookie 文件被彻底锁定！\n请【关闭 115 浏览器】，换用系统浏览器（如 Chrome/Edge）打开本页面（127.0.0.1:5000），再次点击即可提取。"
            return jsonify({'success': False, 'error': err_msg})
        elif cookie_str:
            return jsonify({'success': True, 'cookie': cookie_str})
        else:
            return jsonify({'success': False, 'error': '未在本地 115 浏览器中找到登录记录，请先在 115 浏览器中登录账号！'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'提取失败: {str(e)}'})

@app.route('/api/batch_115_start', methods=['POST'])
def batch_115_start():
    data = request.json
    mode = data.get('mode', 'all')
    filenames = data.get('filenames', [])
    cookie = data.get('cookie', '').strip()
    ffmpeg_path = data.get('ffmpeg_path', '').strip()
    strategy = data.get('strategy', 'traffic')
    time_max_size_mb = data.get('time_max_size_mb', 1.0)
    is_unlimited = data.get('is_unlimited', False)
    use_proxy = data.get('use_proxy', False)
    proxy_address = data.get('proxy_address', '').strip()

    save_config(cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited, use_proxy, proxy_address)

    if not cookie:
        return jsonify({'success': False, 'error': '115网盘直下必须先填写或提取 Cookie'})

    headers = build_headers("https://webapi.115.com/", cookie)
    try:
        root_url = "https://webapi.115.com/files?aid=1&cid=0&limit=1000&format=json"
        resp = requests.get(root_url, headers=headers, verify=False, timeout=15).json()
        
        m3u8_cid = None
        for item in resp.get('data', []):
            if item.get('n') == 'm3u8' and ('fid' not in item or not item.get('fid')):
                m3u8_cid = item.get('cid')
                break
        
        if not m3u8_cid:
            return jsonify({'success': False, 'error': "未在网盘根目录找到名为 'm3u8' 的文件夹！"})

        files_url = f"https://webapi.115.com/files?aid=1&cid={m3u8_cid}&limit=1000&format=json"
        files_resp = requests.get(files_url, headers=headers, verify=False, timeout=15).json()
        
        target_files = []
        for item in files_resp.get('data', []):
            if item.get('pc'):
                if mode == 'all':
                    target_files.append(item)
                else:
                    for name in filenames:
                        if name in item.get('n', ''):
                            target_files.append(item)
                            break
        
        if not target_files:
            return jsonify({'success': False, 'error': "m3u8 文件夹内没有找到符合条件的视频文件。"})
            
    except Exception as e:
        return jsonify({'success': False, 'error': f"115 API 请求失败: {str(e)}"})

    time_max_size = -1 if is_unlimited else int(time_max_size_mb * 1024 * 1024)
    batch_id = str(uuid.uuid4())
    batch_tasks = []

    for tf in target_files:
        pc = tf['pc']
        video_name = format_video_name(tf['n'])
        m3u8_url = f"https://115.com/api/video/m3u8/{pc}.m3u8"
        task_id = str(uuid.uuid4())
        folder_name = re.sub(r'[\\/*?:"<>|]', '_', video_name)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
        work_dir = os.path.join(base_dir, 'static', f"_{folder_name}_temp_{task_id[:8]}")
        os.makedirs(work_dir, exist_ok=True)
        acquisition_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        
        tasks[task_id] = {
            'id': task_id,
            'video_name': video_name,
            'm3u8_url': m3u8_url,
            'cookie': cookie,
            'ffmpeg_path': ffmpeg_path,
            'strategy': strategy,
            'time_max_size': time_max_size,
            'time_max_size_mb': time_max_size_mb,
            'is_unlimited': is_unlimited,
            'use_proxy': use_proxy,
            'proxy_address': proxy_address,
            'work_dir': work_dir,
            'archive_path': None,
            'status': 'pending', 
            'start_time': None,
            'elapsed_time': 0,
            'total_segments': 0,
            'current': 0,
            'total_duration': 0,
            'total_original_size': 0,  
            'total_traffic': 0,
            'total_image_size': 0,
            'acquisition_time': acquisition_time,
            'frames': [],
            'failed_segments': [],
            'cancelled': False
        }
        batch_tasks.append({'task_id': task_id, 'video_name': video_name})

    batches[batch_id] = {
        'id': batch_id,
        'tasks': batch_tasks,
        'current_idx': 0,
        'cancelled': False
    }

    def batch_worker(b_id):
        b = batches[b_id]
        for idx, b_task in enumerate(b['tasks']):
            if b['cancelled']:
                for remaining in b['tasks'][idx:]:
                    tasks[remaining['task_id']]['status'] = 'cancelled'
                break
            b['current_idx'] = idx
            t_id = b_task['task_id']
            t = tasks[t_id]
            t['status'] = 'starting'
            process_task(t_id, t['m3u8_url'], t['video_name'], t['cookie'])

    threading.Thread(target=batch_worker, args=(batch_id,)).start()

    return jsonify({'success': True, 'batch_id': batch_id, 'tasks': batch_tasks})


@app.route('/api/start', methods=['POST'])
def start_download():
    data = request.json
    m3u8_url = data.get('m3u8_url', '').strip()
    video_name = data.get('video_name', '').strip()
    cookie = data.get('cookie', '').strip()
    ffmpeg_path = data.get('ffmpeg_path', '').strip()
    strategy = data.get('strategy', 'traffic')
    time_max_size = data.get('time_max_size', 1048576)
    time_max_size_mb = data.get('time_max_size_mb', 1.0)
    is_unlimited = data.get('is_unlimited', False)
    use_proxy = data.get('use_proxy', False)
    proxy_address = data.get('proxy_address', '').strip()

    save_config(cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited, use_proxy, proxy_address)

    if not m3u8_url:
        return jsonify({'success': False, 'error': '请输入m3u8网址'})

    task_id = str(uuid.uuid4())
    
    video_name = format_video_name(video_name) if video_name else f"未命名视频_{task_id[:8]}"
    folder_name = re.sub(r'[\\/*?:"<>|]', '_', video_name)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    work_dir = os.path.join(base_dir, 'static', f"_{folder_name}_temp_{task_id[:8]}")
    os.makedirs(work_dir, exist_ok=True)
    acquisition_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    tasks[task_id] = {
        'id': task_id,
        'video_name': video_name,
        'm3u8_url': m3u8_url,
        'cookie': cookie,
        'ffmpeg_path': ffmpeg_path,
        'strategy': strategy,
        'time_max_size': time_max_size,
        'time_max_size_mb': time_max_size_mb,
        'is_unlimited': is_unlimited,
        'use_proxy': use_proxy,
        'proxy_address': proxy_address,
        'work_dir': work_dir,
        'archive_path': None,
        'status': 'starting',
        'start_time': None,
        'elapsed_time': 0,
        'total_segments': 0,
        'current': 0,
        'total_duration': 0,
        'total_original_size': 0,  
        'total_traffic': 0,
        'total_image_size': 0,
        'acquisition_time': acquisition_time,
        'frames': [],
        'failed_segments': [],
        'cancelled': False
    }

    thread = threading.Thread(target=process_task, args=(task_id, m3u8_url, video_name, cookie))
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/resume/<task_id>', methods=['POST'])
def resume_task(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})
    
    if task['status'] in ['starting', 'parsing', 'downloading']:
        return jsonify({'success': False, 'error': '任务正在运行中，无需恢复'})

    data = request.json or {}
    if data.get('m3u8_url'):
        task['m3u8_url'] = data['m3u8_url'].strip()
    if data.get('cookie') is not None:
        task['cookie'] = data['cookie'].strip()
    if data.get('ffmpeg_path') is not None:
        task['ffmpeg_path'] = data['ffmpeg_path'].strip()
    if data.get('strategy'):
        task['strategy'] = data['strategy']
    if data.get('time_max_size') is not None:
        task['time_max_size'] = data['time_max_size']
    if data.get('time_max_size_mb') is not None:
        task['time_max_size_mb'] = data['time_max_size_mb']
    if data.get('is_unlimited') is not None:
        task['is_unlimited'] = data['is_unlimited']
    if data.get('use_proxy') is not None:
        task['use_proxy'] = data['use_proxy']
    if data.get('proxy_address') is not None:
        task['proxy_address'] = data['proxy_address'].strip()

    if not task['m3u8_url']:
         return jsonify({'success': False, 'error': '未找到M3U8链接，请在输入框补全'})

    task['status'] = 'starting'
    thread = threading.Thread(target=process_task, args=(task_id, task['m3u8_url'], task['video_name'], task['cookie']))
    thread.daemon = True
    thread.start()

    return jsonify({'success': True})

@app.route('/api/load_local', methods=['POST'])
def load_local():
    """彻底优化：支持从纯净版 ZIP 直取 metadata，完美恢复历史会话"""
    data = request.json
    input_path = data.get('folder_path', '').strip()
    
    if not os.path.exists(input_path):
        return jsonify({'success': False, 'error': '无效的本地路径或文件不存在'})
        
    metadata = None
    is_zip_mode = False

    # 若输入的是打好的 ZIP 包 (建议的新格式)
    if os.path.isfile(input_path) and input_path.lower().endswith('.zip'):
        is_zip_mode = True
        try:
            with zipfile.ZipFile(input_path, 'r') as zf:
                # 获取最后一次写入的 metadata.json (完美解决多次封卷的覆盖问题)
                meta_infos = [info for info in zf.infolist() if info.filename == 'metadata.json']
                if not meta_infos:
                    return jsonify({'success': False, 'error': '此 ZIP 文件中未找到 metadata.json 数据！'})
                meta_data = zf.read(meta_infos[-1])
                metadata = json.loads(meta_data.decode('utf-8'))
        except Exception as e:
            return jsonify({'success': False, 'error': f'提取 ZIP 压缩包失败: {str(e)}'})
            
    # 向后兼容：若是老版本遗留的离散文件夹
    elif os.path.isdir(input_path):
        metadata_path = os.path.join(input_path, 'metadata.json')
        if not os.path.exists(metadata_path):
            return jsonify({'success': False, 'error': '该文件夹下未找到 metadata.json 文件，请确认是有效的下载目录'})
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        except Exception as e:
            return jsonify({'success': False, 'error': f'解析 metadata.json 失败: {str(e)}'})

    task_id = str(uuid.uuid4())
    total_segments = metadata.get('total_segments', len(metadata.get('frames', [])))
    frames = metadata.get('frames', [])
    status = 'completed' if (total_segments > 0 and len(frames) >= total_segments) else 'stopped'
    
    total_img_size = metadata.get('total_image_size', 0)
    
    # 模拟沙盒式工作区准备 (在用户点击续传前，不会向其中提取文件，节省性能)
    folder_name = re.sub(r'[\\/*?:"<>|]', '_', metadata.get('video_name', '未命名视频'))
    base_dir = os.path.dirname(os.path.abspath(__file__))
    work_dir = os.path.join(base_dir, 'static', f"_{folder_name}_temp_{task_id[:8]}")

    if is_zip_mode:
        if total_img_size == 0:
            total_img_size = os.path.getsize(input_path)
    else:
        work_dir = input_path
        if total_img_size == 0 and frames:
            for fr in frames:
                p = os.path.join(work_dir, fr['image'])
                if os.path.exists(p):
                    total_img_size += os.path.getsize(p)

    acq_time = metadata.get('acquisition_time', '')
    if not acq_time:
        acq_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getctime(input_path)))

    tasks[task_id] = {
        'id': task_id,
        'video_name': metadata.get('video_name', '本地视频'),
        'm3u8_url': metadata.get('m3u8_url', ''),
        'cookie': metadata.get('cookie', ''),
        'ffmpeg_path': metadata.get('ffmpeg_path', ''),
        'strategy': metadata.get('strategy', 'traffic'),
        'time_max_size_mb': metadata.get('time_max_size_mb', 1.0),
        'is_unlimited': metadata.get('is_unlimited', False),
        'use_proxy': metadata.get('use_proxy', False),
        'proxy_address': metadata.get('proxy_address', '127.0.0.1:1080'),
        'work_dir': work_dir,
        'archive_path': input_path if is_zip_mode else None,
        'status': status,
        'total_segments': total_segments,
        'current': metadata.get('current', len(frames)),
        'total_duration': metadata.get('total_duration', 0),
        'total_original_size': metadata.get('total_original_size', 0),
        'total_traffic': metadata.get('total_traffic', 0),
        'total_image_size': total_img_size,
        'acquisition_time': acq_time,
        'elapsed_time': metadata.get('elapsed_time', 0),
        'frames': frames,
        'failed_segments': metadata.get('failed_segments', []),
        'cancelled': False,
        'start_time': None
    }

    return jsonify({
        'success': True, 
        'task_id': task_id, 
        'task_data': tasks[task_id]
    })

@app.route('/api/status/<task_id>')
def get_status(task_id):
    data = get_task_status_dict(task_id)
    if not data:
        return jsonify({'success': False, 'error': '任务不存在'})
    data['success'] = True
    return jsonify(data)

@app.route('/api/frame/<task_id>/<path:filename>')
def get_frame(task_id, filename):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    # 内存流式无损读取核心：
    # 我们不需要在硬盘解压缩任何文件，直接从归档 ZIP 或临时 ZIP 内调取数据流到前端展示！
    
    target_zip = None
    temp_zip = os.path.join(task.get('work_dir', ''), 'frames.zip')
    
    if os.path.exists(temp_zip):
        target_zip = temp_zip
    elif task.get('archive_path') and os.path.exists(task.get('archive_path')):
        target_zip = task['archive_path']
        
    if target_zip:
        try:
            with zipfile.ZipFile(target_zip, 'r') as zf:
                if filename in zf.namelist():
                    data = zf.read(filename)
                    return send_file(io.BytesIO(data), mimetype='image/jpeg')
        except Exception:
            pass

    # 向后兼容：应对纯旧版的离散文件夹目录读取
    work_dir = task.get('work_dir', '')
    if work_dir and os.path.exists(work_dir):
        raw_path = os.path.join(work_dir, filename)
        if os.path.exists(raw_path):
            return send_file(raw_path, mimetype='image/jpeg')

    return jsonify({'success': False, 'error': '图片流读取失败或不存在'}), 404

@app.route('/api/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    task = tasks.get(task_id)
    if task:
        task['cancelled'] = True
    return jsonify({'success': True})

def open_browser():
    time.sleep(1.2)
    target_url = "http://127.0.0.1:5000"
    import webbrowser
    webbrowser.open(target_url)

if __name__ == '__main__':
    try:
        silent_cookie = extract_115_cookies_from_browser()
        if silent_cookie and silent_cookie != "ERROR_LOCKED":
            cfg = load_config()
            if not cfg.get("cookie"):
                cfg["cookie"] = silent_cookie
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=4)
    except:
        pass

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)