"""
视频生成路由
"""

import time
import threading
import requests
from flask import Blueprint, request, jsonify

from ..config import get_vendor_api_key, get_vendor_base_url, get_vendor_from_model, resolve_image_url
from ..models import video_tasks, task_lock, DEFAULT_VIDEO_MODEL
from ..services.video_gen import poll_video_status

video_bp = Blueprint('video', __name__)


def _minimax_api_root(base_url):
    """从 base_url 提取 MiniMax API 根地址（去掉 /v1 后缀）"""
    root = base_url.rstrip('/')
    if root.endswith('/v1'):
        root = root[:-3]
    return root


def _closest_ratio(width, height):
    """计算最接近的支持宽高比（MiniMax-H3 支持：21:9, 16:9, 4:3, 1:1, 3:4, 9:16）"""
    candidates = {'21:9': 21/9, '16:9': 16/9, '4:3': 4/3, '1:1': 1.0, '3:4': 3/4, '9:16': 9/16}
    target = width / height if height else 1.0
    return min(candidates, key=lambda k: abs(candidates[k] - target))


def _agnes25_aspect_ratio(width, height):
    """计算最接近的支持宽高比（Agnes Video 2.5 支持：16:9, 4:3, 1:1, 3:4, 9:16）"""
    candidates = {'16:9': 16/9, '4:3': 4/3, '1:1': 1.0, '3:4': 3/4, '9:16': 9/16}
    target = width / height if height else 1.0
    return min(candidates, key=lambda k: abs(candidates[k] - target))


def _build_agnes25_payload(model, prompt, width, height, num_frames, frame_rate, resolved_urls, seed):
    """构建 Agnes Video 2.5/2.5 Flash 的请求体（新参数格式）

    注意：该模型禁止传 width/height/num_frames/frame_rate/negative_prompt/image，
    只支持 mode/seconds/size/aspect_ratio/first_frame/last_frame/images 等字段。
    """
    # 时长：由帧数/帧率计算，限制在 4~12 秒（官方支持范围）
    try:
        seconds = round(num_frames / frame_rate) if frame_rate else 5
    except (TypeError, ZeroDivisionError):
        seconds = 5
    seconds = max(4, min(12, seconds))

    payload = {
        'model': model,
        'prompt': prompt,
        'seconds': str(seconds),
        'size': '720P',
    }

    if resolved_urls:
        # 图生视频：使用 keyframe 首尾帧模式（最多取首帧 + 尾帧 2 张）
        payload['mode'] = 'keyframe'
        payload['first_frame'] = resolved_urls[0]
        if len(resolved_urls) >= 2:
            payload['last_frame'] = resolved_urls[-1]
    else:
        # 纯文生视频模式，必须指定宽高比（不支持自定义像素）
        payload['mode'] = 'text'
        payload['aspect_ratio'] = _agnes25_aspect_ratio(width, height)

    if seed is not None:
        payload['seed'] = seed
    return payload


