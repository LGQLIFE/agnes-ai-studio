"""
数字人口播视频生成路由模块
包含：文稿分段、TTS 配音、画面生成、字幕烧录、视频合成
支持三种模式：A-静态形象图、B-视频素材、C-AI 生成画面
"""

import os
import uuid
import json
import time
import threading
import subprocess
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify

from ..config import (
    get_app_dir, get_api_key, get_vendor_base_url, get_vendor_api_key,
    ensure_output_dirs, shutdown_event
)
from ..models import DEFAULT_TEXT_MODEL, DEFAULT_IMAGE_MODEL, DEFAULT_VIDEO_MODEL, TEXT_MODEL_OPTIONS, IMAGE_MODEL_OPTIONS, VIDEO_MODEL_OPTIONS
from ..services.text_model import call_text_model
from ..services.tts import generate_tts_audio, get_audio_duration, get_available_voices
from ..services.video_gen import download_video_by_video_id, download_and_save_file
from ..services.video_merge import get_ffmpeg_path, get_video_duration, burn_chinese_subtitle, _find_chinese_font

anchor_bp = Blueprint('anchor', __name__)

# ==================== 任务状态管理 ====================

anchor_tasks = {}
anchor_lock = threading.Lock()
anchor_task_id = None  # 当前任务 ID（单任务模式）


def _update_anchor(task_id, **kwargs):
    """更新任务状态"""
    with anchor_lock:
        if task_id in anchor_tasks:
            anchor_tasks[task_id].update(kwargs)
            anchor_tasks[task_id]['updated_at'] = datetime.now().isoformat()


# ==================== API 端点 ====================

@anchor_bp.route('/api/anchor/voices', methods=['GET'])
def anchor_voices():
    """获取可用的 TTS 音色列表"""
    voices = get_available_voices()
    return jsonify({'success': True, 'voices': voices})


@anchor_bp.route('/api/anchor/models', methods=['GET'])
def anchor_models():
    """获取可用的模型选项"""
    return jsonify({
        'success': True,
        'text_models': TEXT_MODEL_OPTIONS,
        'image_models': IMAGE_MODEL_OPTIONS,
        'video_models': VIDEO_MODEL_OPTIONS,
        'defaults': {
            'text_model': DEFAULT_TEXT_MODEL,
            'image_model': DEFAULT_IMAGE_MODEL,
            'video_model': DEFAULT_VIDEO_MODEL,
        }
    })


