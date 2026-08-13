import os
import re
import json
import uuid
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

    # 1. 下载master m3u8
    content = download_m3u8_text(m3u8_url, cookie)

    # 2. 检查是否是master m3u8（多层）
    media_urls = parse_master_m3u8(content, base_url)

    if media_urls:
        # 多层结构：下载第一个media m3u8（通常第一个清晰度最高）
        media_url = media_urls[0]
        media_base = media_url.rsplit('/', 1)[0] + '/'
        media_content = download_m3u8_text(media_url, cookie)
        segments = parse_media_m3u8(media_content, media_base)
    else:
        # 单层结构：直接解析
        segments = parse_media_m3u8(content, base_url)

    total_duration = sum(s["duration"] for s in segments)
    return segments, total_duration


def extract_first_frame(ts_path, output_image_path, ffmpeg_path=''):
    """使用ffmpeg从ts文件中提取首帧图片"""
    # 智能解析 ffmpeg 路径
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
            return True
        return False
    except Exception as e:
        return False


def smart_download_and_extract(ts_url, ts_path, img_path, m3u8_url, cookie='', max_size=1536*1024, ffmpeg_path=''):
    """边下载边提取：每 128KB 尝试一次提取，成功则立即中断连接，极限节省流量"""
    headers = build_headers(m3u8_url, cookie)

    for retry in range(3):
        try:
            range_headers = headers.copy()
            # 设定一个极限安全水位，防止遇到死流一直下
            range_headers['Range'] = f'bytes=0-{max_size-1}'
            
            # stream=True 是核心，允许我们按块读取并随时切断
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
            check_interval = 128 * 1024  # 每 128KB 触发一次 FFmpeg 盲盒测试
            success = False

            with open(ts_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        buffer_size += len(chunk)

                        # 数据积攒达到检查点
                        if buffer_size >= check_interval:
                            f.flush()  # 确保内存数据已刷入硬盘
                            if extract_first_frame(ts_path, img_path, ffmpeg_path):
                                resp.close()  # 提取成功，果断斩断下载连接！
                                success = True
                                break
                            buffer_size = 0  # 没成功，重置积攒器，继续下

                        if downloaded >= max_size:
                            break
            
            # 如果循环结束了还没成功（比如文件总共就不够 128KB），做最后一次保底尝试
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

    try:
        # 1. 解析m3u8（支持多层结构）
        task['status'] = 'parsing'
        segments, total_duration = resolve_m3u8(m3u8_url, cookie)

        if not segments:
            task['status'] = 'error'
            task['error'] = '未找到ts切片'
            return

        task['total_segments'] = len(segments)
        task['total_duration'] = total_duration
        task['status'] = 'downloading'

        # 2. 依次处理每个ts切片
        for idx, seg in enumerate(segments):
            if task.get('cancelled'):
                if task['frames']:
                    task['status'] = 'stopped'
                else:
                    task['status'] = 'cancelled'
                return

            ts_path = os.path.join(work_dir, f'seg_{idx:04d}.ts')
            img_path = os.path.join(work_dir, f'frame_{idx:04d}.jpg')

            # 使用流式截断下载策略
            success, orig_size, traffic = smart_download_and_extract(seg['url'], ts_path, img_path, m3u8_url, cookie, ffmpeg_path=ffmpeg_path)
            
            if not success:
                task['failed_segments'].append(idx)
                task['current'] = idx + 1
                if os.path.exists(ts_path):
                    os.remove(ts_path)
                continue

            # 累计大小与流量
            task['total_original_size'] += orig_size
            task['total_traffic'] += traffic

            # 记录成功帧
            task['frames'].append({
                'index': idx,
                'time': sum(s['duration'] for s in segments[:idx]),
                'duration': seg['duration'],
                'image': f'frame_{idx:04d}.jpg'
            })

            # 删除ts文件节省空间
            if os.path.exists(ts_path):
                os.remove(ts_path)

            task['current'] = idx + 1

        task['status'] = 'completed'

    except Exception as e:
        task['status'] = 'error'
        task['error'] = str(e)


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
    
    # 获取 app.py 文件所在的绝对路径目录，拼接到 static/tasks
    base_dir = os.path.dirname(os.path.abspath(__file__))
    work_dir = os.path.join(base_dir, 'static', 'tasks', task_id)
    os.makedirs(work_dir, exist_ok=True)

    tasks[task_id] = {
        'id': task_id,
        'video_name': video_name or '未命名视频',
        'm3u8_url': m3u8_url,
        'cookie': cookie,
        'ffmpeg_path': ffmpeg_path,
        'work_dir': work_dir,
        'status': 'starting',
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


@app.route('/api/status/<task_id>')
def get_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})

    return jsonify({
        'success': True,
        'status': task['status'],
        'total_segments': task['total_segments'],
        'current': task['current'],
        'total_duration': task['total_duration'],
        'total_original_size': task.get('total_original_size', 0),
        'total_traffic': task.get('total_traffic', 0),
        'frames': task['frames'],
        'failed_segments': task['failed_segments'],
        'error': task.get('error', '')
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
