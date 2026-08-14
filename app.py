import os
import re
import json
import uuid
import time
import shutil
import requests
import subprocess
import threading
from urllib.parse import urljoin, urlparse
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

# 存储下载任务状态
tasks = {}

# 配置文件路径
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

def load_config():
    """读取配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"cookie": "", "ffmpeg_path": "", "strategy": "traffic", "time_max_size_mb": 1.0, "is_unlimited": False}

def save_config(cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited):
    """保存配置到文件"""
    try:
        config = load_config()
        config['cookie'] = cookie
        config['ffmpeg_path'] = ffmpeg_path
        config['strategy'] = strategy
        config['time_max_size_mb'] = time_max_size_mb
        config['is_unlimited'] = is_unlimited
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存配置失败: {e}")


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


def download_m3u8_text(m3u8_url, cookie=''):
    headers = build_headers(m3u8_url, cookie)
    try:
        resp = requests.get(m3u8_url, headers=headers, verify=False, timeout=20)
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


def resolve_m3u8(m3u8_url, cookie=''):
    base_url = m3u8_url.rsplit('/', 1)[0] + '/'
    content = download_m3u8_text(m3u8_url, cookie)
    media_urls = parse_master_m3u8(content, base_url)

    if media_urls:
        media_url = media_urls[0]
        media_base = media_url.rsplit('/', 1)[0] + '/'
        media_content = download_m3u8_text(media_url, cookie)
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
                return False  
            return True 
        return False
    except Exception:
        return False


def smart_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1536*1024, ffmpeg_path=''):
    """流量优先策略：边下载边提取"""
    headers = build_headers(m3u8_url, cookie)

    for retry in range(3):
        try:
            range_headers = headers.copy()
            range_headers['Range'] = f'bytes=0-{max_size-1}'
            
            resp = requests.get(ts_url, headers=range_headers, verify=False, timeout=30, stream=True)
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


def fast_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1024*1024, ffmpeg_path=''):
    """时间优先策略：固定大小提取"""
    headers = build_headers(m3u8_url, cookie)

    for retry in range(3):
        try:
            range_headers = headers.copy()
            if max_size > 0:
                range_headers['Range'] = f'bytes=0-{max_size-1}'

            resp = requests.get(ts_url, headers=range_headers, verify=False, timeout=30, stream=True)
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
    """后台处理任务，支持断点续传与失败重试逻辑"""
    task = tasks[task_id]
    work_dir = task['work_dir']
    ffmpeg_path = task.get('ffmpeg_path', '')
    strategy = task.get('strategy', 'traffic')
    time_max_size = task.get('time_max_size', 1048576)

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
                        'total_duration': task.get('total_duration', 0),
                        'total_segments': task.get('total_segments', 0),
                        'current': task.get('current', 0),                    # 持久化保存断点
                        'failed_segments': task.get('failed_segments', []),   # 持久化保存失败列表
                        'total_original_size': task.get('total_original_size', 0),
                        'total_traffic': task.get('total_traffic', 0),
                        'elapsed_time': elapsed,
                        'frames': task.get('frames', [])
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"写入元数据失败: {e}")

    task['start_time'] = time.time()
    task['cancelled'] = False

    try:
        task['status'] = 'parsing'
        segments, total_duration = resolve_m3u8(m3u8_url, cookie)

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

            task['current'] = idx + 1  # 实时更新扫描进度
            
            # 断点续传核心：如果有成功的记录，直接跳过并保留进度
            if idx in existing_frames:
                continue

            ts_path = os.path.join(work_dir, f'seg_{idx:04d}.ts')
            img_path = os.path.join(work_dir, f'frame_{idx:04d}.jpg')

            if strategy == 'traffic':
                success, orig_size, traffic = smart_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, ffmpeg_path=ffmpeg_path)
            else:
                success, orig_size, traffic = fast_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, max_size=time_max_size, ffmpeg_path=ffmpeg_path)
            
            if not success:
                if idx not in task['failed_segments']:
                    task['failed_segments'].append(idx)
                if os.path.exists(ts_path):
                    os.remove(ts_path)
                continue

            # 如果这帧之前失败过，这次成功了，就把他移出失败列表
            if idx in task['failed_segments']:
                task['failed_segments'].remove(idx)

            task['total_original_size'] += orig_size
            task['total_traffic'] += traffic

            task['frames'].append({
                'index': idx,
                'time': sum(s['duration'] for s in segments[:idx]),
                'duration': seg['duration'],
                'image': f'frame_{idx:04d}.jpg'
            })
            
            # 严格排序，保障传给前端的列表永远是有序的
            task['frames'] = sorted(task['frames'], key=lambda k: k['index'])

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


@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', 
                           default_cookie=config.get('cookie', ''), 
                           default_ffmpeg=config.get('ffmpeg_path', ''),
                           default_strategy=config.get('strategy', 'traffic'),
                           default_time_max_size_mb=config.get('time_max_size_mb', 1.0),
                           default_is_unlimited=config.get('is_unlimited', False))


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

    save_config(cookie, ffmpeg_path, strategy, time_max_size_mb, is_unlimited)

    if not m3u8_url:
        return jsonify({'success': False, 'error': '请输入m3u8网址'})

    task_id = str(uuid.uuid4())
    
    if video_name:
        folder_name = re.sub(r'[\\/*?:"<>|]', '_', video_name)
    else:
        folder_name = f"未命名视频_{task_id[:8]}"

    base_dir = os.path.dirname(os.path.abspath(__file__))
    work_dir = os.path.join(base_dir, 'static', folder_name)
    os.makedirs(work_dir, exist_ok=True)

    tasks[task_id] = {
        'id': task_id,
        'video_name': video_name or '未命名视频',
        'm3u8_url': m3u8_url,
        'cookie': cookie,
        'ffmpeg_path': ffmpeg_path,
        'strategy': strategy,
        'time_max_size': time_max_size,
        'time_max_size_mb': time_max_size_mb,
        'is_unlimited': is_unlimited,
        'work_dir': work_dir,
        'status': 'starting',
        'start_time': None,
        'elapsed_time': 0,
        'total_segments': 0,
        'current': 0,
        'total_duration': 0,
        'total_original_size': 0,  
        'total_traffic': 0,        
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

    if not task['m3u8_url']:
         return jsonify({'success': False, 'error': '未找到M3U8链接，请在输入框补全'})

    task['status'] = 'starting'
    
    thread = threading.Thread(target=process_task, args=(task_id, task['m3u8_url'], task['video_name'], task['cookie']))
    thread.daemon = True
    thread.start()

    return jsonify({'success': True})


@app.route('/api/load_local', methods=['POST'])
def load_local():
    data = request.json
    folder_path = data.get('folder_path', '').strip()
    
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({'success': False, 'error': '无效的本地文件夹路径'})
        
    metadata_path = os.path.join(folder_path, 'metadata.json')
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

    tasks[task_id] = {
        'id': task_id,
        'video_name': metadata.get('video_name', '本地视频'),
        'm3u8_url': metadata.get('m3u8_url', ''),
        'cookie': metadata.get('cookie', ''),
        'ffmpeg_path': metadata.get('ffmpeg_path', ''),
        'strategy': metadata.get('strategy', 'traffic'),
        'time_max_size_mb': metadata.get('time_max_size_mb', 1.0),
        'is_unlimited': metadata.get('is_unlimited', False),
        'work_dir': folder_path,
        'status': status,
        'total_segments': total_segments,
        'current': metadata.get('current', len(frames)), # 恢复原进度记录点
        'total_duration': metadata.get('total_duration', 0),
        'total_original_size': metadata.get('total_original_size', 0),
        'total_traffic': metadata.get('total_traffic', 0),
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
    task = tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})

    elapsed_time = task.get('elapsed_time', 0)
    if task.get('start_time'):
        elapsed_time += time.time() - task['start_time']

    return jsonify({
        'success': True,
        'status': task['status'],
        'total_segments': task['total_segments'],
        'current': task['current'],
        'total_duration': task['total_duration'],
        'total_original_size': task.get('total_original_size', 0),
        'total_traffic': task.get('total_traffic', 0),
        'elapsed_time': elapsed_time,
        'frames': task['frames'],
        'failed_segments': task['failed_segments'],
        'error': task.get('error', ''),
        'work_dir': task['work_dir']  
    })


@app.route('/api/frame/<task_id>/<path:filename>')
def get_frame(task_id, filename):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    file_path = os.path.join(task['work_dir'], filename)
    if os.path.exists(file_path):
        return send_file(file_path, mimetype='image/jpeg')
    return jsonify({'success': False, 'error': '图片不存在'}), 404


@app.route('/api/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    task = tasks.get(task_id)
    if task:
        task['cancelled'] = True
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
