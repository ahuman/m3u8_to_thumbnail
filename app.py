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


def build_headers(m3u8_url, cookie=''):
    """构建请求头，自动从m3u8 URL推断Referer和Origin"""
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
    """下载m3u8文本内容"""
    headers = build_headers(m3u8_url, cookie)
    try:
        resp = requests.get(m3u8_url, headers=headers, verify=False, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise Exception(f"下载m3u8失败: {str(e)}")


def parse_master_m3u8(m3u8_content, base_url):
    """解析master m3u8，提取media m3u8地址（多层结构）"""
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
    """解析media m3u8，提取ts切片信息"""
    segments = []
    lines = [line.strip() for line in m3u8_content.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-KEY:"):
            pass  # 检测到加密，暂不处理密钥
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":")[1].split(",")[0])
            i += 1
            if i < len(lines):
                seg_url = urljoin(base_url, lines[i])
                segments.append({"duration": duration, "url": seg_url})
        i += 1
    return segments


def resolve_m3u8(m3u8_url, cookie=''):
    """解析m3u8（支持多层结构），返回ts切片列表和总时长"""
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
    """使用ffmpeg从ts文件中提取首帧图片，并精准校验画面完整性"""
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
        
        # 1. 检查是否成功生成了文件
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 0:
            stderr = result.stderr.lower()
            
            # 2. 核心修复：只拦截“画面宏块损坏”或“掩盖错误”，忽略因强行截断产生的常规 EOF 报错
            if "concealing" in stderr or "error while decoding mb" in stderr:
                return False  # 画面确实有拉伸/拖影，返回 False 继续下载更多数据
            
            return True # 画面完整，出图成功
            
        return False
    except Exception:
        return False


def smart_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1536*1024, ffmpeg_path=''):
    """边下载边提取：每 128KB 尝试一次提取，依靠 FFmpeg 的报错日志做严格拦截"""
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


def process_task(task_id, m3u8_url, video_name, cookie=''):
    """后台处理任务"""
    task = tasks[task_id]
    work_dir = task['work_dir']
    ffmpeg_path = task.get('ffmpeg_path', '')

    def save_metadata():
        """持久化保存已下载的帧数据及耗时信息到 JSON 文件中"""
        if task.get('frames'):
            meta_path = os.path.join(work_dir, 'metadata.json')
            try:
                # 动态计算耗时
                if 'start_time' in task:
                    if task['status'] in ['starting', 'parsing', 'downloading']:
                        current_elapsed = time.time() - task['start_time']
                    else:
                        current_elapsed = task.get('end_time', time.time()) - task['start_time']
                else:
                    current_elapsed = task.get('elapsed_time', 0)

                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'video_name': task.get('video_name', '未命名视频'),
                        'total_duration': task.get('total_duration', 0),
                        'total_segments': task.get('total_segments', 0),
                        'total_original_size': task.get('total_original_size', 0),
                        'total_traffic': task.get('total_traffic', 0),
                        'elapsed_time': current_elapsed,
                        'frames': task.get('frames', [])
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"写入元数据失败: {e}")

    try:
        task['status'] = 'parsing'
        segments, total_duration = resolve_m3u8(m3u8_url, cookie)

        if not segments:
            task['status'] = 'error'
            task['error'] = '未找到ts切片'
            task['end_time'] = time.time()
            return

        task['total_segments'] = len(segments)
        task['total_duration'] = total_duration
        task['status'] = 'downloading'

        for idx, seg in enumerate(segments):
            if task.get('cancelled'):
                if task['frames']:
                    task['status'] = 'stopped'
                else:
                    task['status'] = 'cancelled'
                task['end_time'] = time.time()
                save_metadata()
                return

            ts_path = os.path.join(work_dir, f'seg_{idx:04d}.ts')
            img_path = os.path.join(work_dir, f'frame_{idx:04d}.jpg')

            success, orig_size, traffic = smart_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, ffmpeg_path=ffmpeg_path)
            
            if not success:
                task['failed_segments'].append(idx)
                task['current'] = idx + 1
                if os.path.exists(ts_path):
                    os.remove(ts_path)
                continue

            task['total_original_size'] += orig_size
            task['total_traffic'] += traffic

            task['frames'].append({
                'index': idx,
                'time': sum(s['duration'] for s in segments[:idx]),
                'duration': seg['duration'],
                'image': f'frame_{idx:04d}.jpg'
            })

            if os.path.exists(ts_path):
                os.remove(ts_path)

            task['current'] = idx + 1
            
            if (idx + 1) % 10 == 0:
                save_metadata()

        task['status'] = 'completed'
        task['end_time'] = time.time()
        save_metadata()

    except Exception as e:
        task['status'] = 'error'
        task['error'] = str(e)
        task['end_time'] = time.time()
        save_metadata()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/start', methods=['POST'])
def start_download():
    data = request.json
    m3u8_url = data.get('m3u8_url', '').strip()
    video_name = data.get('video_name', '').strip()
    cookie = data.get('cookie', '').strip()
    ffmpeg_path = data.get('ffmpeg_path', '').strip()

    if not m3u8_url:
        return jsonify({'success': False, 'error': '请输入m3u8网址'})

    task_id = str(uuid.uuid4())
    
    # 核心修改：处理视频名称，作为文件夹名称（过滤掉Windows/Linux不支持的特殊字符）
    if video_name:
        folder_name = re.sub(r'[\\/*?:"<>|]', '_', video_name)
    else:
        # 如果没填名称，用 UUID 前8位兜底
        folder_name = f"未命名视频_{task_id[:8]}"

    # 获取 app.py 文件所在的绝对路径目录，拼接到 static/ 下
    base_dir = os.path.dirname(os.path.abspath(__file__))
    work_dir = os.path.join(base_dir, 'static', folder_name)
    os.makedirs(work_dir, exist_ok=True)

    tasks[task_id] = {
        'id': task_id,
        'video_name': video_name or '未命名视频',
        'm3u8_url': m3u8_url,
        'cookie': cookie,
        'ffmpeg_path': ffmpeg_path,
        'work_dir': work_dir,
        'status': 'starting',
        'start_time': time.time(),
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


@app.route('/api/load_local', methods=['POST'])
def load_local():
    """读取本地已完成文件夹中的元数据并挂载"""
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
    tasks[task_id] = {
        'id': task_id,
        'video_name': metadata.get('video_name', '本地视频'),
        'm3u8_url': '',
        'work_dir': folder_path,
        'status': 'completed',
        'total_segments': metadata.get('total_segments', len(metadata.get('frames', []))),
        'current': len(metadata.get('frames', [])),
        'total_duration': metadata.get('total_duration', 0),
        'total_original_size': metadata.get('total_original_size', 0),
        'total_traffic': metadata.get('total_traffic', 0),
        'elapsed_time': metadata.get('elapsed_time', 0),
        'frames': metadata.get('frames', []),
        'failed_segments': [],
        'cancelled': False
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

    # 动态计算耗时返回前端
    if 'start_time' in task:
        if task['status'] in ['starting', 'parsing', 'downloading']:
            elapsed_time = time.time() - task['start_time']
        else:
            elapsed_time = task.get('end_time', time.time()) - task['start_time']
    else:
        # 本地加载的任务没有 start_time
        elapsed_time = task.get('elapsed_time', 0)

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
    app.run(host='0.0.0.0', port=5000, debug=True)