def _submit_minimax_video(model, api_key, base_url, prompt, width, height, num_frames, frame_rate, resolved_urls):
    """通过 MiniMax V2 接口提交视频生成任务（MiniMax-H3）

    Returns:
        (task_id, error_response) —— 成功时 error_response 为 None
    """
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    api_root = _minimax_api_root(base_url)

    content = [{'type': 'text', 'text': prompt}]
    if resolved_urls:
        # 图生视频：首帧 + 可选尾帧（首尾帧需成对，最多 2 张）
        content.append({
            'type': 'image_url',
            'image_url': {'url': resolved_urls[0]},
            'role': 'first_frame'
        })
        if len(resolved_urls) >= 2:
            content.append({
                'type': 'image_url',
                'image_url': {'url': resolved_urls[-1]},
                'role': 'last_frame'
            })

    # 时长：由帧数/帧率计算，限制在 4~15 秒（MiniMax-H3 支持范围）
    try:
        duration = round(num_frames / frame_rate) if frame_rate else 5
    except (TypeError, ZeroDivisionError):
        duration = 5
    duration = max(4, min(15, duration))

    payload = {
        'model': model,
        'content': content,
        'resolution': '768P',
        'duration': duration,
        # 文生视频必须指定比例；图生视频自适应输入图片比例
        'ratio': 'adaptive' if resolved_urls else _closest_ratio(width, height)
    }

    print(f"[视频生成] MiniMax V2 提交: model={model}, duration={duration}s, ratio={payload['ratio']}, 图片数={len(resolved_urls)}")
    resp = requests.post(
        f'{api_root}/v2/video_generation',
        headers=headers,
        json=payload,
        timeout=60
    )

    if resp.status_code == 429:
        return None, (jsonify({
            'success': False,
            'error': 'MiniMax 视频生成 API 触发速率限制，请等待 1-2 分钟后再次点击生成。',
            'retry_after': 60
        }), 429)

    if resp.status_code == 402:
        return None, (jsonify({
            'success': False,
            'error': 'MiniMax 账户余额不足，请前往 MiniMax 开放平台充值后重试。'
        }), 402)

    if resp.status_code != 200:
        return None, (jsonify({
            'success': False,
            'error': f'MiniMax API 错误 ({resp.status_code}): {resp.text[:500]}'
        }), resp.status_code)

    result = resp.json()
    task_id = result.get('task_id', '')
    if not task_id:
        return None, (jsonify({
            'success': False,
            'error': f'MiniMax API 响应中未找到任务 ID: {str(result)[:200]}'
        }), 500)
    return task_id, None