@anchor_bp.route('/api/anchor/upload', methods=['POST'])
def anchor_upload():
    """上传数字人形象图片或视频"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未找到上传文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.mp4', '.mov', '.avi'}
    if ext not in allowed_exts:
        return jsonify({'success': False, 'error': f'不支持的文件格式: {ext}'}), 400

    app_dir = get_app_dir()
    anchor_dir = os.path.join(app_dir, 'anchor')
    os.makedirs(anchor_dir, exist_ok=True)

    unique_name = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    save_path = os.path.join(anchor_dir, unique_name)
    file.save(save_path)

    file_size = os.path.getsize(save_path)
    print(f"[数字人] 上传文件: {unique_name} ({file_size // 1024}KB)")

    return jsonify({
        'success': True,
        'filename': unique_name,
        'url': f'/anchor/{unique_name}',
        'size': file_size
    })


@anchor_bp.route('/api/anchor/generate', methods=['POST'])
def anchor_generate():
    """提交数字人口播任务"""
    global anchor_task_id

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体为空'}), 400

    script = data.get('script', '').strip()
    if not script:
        return jsonify({'success': False, 'error': '请输入文稿内容'}), 400

    mode = data.get('mode', 'A')  # A/B/C
    voice = data.get('voice', 'zh-CN-XiaoxiaoNeural')
    avatar_file = data.get('avatar_file', '')  # 上传的形象文件名
    video_prompt = data.get('video_prompt', '')  # C 模式的画面提示词
    min_duration = int(data.get('min_duration', 5))  # 每段最小时长（秒）
    text_model = data.get('text_model', DEFAULT_TEXT_MODEL)
    image_model = data.get('image_model', DEFAULT_IMAGE_MODEL)
    video_model = data.get('video_model', DEFAULT_VIDEO_MODEL)

    # 验证参数
    if mode == 'A' and not avatar_file:
        return jsonify({'success': False, 'error': '模式 A 需要上传数字人形象图片'}), 400
    if mode == 'B' and not avatar_file:
        return jsonify({'success': False, 'error': '模式 B 需要上传数字人视频素材'}), 400
    if mode == 'C' and not video_prompt:
        return jsonify({'success': False, 'error': '模式 C 需要输入画面风格提示词'}), 400

    # 创建任务
    task_id = f"anchor_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    app_dir = get_app_dir()
    task_dir = os.path.join(app_dir, 'anchor', task_id)
    os.makedirs(task_dir, exist_ok=True)

    task = {
        'task_id': task_id,
        'status': 'processing',
        'step': 'init',
        'message': '正在初始化...',
        'mode': mode,
        'voice': voice,
        'script': script,
        'avatar_file': avatar_file,
        'video_prompt': video_prompt,
        'min_duration': min_duration,
        'text_model': text_model,
        'image_model': image_model,
        'video_model': video_model,
        'task_dir': task_dir,
        'segments': [],
        'output_file': None,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
    }

    with anchor_lock:
        anchor_tasks[task_id] = task
        anchor_task_id = task_id

    # 启动后台线程
    t = threading.Thread(target=_anchor_pipeline, args=(task_id,), daemon=True)
    t.start()

    return jsonify({'success': True, 'task_id': task_id})


@anchor_bp.route('/api/anchor/status', methods=['GET'])
def anchor_status():
    """查询当前任务进度"""
    task_id = request.args.get('task_id', anchor_task_id)
    if not task_id or task_id not in anchor_tasks:
        return jsonify({'success': True, 'task': None})

    with anchor_lock:
        task = dict(anchor_tasks[task_id])

    # 不返回完整文稿（太大）
    task.pop('script', None)
    task.pop('task_dir', None)

    return jsonify({'success': True, 'task': task})


# ==================== 流水线逻辑 ====================

def _anchor_pipeline(task_id):
    """数字人口播视频生成流水线（后台线程）"""
    with anchor_lock:
        task = dict(anchor_tasks[task_id])

    mode = task['mode']
    voice = task['voice']
    script = task['script']
    avatar_file = task.get('avatar_file', '')
    video_prompt = task.get('video_prompt', '')
    min_duration = task.get('min_duration', 5)
    text_model = task.get('text_model', DEFAULT_TEXT_MODEL)
    image_model = task.get('image_model', DEFAULT_IMAGE_MODEL)
    video_model = task.get('video_model', DEFAULT_VIDEO_MODEL)
    task_dir = task['task_dir']
    api_key = get_api_key()

    print(f"\n{'='*60}")
    print(f"[数字人 {task_id}] 开始流水线 模式={mode} 音色={voice}")
    print(f"{'='*60}\n")

    try:
        # ---- Step 1: 文稿分段 ----
        _update_anchor(task_id, step='segment', message='正在分析文稿并分段...')
        segments = _segment_script(script, api_key, text_model, min_duration)
        if not segments:
            _update_anchor(task_id, status='failed', message='文稿分段失败')
            return

        print(f"[数字人 {task_id}] 文稿分为 {len(segments)} 段")
        _update_anchor(task_id, segments=segments)

        # ---- Step 2: TTS 配音 ----
        _update_anchor(task_id, step='tts', message='正在生成 TTS 配音...')
        audio_dir = os.path.join(task_dir, 'audio')
        os.makedirs(audio_dir, exist_ok=True)

        for i, seg in enumerate(segments):
            if shutdown_event.is_set():
                _update_anchor(task_id, status='cancelled', message='已取消')
                return

            text = seg['text']
            audio_path = os.path.join(audio_dir, f'seg_{i:03d}.mp3')
            _update_anchor(task_id, message=f'正在生成第 {i+1}/{len(segments)} 段配音...')

            result = generate_tts_audio(text, voice, audio_path)
            if result:
                duration = get_audio_duration(audio_path)
                # 确保每段至少 min_duration 秒
                if duration < min_duration:
                    duration = min_duration
                with anchor_lock:
                    anchor_tasks[task_id]['segments'][i]['audio_file'] = f'seg_{i:03d}.mp3'
                    anchor_tasks[task_id]['segments'][i]['duration'] = round(duration, 2)
                print(f"[数字人 {task_id}] 第 {i+1} 段配音完成: {duration:.1f}s")
            else:
                print(f"[数字人 {task_id}] 第 {i+1} 段配音失败")
                _update_anchor(task_id, status='failed', message=f'第 {i+1} 段配音生成失败')
                return

        # ---- Step 3: 画面生成 ----
        _update_anchor(task_id, step='visual', message='正在生成画面...')
        visual_dir = os.path.join(task_dir, 'visuals')
        os.makedirs(visual_dir, exist_ok=True)

        if mode == 'A':
            success = _mode_a_static_image(task_id, avatar_file, segments, visual_dir, api_key, video_model)
        elif mode == 'B':
            success = _mode_b_video_material(task_id, avatar_file, segments, visual_dir)
        else:
            success = _mode_c_ai_generate(task_id, video_prompt, segments, visual_dir, api_key, video_model)

        if not success:
            return

        # ---- Step 4: 逐段合成视频 ----
        _update_anchor(task_id, step='compose', message='正在合成视频...')
        segment_videos = []
        ffmpeg = get_ffmpeg_path()

        for i, seg in enumerate(segments):
            if shutdown_event.is_set():
                _update_anchor(task_id, status='cancelled', message='已取消')
                return

            _update_anchor(task_id, message=f'正在合成第 {i+1}/{len(segments)} 段视频...')
            visual_path = seg.get('visual_path', '')
            audio_path = os.path.join(audio_dir, f'seg_{i:03d}.mp3')
            output_path = os.path.join(task_dir, f'segment_{i:03d}.mp4')

            if not os.path.exists(visual_path) or not os.path.exists(audio_path):
                print(f"[数字人 {task_id}] 第 {i+1} 段素材缺失，跳过")
                continue

            duration = seg.get('duration', 5)
            subtitle_text = seg['text'][:200]  # 字幕取前200字

            # 获取视频尺寸（模式A会根据图片尺寸动态设置，其他模式用默认值）
            with anchor_lock:
                vid_w = anchor_tasks[task_id].get('video_width', 1152)
                vid_h = anchor_tasks[task_id].get('video_height', 768)

            success = _compose_segment_video(
                ffmpeg, visual_path, audio_path, subtitle_text,
                duration, output_path, mode, video_width=vid_w, video_height=vid_h
            )
            if success:
                segment_videos.append(output_path)
                print(f"[数字人 {task_id}] 第 {i+1} 段视频合成完成")
            else:
                print(f"[数字人 {task_id}] 第 {i+1} 段视频合成失败")

        if not segment_videos:
            _update_anchor(task_id, status='failed', message='没有成功合成的视频段')
            return

        # ---- Step 5: 最终拼接 ----
        _update_anchor(task_id, step='merge', message='正在拼接最终视频...')
        final_output = _merge_final_video(ffmpeg, segment_videos, task_dir, task_id)

        if final_output:
            _update_anchor(
                task_id,
                status='completed',
                step='completed',
                message=f'数字人口播视频生成完成！共 {len(segments)} 段',
                output_file=os.path.basename(final_output)
            )
            print(f"[数字人 {task_id}] 最终视频: {os.path.basename(final_output)}")
        else:
            _update_anchor(task_id, status='failed', message='最终视频拼接失败')

    except Exception as e:
        print(f"[数字人 {task_id}] 流水线异常: {e}")
        import traceback
        traceback.print_exc()
        _update_anchor(task_id, status='failed', message=f'生成失败: {str(e)}')


# ==================== Step 1: 文稿分段 ====================

def _segment_script(script, api_key, text_model=None, min_duration=5):
    """调用文本模型将文稿按语义分段"""
    # 根据最小时长计算每段字数（中文约 4 字/秒）
    chars_per_sec = 4
    min_chars = min_duration * chars_per_sec
    max_chars = max(min_chars * 2, 120)
    system_prompt = f"""你是一个专业的视频文稿编辑。你的任务是将用户提供的长文稿按照语义段落进行合理分段。