@video_bp.route('/api/video/generate', methods=['POST'])
def generate_video():
    """提交视频生成任务"""
    data = request.get_json()
    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'success': False, 'error': '请输入视频描述'}), 400

    width = data.get('width', 1152)
    height = data.get('height', 768)
    num_frames = data.get('num_frames', 121)
    frame_rate = data.get('frame_rate', 24)
    # 支持多张参考图
    image_urls = data.get('image_urls', [])
    if not image_urls:
        single_url = data.get('image_url', '')
        if single_url:
            image_urls = [single_url]
    model = data.get('model', DEFAULT_VIDEO_MODEL)
    # 转换本地图片为 base64
    resolved_urls = []
    for u in image_urls:
        resolved = resolve_image_url(u.strip()) if isinstance(u, str) else u
        if resolved:
            resolved_urls.append(resolved)
    negative_prompt = data.get('negative_prompt', '')
    seed = data.get('seed')
    
    api_key = get_vendor_api_key(model)
    if not api_key:
        return jsonify({'success': False, 'error': '请先配置 API Key'}), 401
    base_url = get_vendor_base_url(model)

    try:
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        # MiniMax 视频生成（V2 接口，MiniMax-H3 模型）
        if get_vendor_from_model(model) == 'minimax':
            task_id, err = _submit_minimax_video(
                model, api_key, base_url, prompt,
                width, height, num_frames, frame_rate, resolved_urls
            )
            if err is not None:
                return err
            video_id = ''
        elif model.startswith('agnes-video-2.5'):
            # Agnes Video 2.5 / 2.5 Flash：新参数格式（禁止 width/height/num_frames 等字段）
            payload = _build_agnes25_payload(
                model, prompt, width, height, num_frames, frame_rate, resolved_urls, seed
            )
            print(f"[视频生成] Agnes Video 2.5 提交: mode={payload['mode']}, seconds={payload['seconds']}, size={payload['size']}")
            resp = requests.post(
                f'{base_url}/videos',
                headers=headers,
                json=payload,
                timeout=60
            )

            if resp.status_code == 429:
                print(f"[视频生成] API 触发速率限制 (429)，请用户稍后重试")
                return jsonify({
                    'success': False,
                    'error': '视频生成 API 触发速率限制（每分钟最多 2 个请求）。请等待 1-2 分钟后再次点击生成。',
                    'retry_after': 60
                }), 429

            if resp.status_code not in (200, 201):
                return jsonify({
                    'success': False,
                    'error': f'API 错误 ({resp.status_code}): {resp.text[:500]}'
                }), resp.status_code

            result = resp.json()
            task_id = result.get('id') or result.get('task_id') or result.get('video_id', '')
            video_id = result.get('video_id', '')

            if not task_id:
                return jsonify({
                    'success': False,
                    'error': f'API 响应中未找到任务 ID: {str(result)[:200]}'
                }), 500
        else:
            payload = {
                'model': model,
                'prompt': prompt,
                'width': width,
                'height': height,
                'num_frames': num_frames,
                'frame_rate': frame_rate
            }

            if resolved_urls:
                # 多张参考图：传递数组；单张则传字符串
                if len(resolved_urls) == 1:
                    payload['image'] = resolved_urls[0]
                else:
                    payload['image'] = resolved_urls
            if negative_prompt:
                payload['negative_prompt'] = negative_prompt
            if seed is not None:
                payload['seed'] = seed

            # 发送请求，处理 429 速率限制
            resp = requests.post(
                f'{base_url}/videos',
                headers=headers,
                json=payload,
                timeout=60
            )

            if resp.status_code == 429:
                print(f"[视频生成] API 触发速率限制 (429)，请用户稍后重试")
                return jsonify({
                    'success': False,
                    'error': '视频生成 API 触发速率限制（每分钟最多 2 个请求）。请等待 1-2 分钟后再次点击生成。',
                    'retry_after': 60
                }), 429

            if resp.status_code == 503:
                print(f"[视频生成] API 服务不可用 (503)，请用户稍后重试")
                return jsonify({
                    'success': False,
                    'error': '视频生成 API 服务暂时不可用，请稍后重试。',
                    'retry_after': 30
                }), 503

            if resp.status_code != 200:
                return jsonify({
                    'success': False,
                    'error': f'API 错误 ({resp.status_code}): {resp.text[:500]}'
                }), resp.status_code

            # status_code == 200，处理成功
            result = resp.json()
            # 兼容 base_resp 风格的错误响应（如旧接口返回 200 + 错误体）
            base_resp = result.get('base_resp')
            if isinstance(base_resp, dict) and base_resp.get('status_code') not in (0, None):
                return jsonify({
                    'success': False,
                    'error': f'API 错误 ({base_resp.get("status_code")}): {base_resp.get("status_msg", "")}'
                }), 500
            # 兼容不同的响应格式
            task_id = result.get('task_id') or result.get('id') or result.get('video_id', '')
            video_id = result.get('video_id') or result.get('id', '')

            if not task_id:
                return jsonify({
                    'success': False,
                    'error': f'API 响应中未找到任务 ID: {str(result)[:200]}'
                }), 500

        with task_lock:
            video_tasks[task_id] = {
                'task_id': task_id,
                'video_id': video_id,
                'status': 'queued',
                'prompt': prompt,
                'created_at': time.time(),
                'result': None
            }

        thread = threading.Thread(
            target=poll_video_status,
            args=(task_id, api_key, model, video_id),
            daemon=True
        )
        thread.start()

        return jsonify({
            'success': True,
            'task_id': task_id,
            'video_id': video_id,
            'status': 'queued',
            'message': '视频任务已提交，正在排队处理...'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@video_bp.route('/api/video/status/<task_id>', methods=['GET'])
def get_video_status(task_id):
    """查询视频任务状态"""
    with task_lock:
        task = video_tasks.get(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        response = {
            'success': True,
            'task_id': task['task_id'],
            'status': task['status'],
            'prompt': task['prompt'],
            'created_at': task['created_at']
        }

        if task['status'] == 'completed':
            response['video_url'] = task['result'].get('video_url', '')
            response['local_file'] = task['result'].get('local_file', '')
            response['raw_response'] = task['result'].get('raw_response', {})
        elif task['status'] == 'failed':
            response['error'] = task['result'].get('error', '生成失败')

        return jsonify(response)


@video_bp.route('/api/video/tasks', methods=['GET'])
def list_video_tasks():
    """列出所有视频任务"""
    with task_lock:
        tasks = []
        for task_id, task in video_tasks.items():
            t = {
                'task_id': task['task_id'],
                'status': task['status'],
                'prompt': task['prompt'][:50] + '...' if len(task['prompt']) > 50 else task['prompt'],
                'created_at': task['created_at']
            }
            if task['status'] == 'completed' and task.get('result'):
                t['video_url'] = task['result'].get('video_url', '')
                t['local_file'] = task['result'].get('local_file', '')
            tasks.append(t)
        return jsonify({'success': True, 'tasks': tasks})