要求：
1. 每段控制在 {min_chars}-{max_chars} 字左右（适合 {min_duration}-{min_duration*3} 秒的语音时长）
2. 保持语义完整，不要在句子中间断开
3. 每段之间要有自然的过渡
4. 直接输出 JSON 数组，每个元素是一个字符串（该段的文本）
5. 不要添加任何额外内容，只输出 JSON

示例输出格式：
["第一段文本内容。", "第二段文本内容。", "第三段文本内容。"]"""

    user_prompt = f"请将以下文稿进行分段：\n\n{script}"

    try:
        result = call_text_model(system_prompt, user_prompt, api_key, model=text_model, max_tokens=4096)
        if not result:
            print("[数字人] 文本模型返回空")
            return _fallback_segment(script)

        # 解析 JSON
        result = result.strip()
        if result.startswith('```'):
            result = result.split('```')[1]
            if result.startswith('json'):
                result = result[4:]
            result = result.strip()

        segments_data = json.loads(result)
        if isinstance(segments_data, list) and len(segments_data) > 0:
            segments = []
            for text in segments_data:
                text = str(text).strip()
                if text:
                    segments.append({'text': text, 'duration': 0})
            if segments:
                return segments
    except (json.JSONDecodeError, Exception) as e:
        print(f"[数字人] 文稿分段解析失败: {e}")

    return _fallback_segment(script)


def _fallback_segment(script):
    """备用的简单分段：按句号/问号/叹号分割，每 2-3 句为一段"""
    import re
    sentences = re.split(r'(?<=[。！？.!?])\s*', script)
    sentences = [s.strip() for s in sentences if s.strip()]

    segments = []
    current = ''
    for s in sentences:
        if len(current) + len(s) > 80 and current:
            segments.append({'text': current.strip(), 'duration': 0})
            current = s
        else:
            current = current + s if current else s

    if current.strip():
        segments.append({'text': current.strip(), 'duration': 0})

    return segments if segments else [{'text': script[:200], 'duration': 0}]


# ==================== Step 3: 三种画面模式 ====================

def _get_image_dimensions(image_path):
    """检测图片尺寸，返回 (width, height)"""
    try:
        # 尝试用 PIL
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size  # (width, height)
    except ImportError:
        pass
    except Exception as e:
        print(f"[数字人] PIL 检测图片尺寸失败: {e}")
    
    # 回退到 ffprobe
    try:
        from ..services.video_merge import get_ffprobe_path
        ffprobe = get_ffprobe_path()
        if ffprobe:
            cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=width,height',
                   '-of', 'csv=p=0', image_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(',')
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
    except Exception as e:
        print(f"[数字人] ffprobe 检测图片尺寸失败: {e}")
    
    return None, None


def _calc_video_dimensions(img_w, img_h):
    """根据图片尺寸计算合适的视频尺寸，保持宽高比，长边不超过 1152"""
    if not img_w or not img_h or img_w <= 0 or img_h <= 0:
        return 1152, 768  # 默认 3:2
    
    # 目标长边 1152，短边按比例缩放并对齐到 8 的倍数（视频编码要求）
    max_side = 1152
    if img_w >= img_h:
        out_w = max_side
        out_h = int(img_h * max_side / img_w)
    else:
        out_h = max_side
        out_w = int(img_w * max_side / img_h)
    
    # 对齐到 8 的倍数
    out_w = max(64, (out_w // 8) * 8)
    out_h = max(64, (out_h // 8) * 8)
    
    return out_w, out_h

def _mode_a_static_image(task_id, avatar_file, segments, visual_dir, api_key=None, video_model=None):
    """模式 A：静态形象图 — 使用 Agnes Video I2V 生成动态画面（人物动作+表情变化）
    
    如果 I2V 失败，自动回退到 Ken Burns 效果。
    """
    import base64
    
    app_dir = get_app_dir()
    image_path = os.path.join(app_dir, 'anchor', avatar_file)
    if not os.path.exists(image_path):
        _update_anchor(task_id, status='failed', message=f'形象图片不存在: {avatar_file}')
        return False

    with anchor_lock:
        for i, seg in enumerate(anchor_tasks[task_id]['segments']):
            visual_path = os.path.join(visual_dir, f'seg_{i:03d}.mp4')
            seg['visual_path'] = visual_path

    # 检测图片尺寸，计算视频尺寸以保持宽高比一致
    img_w, img_h = _get_image_dimensions(image_path)
    vid_w, vid_h = _calc_video_dimensions(img_w, img_h)
    print(f"[数字人 {task_id}] 模式A 图片尺寸={img_w}x{img_h}，视频尺寸={vid_w}x{vid_h}")
    
    # 将尺寸存储到任务数据中，供后续合成使用
    with anchor_lock:
        anchor_tasks[task_id]['video_width'] = vid_w
        anchor_tasks[task_id]['video_height'] = vid_h

    # 将本地图片转为 base64 data URL（供 I2V API 使用）
    image_base64 = None
    try:
        ext = os.path.splitext(avatar_file)[1].lower()
        mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
        mime = mime_map.get(ext, 'image/png')
        with open(image_path, 'rb') as f:
            b64_data = base64.b64encode(f.read()).decode('utf-8')
        image_base64 = f'data:{mime};base64,{b64_data}'
        print(f"[数字人 {task_id}] 模式A 图片已转base64 ({len(b64_data)//1024}KB)")
    except Exception as e:
        print(f"[数字人 {task_id}] 模式A 图片转base64失败: {e}")

    # I2V 提示词：描述人物自然说话的状态
    i2v_prompt = (
        "A person speaking naturally to camera, subtle head movements, "
        "natural facial expressions, mouth moving as if talking, "
        "gentle body sway, professional presenter style, "
        "steady eye contact with camera, smooth breathing motion"
    )

    # 如果有 API 配置，尝试使用 I2V 生成动态视频
    use_i2v = api_key and video_model and image_base64
    if use_i2v:
        base_url = get_vendor_base_url(video_model)
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    ffmpeg = get_ffmpeg_path()
    
    # 记录上次视频请求时间，用于速率限制控制（2 requests/min）
    _last_video_request_time = 0
    
    for i, seg in enumerate(segments):
        if shutdown_event.is_set():
            return False
        duration = seg.get('duration', 5)
        if duration < 1:
            duration = 5
        visual_path = os.path.join(visual_dir, f'seg_{i:03d}.mp4')
        seg_text = seg.get('text', '')

        i2v_success = False
        
        # 尝试使用 I2V API 生成动态视频
        if use_i2v:
            # 速率限制控制：确保两次请求间隔至少 35 秒
            elapsed = time.time() - _last_video_request_time
            if elapsed < 35 and _last_video_request_time > 0:
                wait_sec = 35 - elapsed
                print(f"[数字人 {task_id}] 模式A 速率限制保护：等待 {wait_sec:.0f} 秒...")
                _update_anchor(task_id, message=f'等待 API 限流恢复 ({wait_sec:.0f}s)...')
                time.sleep(wait_sec)
            _last_video_request_time = time.time()
            _update_anchor(task_id, message=f'AI 生成第 {i+1}/{len(segments)} 段动态画面...')
            num_frames = min(int(duration * 24) // 8 * 8 + 1, 441)
            
            if video_model.startswith('agnes-video-2.5'):
                # Agnes Video 2.5 新参数格式：首帧图生视频（keyframe 模式）
                payload = {
                    'model': video_model,
                    'prompt': i2v_prompt,
                    'mode': 'keyframe',
                    'first_frame': image_base64,
                    'seconds': str(max(4, min(12, round(duration)))),
                    'size': '720P',
                }
            else:
                payload = {
                    'model': video_model,
                    'prompt': i2v_prompt,
                    'image': image_base64,
                    'width': vid_w,
                    'height': vid_h,
                    'num_frames': num_frames,
                    'frame_rate': 24,
                }
            
            max_retries = 2
            for attempt in range(max_retries):
                if shutdown_event.is_set():
                    return False
                print(f"[数字人 {task_id}] 模式A I2V 第 {i+1} 段 (尝试 {attempt+1}/{max_retries})")
                try:
                    resp = requests.post(f'{base_url}/videos', headers=headers, json=payload, timeout=60)
                    if resp.status_code != 200:
                        print(f"[数字人 {task_id}] I2V API错误: {resp.status_code} {resp.text[:200]}")
                        if attempt < max_retries - 1:
                            # 429/503 需要更长等待时间
                            if resp.status_code in (429, 503):
                                wait_time = 60 * (attempt + 1)
                                print(f"[数字人 {task_id}] 速率限制/队列满，等待 {wait_time} 秒...")
                                time.sleep(wait_time)
                            else:
                                time.sleep(3)
                            continue
                        break
                    
                    result = resp.json()
                    task_api_id = result.get('id') or result.get('task_id') or result.get('video_id', '')
                    vid_id = result.get('video_id') or result.get('id', '')
                    if not task_api_id:
                        print(f"[数字人 {task_id}] I2V 未获取到任务ID")
                        break
                    
                    # 轮询等待视频完成
                    video_url = _poll_video(task_api_id, base_url, headers, task_id, i, video_model=video_model, video_id=vid_id)
                    if not video_url:
                        if attempt < max_retries - 1:
                            time.sleep(3)
                            continue
                        break
                    
                    # 下载视频
                    local_file = None
                    if video_url == '__VIDEO_STREAM__':
                        # agnes-video-2.5: 直接从 /agnesapi 下载视频流
                        local_file = download_video_by_video_id(
                            vid_id, base_url, headers,
                            os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}',
                            model_name=video_model
                        )
                    elif video_url and video_url.startswith('http'):
                        local_file = download_and_save_file(video_url, os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}', 'mp4')
                    elif result.get('video_id'):
                        local_file = download_video_by_video_id(
                            result['video_id'], base_url, headers,
                            os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}'
                        )
                    
                    if local_file:
                        full_path = os.path.join(app_dir, 'anchor', task_id, 'visuals', local_file)
                        with anchor_lock:
                            anchor_tasks[task_id]['segments'][i]['visual_path'] = full_path
                        print(f"[数字人 {task_id}] 模式A I2V 第 {i+1} 段画面完成")
                        i2v_success = True
                        break
                    else:
                        print(f"[数字人 {task_id}] I2V 视频下载失败")
                        
                except Exception as e:
                    print(f"[数字人 {task_id}] I2V 异常: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
        
        # 如果 I2V 失败，回退到 Ken Burns 效果
        if not i2v_success:
            print(f"[数字人 {task_id}] 模式A 第 {i+1} 段回退到 Ken Burns 效果")
            # Ken Burns 效果参数
            ken_burns_effects = [
                ('in', 'iw/2-(iw/zoom/2)', 'ih/2-(ih/zoom/2)'),
                ('out', 'iw/2-(iw/zoom/2)', 'ih/2-(ih/zoom/2)'),
                ('in', '0', 'ih/2-(ih/zoom/2)'),
                ('in', 'iw-iw/zoom', 'ih/2-(ih/zoom/2)'),
            ]
            effect_idx = i % len(ken_burns_effects)
            zoom_dir, x_expr, y_expr = ken_burns_effects[effect_idx]
            total_frames = int(duration * 24)
            if zoom_dir == 'in':
                zoom_expr = f'min(zoom+0.0015,1.2)'
            else:
                zoom_expr = f'if(eq(on,1),1.2,max(zoom-0.0015,1.0))'
            
            vf = (
                f'scale={vid_w}:{vid_h}:force_original_aspect_ratio=decrease,'
                f'pad={vid_w}:{vid_h}:(ow-iw)/2:(oh-ih)/2:black,'
                f'zoompan=z=\'{zoom_expr}\':x=\'{x_expr}\':y=\'{y_expr}\':'
                f'd={total_frames}:s={vid_w}x{vid_h}:fps=24'
            )
            cmd = [
                ffmpeg, '-y',
                '-loop', '1', '-i', image_path,
                '-t', str(duration),
                '-vf', vf,
                '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p',
                '-r', '24',
                visual_path
            ]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, stderr = proc.communicate(timeout=120)
                if proc.returncode == 0 and os.path.exists(visual_path) and os.path.getsize(visual_path) > 0:
                    print(f"[数字人 {task_id}] 模式A 第 {i+1} 段 Ken Burns 效果完成")
                else:
                    # 最终回退到静态图片
                    cmd_fallback = [
                        ffmpeg, '-y',
                        '-loop', '1', '-i', image_path,
                        '-t', str(duration),
                        '-vf', 'scale=1152:768:force_original_aspect_ratio=decrease,pad=1152:768:(ow-iw)/2:(oh-ih)/2:black',
                        '-c:v', 'libx264', '-tune', 'stillimage',
                        '-pix_fmt', 'yuv420p', '-r', '24',
                        visual_path
                    ]
                    proc2 = subprocess.Popen(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    proc2.communicate(timeout=60)
                    if proc2.returncode != 0:
                        _update_anchor(task_id, status='failed', message=f'第 {i+1} 段画面生成失败')
                        return False
            except Exception as e:
                print(f"[数字人 {task_id}] 模式A Ken Burns 异常: {e}")
                return False

    return True


def _mode_b_video_material(task_id, avatar_file, segments, visual_dir):
    """模式 B：视频素材 — 将视频按音频时长截取/循环"""
    app_dir = get_app_dir()
    video_path = os.path.join(app_dir, 'anchor', avatar_file)
    if not os.path.exists(video_path):
        _update_anchor(task_id, status='failed', message=f'视频素材不存在: {avatar_file}')
        return False

    source_duration = get_video_duration(video_path)
    if source_duration <= 0:
        _update_anchor(task_id, status='failed', message='无法读取视频素材时长')
        return False

    with anchor_lock:
        for i, seg in enumerate(anchor_tasks[task_id]['segments']):
            visual_path = os.path.join(visual_dir, f'seg_{i:03d}.mp4')
            seg['visual_path'] = visual_path

    ffmpeg = get_ffmpeg_path()
    for i, seg in enumerate(segments):
        if shutdown_event.is_set():
            return False
        duration = seg.get('duration', 5)
        if duration < 1:
            duration = 5
        visual_path = os.path.join(visual_dir, f'seg_{i:03d}.mp4')

        if duration <= source_duration:
            # 截取前 duration 秒
            cmd = [
                ffmpeg, '-y', '-i', video_path,
                '-t', str(duration),
                '-vf', 'scale=1152:768:force_original_aspect_ratio=decrease,pad=1152:768:(ow-iw)/2:(oh-ih)/2:black',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-an', '-r', '24',
                visual_path
            ]
        else:
            # 循环视频素材
            loops = int(duration / source_duration) + 1
            cmd = [
                ffmpeg, '-y',
                '-stream_loop', str(loops), '-i', video_path,
                '-t', str(duration),
                '-vf', 'scale=1152:768:force_original_aspect_ratio=decrease,pad=1152:768:(ow-iw)/2:(oh-ih)/2:black',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-an', '-r', '24',
                visual_path
            ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _, stderr = proc.communicate(timeout=120)
            if proc.returncode == 0 and os.path.exists(visual_path) and os.path.getsize(visual_path) > 0:
                print(f"[数字人 {task_id}] 模式B 第 {i+1} 段画面完成 ({duration:.1f}s)")
            else:
                err = stderr.decode('utf-8', errors='replace')[:300]
                print(f"[数字人 {task_id}] 模式B 第 {i+1} 段画面失败: {err}")
                return False
        except Exception as e:
            print(f"[数字人 {task_id}] 模式B 异常: {e}")
            return False

    return True


def _mode_c_ai_generate(task_id, video_prompt, segments, visual_dir, api_key, video_model=None):
    """模式 C：AI 生成画面 — 为每段调用 Agnes Video API 生成画面"""
    if not video_model:
        video_model = DEFAULT_VIDEO_MODEL
    base_url = get_vendor_base_url(video_model)
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    with anchor_lock:
        for i, seg in enumerate(anchor_tasks[task_id]['segments']):
            seg['visual_path'] = os.path.join(visual_dir, f'seg_{i:03d}.mp4')

    # 记录上次视频请求时间，用于速率限制控制（2 requests/min）
    _last_video_request_time = 0
    
    for i, seg in enumerate(segments):
        if shutdown_event.is_set():
            return False

        # 速率限制控制：确保两次请求间隔至少 35 秒
        elapsed = time.time() - _last_video_request_time
        if elapsed < 35 and _last_video_request_time > 0:
            wait_sec = 35 - elapsed
            print(f"[数字人 {task_id}] 模式C 速率限制保护：等待 {wait_sec:.0f} 秒...")
            _update_anchor(task_id, message=f'等待 API 限流恢复 ({wait_sec:.0f}s)...')
            time.sleep(wait_sec)
        _last_video_request_time = time.time()
        
        _update_anchor(task_id, message=f'AI 生成第 {i+1}/{len(segments)} 段画面...')
        seg_text = seg['text']
        duration = seg.get('duration', 5)
        if duration < 1:
            duration = 5

        # 构建视频提示词：结合用户风格提示词和段落内容
        prompt = f"{video_prompt}，{seg_text}" if video_prompt else seg_text
        # 限制帧数：8n+1 规则，按每秒24帧计算
        num_frames = min(int(duration * 24) // 8 * 8 + 1, 441)

        if video_model.startswith('agnes-video-2.5'):
            # Agnes Video 2.5 新参数格式：纯文生视频（text 模式）
            payload = {
                'model': video_model,
                'prompt': prompt[:1000],
                'mode': 'text',
                'seconds': str(max(4, min(12, round(duration)))),
                'size': '720P',
                'aspect_ratio': '16:9',
            }
        else:
            payload = {
                'model': video_model,
                'prompt': prompt[:1000],  # 限制提示词长度
                'width': 1152,
                'height': 768,
                'num_frames': num_frames,
                'frame_rate': 24,
            }

        # 重试机制：最多重试 3 次
        max_retries = 3
        segment_success = False
        
        for attempt in range(max_retries):
            if shutdown_event.is_set():
                return False
            
            print(f"[数字人 {task_id}] 模式C 第 {i+1} 段视频生成中... (尝试 {attempt+1}/{max_retries})")
            try:
                resp = requests.post(f'{base_url}/videos', headers=headers, json=payload, timeout=60)
                if resp.status_code != 200:
                    print(f"[数字人 {task_id}] 视频API错误: {resp.status_code} {resp.text[:200]}")
                    if attempt < max_retries - 1:
                        # 429/503 需要更长等待时间
                        if resp.status_code in (429, 503):
                            wait_time = 60 * (attempt + 1)
                            print(f"[数字人 {task_id}] 速率限制/队列满，等待 {wait_time} 秒...")
                            time.sleep(wait_time)
                        else:
                            time.sleep(3)
                        continue
                    else:
                        print(f"[数字人 {task_id}] 第 {i+1} 段视频生成失败（已重试 {max_retries} 次），继续执行下一步")
                        break

                result = resp.json()
                task_api_id = result.get('id') or result.get('task_id') or result.get('video_id', '')
                vid_id = result.get('video_id') or result.get('id', '')
                if not task_api_id:
                    print(f"[数字人 {task_id}] 未获取到任务ID: {json.dumps(result)[:200]}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    else:
                        print(f"[数字人 {task_id}] 第 {i+1} 段视频生成失败（已重试 {max_retries} 次），继续执行下一步")
                        break

                # 轮询等待视频完成
                video_url = _poll_video(task_api_id, base_url, headers, task_id, i, video_model=video_model, video_id=vid_id)
                if not video_url:
                    if attempt < max_retries - 1:
                        print(f"[数字人 {task_id}] 第 {i+1} 段视频轮询失败，重试...")
                        time.sleep(3)
                        continue
                    else:
                        print(f"[数字人 {task_id}] 第 {i+1} 段视频生成失败（已重试 {max_retries} 次），继续执行下一步")
                        break

                # 下载视频
                visual_path = os.path.join(visual_dir, f'seg_{i:03d}.mp4')
                local_file = None
                if video_url == '__VIDEO_STREAM__':
                    local_file = download_video_by_video_id(
                        vid_id, base_url, headers,
                        os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}',
                        model_name=video_model
                    )
                elif video_url and video_url.startswith('http'):
                    local_file = download_and_save_file(video_url, os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}', 'mp4')
                elif result.get('video_id'):
                    local_file = download_video_by_video_id(
                        result['video_id'], base_url, headers,
                        os.path.join('anchor', task_id, 'visuals'), f'seg_{i:03d}'
                    )

                if local_file:
                    app_dir = get_app_dir()
                    full_path = os.path.join(app_dir, 'anchor', task_id, 'visuals', local_file)
                    with anchor_lock:
                        anchor_tasks[task_id]['segments'][i]['visual_path'] = full_path
                    print(f"[数字人 {task_id}] 模式C 第 {i+1} 段画面完成")
                    segment_success = True
                    break
                else:
                    print(f"[数字人 {task_id}] 第 {i+1} 段视频下载失败")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    else:
                        print(f"[数字人 {task_id}] 第 {i+1} 段视频生成失败（已重试 {max_retries} 次），继续执行下一步")
                        break

            except Exception as e:
                print(f"[数字人 {task_id}] 模式C 第 {i+1} 段异常: {e}")
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                else:
                    print(f"[数字人 {task_id}] 第 {i+1} 段视频生成失败（已重试 {max_retries} 次），继续执行下一步")
                    break
        
        # 如果这段失败，记录日志但继续执行下一段
        if not segment_success:
            print(f"[数字人 {task_id}] 第 {i+1} 段视频最终失败，跳过继续")
            with anchor_lock:
                anchor_tasks[task_id]['segments'][i]['status'] = 'failed'

    return True


def _poll_video(task_api_id, base_url, headers, task_id, seg_idx, video_model=None, video_id=None):
    """轮询视频生成状态
    
    Args:
        task_api_id: 任务 ID
        video_model: 视频模型名称
        video_id: 视频 ID（用于 agnes-video-2.5 等新 API）
    """
    max_polls = 60
    # 判断是否使用 /agnesapi 查询端点
    use_agnesapi = video_model and video_model.startswith('agnes-video-2.5') and video_id
    
    for poll in range(max_polls):
        if shutdown_event.is_set():
            return None
        time.sleep(5)
        try:
            if use_agnesapi:
                resp = requests.get(
                    f'{base_url}/agnesapi',
                    headers=headers,
                    params={'video_id': video_id, 'model_name': video_model},
                    timeout=30
                )
            else:
                resp = requests.get(f'{base_url}/videos/{task_api_id}', headers=headers, timeout=30)
            
            if resp.status_code == 200:
                # 检查是否是直接的视频流（agnes-video-2.5 完成时可能直接返回视频）
                content_type = resp.headers.get('Content-Type', '')
                if 'video' in content_type or 'octet-stream' in content_type:
                    print(f"[数字人 {task_id}] 第 {seg_idx+1} 段 /agnesapi 返回视频流")
                    return '__VIDEO_STREAM__'  # 特殊标记，表示需要直接从 /agnesapi 下载
                
                data = resp.json()
                status = data.get('status', '')
                if status == 'completed':
                    video_url = (
                        data.get('video_url') or data.get('url') or
                        data.get('output_url') or data.get('video') or ''
                    )
                    if not video_url and isinstance(data.get('data'), dict):
                        video_url = data['data'].get('url', '') or data['data'].get('video_url', '')
                    if not video_url and data.get('video_id'):
                        return data.get('video_id')
                    return video_url
                elif status == 'failed':
                    print(f"[数字人 {task_id}] 第 {seg_idx+1} 段视频生成失败")
                    return None
        except Exception as e:
            print(f"[数字人 {task_id}] 轮询异常: {e}")
            continue

    print(f"[数字人 {task_id}] 第 {seg_idx+1} 段视频生成超时")
    return None


# ==================== Step 4: 逐段合成 ====================

def _compose_segment_video(ffmpeg, visual_path, audio_path, subtitle_text, duration, output_path, mode, video_width=1152, video_height=768):
    """将画面+音频+字幕合成为一段完整视频"""
    if not ffmpeg:
        print("[数字人] ffmpeg 不可用")
        return False

    try:
        # 构建 ffmpeg 命令：画面 + 音频 + 字幕
        font_path = _find_chinese_font()
        subtitle_filter = ''
        if subtitle_text and font_path:
            escaped = subtitle_text.replace("'", "'\\''").replace(':', '\\:').replace('\n', ' ')
            font_escaped = font_path.replace('\\', '/').replace(':', '\\:')
            subtitle_filter = (
                f",drawtext=text='{escaped}'"
                f":fontfile='{font_escaped}'"
                f":fontsize=32:fontcolor=white"
                f":borderw=2:bordercolor=black"
                f":x=(w-text_w)/2:y=h-th-40"
            )
        elif subtitle_text:
            escaped = subtitle_text.replace("'", "'\\''").replace(':', '\\:').replace('\n', ' ')
            subtitle_filter = (
                f",drawtext=text='{escaped}'"
                f":fontsize=32:fontcolor=white"
                f":borderw=2:bordercolor=black"
                f":x=(w-text_w)/2:y=h-th-40"
            )

        vf = f'scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2:black{subtitle_filter}'

        cmd = [
            ffmpeg, '-y',
            '-i', visual_path,
            '-i', audio_path,
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            '-shortest',
            '-r', '24',
            '-pix_fmt', 'yuv420p',
            output_path
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = proc.communicate(timeout=120)

        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        else:
            err = stderr.decode('utf-8', errors='replace')[:300]
            print(f"[数字人] 合成失败: {err}")
            return False
    except Exception as e:
        print(f"[数字人] 合成异常: {e}")
        return False


# ==================== Step 5: 最终拼接 ====================

def _merge_final_video(ffmpeg, segment_videos, task_dir, task_id):
    """将所有段视频拼接为最终视频"""
    if not ffmpeg or len(segment_videos) == 0:
        return None

    if len(segment_videos) == 1:
        # 只有一段，直接复制
        final_path = os.path.join(task_dir, f'anchor_output_{task_id}.mp4')
        try:
            import shutil
            shutil.copy2(segment_videos[0], final_path)
            return final_path
        except Exception:
            return segment_videos[0]

    # 创建 concat 列表文件
    list_file = os.path.join(task_dir, 'concat_list.txt')
    try:
        with open(list_file, 'w', encoding='utf-8') as f:
            for vp in segment_videos:
                safe_path = vp.replace('\\', '/')
                f.write(f"file '{safe_path}'\n")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = f'anchor_output_{timestamp}.mp4'
        output_path = os.path.join(task_dir, output_file)

        # 先尝试 copy 模式
        cmd = [ffmpeg, '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', '-y', output_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = proc.communicate(timeout=120)

        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print(f"[数字人 {task_id}] 拼接成功: {output_file}")
            return output_path

        # copy 失败，重新编码
        print(f"[数字人 {task_id}] copy 拼接失败，尝试重新编码...")
        cmd2 = [ffmpeg, '-f', 'concat', '-safe', '0', '-i', list_file,
                '-c:v', 'libx264', '-c:a', 'aac', '-y', output_path]
        proc2 = subprocess.Popen(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr2 = proc2.communicate(timeout=300)

        if proc2.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print(f"[数字人 {task_id}] 重新编码拼接成功: {output_file}")
            return output_path

        print(f"[数字人 {task_id}] 拼接最终失败")
        return None
    except Exception as e:
        print(f"[数字人 {task_id}] 拼接异常: {e}")
        return None
    finally:
        if os.path.exists(list_file):
            try:
                os.remove(list_file)
            except:
                pass
