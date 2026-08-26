"""
视频生成服务模块
包含：文件下载、视频轮询
"""

import os
import json
import time
import uuid
import requests
from datetime import datetime

from ..config import get_app_dir, get_vendor_base_url, BASE_URL, shutdown_event
from ..models import video_tasks, task_lock


def download_video_by_video_id(video_id, base_url, headers, subdir, prefix, model_name=None):
    """通过 video_id 使用 /agnesapi 端点下载视频
    
    Args:
        video_id: 视频 ID
        base_url: API Base URL
        headers: 请求头
        subdir: 保存子目录
        prefix: 文件名前缀
        model_name: 模型名称（可选，用于 agnes-video-2.5 等新 API）
    
    Returns:
        保存后的文件名，失败返回 None
    """
    try:
        print(f"[视频下载] 通过 video_id 获取视频: {video_id[:50]}...")
        # 构建请求参数
        params = {'video_id': video_id}
        if model_name:
            params['model_name'] = model_name
        agnesapi_url = f'{base_url}/agnesapi'
        resp = requests.get(agnesapi_url, headers=headers, params=params, timeout=(30, 120), stream=True)
        
        if resp.status_code == 200:
            content_type = resp.headers.get('Content-Type', '')
            # 如果返回的是直接的视频流
            if 'video' in content_type or 'octet-stream' in content_type:
                return _save_stream_to_file(resp, subdir, prefix)
            # 如果返回的是 JSON（包含下载 URL）
            try:
                data = resp.json()
                url = (data.get('url', '') or data.get('video_url', '') or 
                       data.get('video', '') or data.get('output_url', ''))
                if isinstance(data.get('data'), dict):
                    url = url or data['data'].get('url', '') or data['data'].get('video_url', '')
                if url:
                    print(f"[视频下载] agnesapi 返回 URL: {url[:150]}...")
                    return download_and_save_file(url, subdir, prefix, 'mp4')
                # 可能 data 本身就是视频数据（base64）
                if data.get('data') and isinstance(data['data'], str) and len(data['data']) > 1000:
                    import base64
                    app_dir = get_app_dir()
                    target_dir = os.path.join(app_dir, subdir)
                    os.makedirs(target_dir, exist_ok=True)
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    short_uuid = uuid.uuid4().hex[:8]
                    filename = f"{prefix}_{timestamp}_{short_uuid}.mp4"
                    filepath = os.path.join(target_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(base64.b64decode(data['data']))
                    file_size = os.path.getsize(filepath)
                    if file_size > 1000:
                        print(f"[保存成功] {subdir}/{filename} ({file_size // 1024}KB)")
                        return filename
            except (json.JSONDecodeError, ValueError):
                pass
        else:
            print(f"[视频下载] agnesapi 端点返回 {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[视频下载] agnesapi 端点异常: {type(e).__name__}: {e}")
    return None


def _save_stream_to_file(resp, subdir, prefix):
    """将流式响应保存为文件"""
    app_dir = get_app_dir()
    target_dir = os.path.join(app_dir, subdir)
    os.makedirs(target_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    short_uuid = uuid.uuid4().hex[:8]
    filename = f"{prefix}_{timestamp}_{short_uuid}.mp4"
    filepath = os.path.join(target_dir, filename)
    with open(filepath, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    file_size = os.path.getsize(filepath)
    if file_size > 1000:
        print(f"[保存成功] {subdir}/{filename} ({file_size // 1024}KB)")
        return filename
    else:
        os.remove(filepath)
        return None


def download_and_save_file(url, subdir, prefix, ext, max_retries=3):
    """从 URL 下载文件并保存到本地目录（支持重试）
    
    Args:
        url: 文件下载 URL
        subdir: 子目录名 ('videos' 或 'pictures')
        prefix: 文件名前缀
        ext: 文件扩展名
        max_retries: 最大重试次数
    
    Returns:
        保存后的文件名，失败返回 None
    """
    app_dir = get_app_dir()
    target_dir = os.path.join(app_dir, subdir)
    os.makedirs(target_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    short_uuid = uuid.uuid4().hex[:8]
    filename = f"{prefix}_{timestamp}_{short_uuid}.{ext}"
    filepath = os.path.join(target_dir, filename)

    # 总下载超时（秒）
    total_timeout = 300 if ext == 'mp4' else 120

    for attempt in range(max_retries):
        try:
            print(f"[下载] {subdir}/{filename} (尝试 {attempt+1}/{max_retries}) url={url[:100]}...")
            resp = requests.get(url, timeout=(30, 60), stream=True)  # 连接超时30s，读取超时60s
            resp.raise_for_status()

            content_type = resp.headers.get('Content-Type', '')
            if ext == 'mp4' and 'video' not in content_type and 'octet-stream' not in content_type and 'mp4' not in content_type:
                print(f"[下载警告] Content-Type 不是视频: {content_type}")

            total_size = int(resp.headers.get('Content-Length', 0))
            if total_size > 0:
                print(f"[下载] 预期文件大小: {total_size // 1024}KB")
            
            downloaded = 0
            start_time = time.time()
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if shutdown_event and shutdown_event.is_set():
                        print(f"[下载] 收到关闭信号，停止下载")
                        return None
                    # 检查总超时
                    if time.time() - start_time > total_timeout:
                        print(f"[下载] 总下载超时({total_timeout}s)，已下载 {downloaded // 1024}KB")
                        raise requests.exceptions.Timeout(f"下载总超时({total_timeout}s)")
                    f.write(chunk)
                    downloaded += len(chunk)

            file_size = os.path.getsize(filepath)
            if file_size < 1000:
                print(f"[下载警告] 文件太小 ({file_size} bytes)，可能是错误响应")
                if attempt < max_retries - 1:
                    os.remove(filepath)
                    time.sleep(2)
                    continue
                else:
                    os.remove(filepath)
                    return None

            elapsed = time.time() - start_time
            print(f"[保存成功] {subdir}/{filename} ({file_size // 1024}KB, {elapsed:.1f}s)")
            return filename
        except Exception as e:
            print(f"[下载失败] {subdir}/{filename} 尝试{attempt+1}: {type(e).__name__}: {e}")
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except:
                    pass
            if attempt < max_retries - 1:
                time.sleep(3)
    return None


def poll_video_status(task_id, api_key, model=None, video_id=None):
    """后台轮询视频生成状态
    
    Args:
        task_id: 任务 ID（从 POST /videos 返回）
        api_key: API Key
        model: 模型名称
        video_id: 视频 ID（用于 agnes-video-2.5 等新 API）
    """
    headers = {'Authorization': f'Bearer {api_key}'}
    base_url = get_vendor_base_url(model) if model else BASE_URL
    max_polls = 120
    poll_interval = 10
    
    # 判断是否使用新的 /agnesapi 查询端点（agnes-video-2.5 / agnes-video-2.5-flash）
    use_agnesapi_poll = model and model.startswith('agnes-video-2.5') and video_id
    # MiniMax 视频生成：使用 V2 查询接口 GET /v2/query/video_generation/{task_id}
    use_minimax_poll = bool(model) and model.lower().startswith('minimax')
    minimax_api_root = base_url.rstrip('/')
    if minimax_api_root.endswith('/v1'):
        minimax_api_root = minimax_api_root[:-3]

    for i in range(max_polls):
        if shutdown_event.wait(timeout=poll_interval):
            print(f"[轮询] 收到关闭信号，退出轮询 task_id={task_id}")
            return
        try:
            # MiniMax V2 视频生成查询（MiniMax-H3）
            if use_minimax_poll:
                resp = requests.get(
                    f'{minimax_api_root}/v2/query/video_generation/{task_id}',
                    headers=headers,
                    timeout=30
                )
                if resp.status_code == 200:
                    q_result = resp.json()
                    task_obj = q_result.get('task', {}) or {}
                    mm_status = task_obj.get('status', '')
                    status = {
                        'succeeded': 'completed',
                        'failed': 'failed',
                        'cancelled': 'failed',
                        'running': 'in_progress'
                    }.get(mm_status, 'queued')

                    with task_lock:
                        if task_id in video_tasks:
                            video_tasks[task_id]['status'] = status

                    if status == 'completed':
                        video_url = (task_obj.get('content') or {}).get('url', '')
                        print(f"[视频URL] MiniMax task_id={task_id}: {video_url[:150] if video_url else '(未获取到)'}")
                        local_filename = None
                        if video_url:
                            local_filename = download_and_save_file(video_url, 'videos', 'video', 'mp4')
                        with task_lock:
                            if task_id in video_tasks:
                                video_tasks[task_id]['result'] = {
                                    'video_url': video_url,
                                    'local_file': local_filename,
                                    'raw_response': q_result
                                }
                        return
                    elif status == 'failed':
                        err_msg = task_obj.get('error') or task_obj.get('fail_reason') or '生成失败'
                        with task_lock:
                            if task_id in video_tasks:
                                video_tasks[task_id]['result'] = {'error': str(err_msg)}
                        return
                elif resp.status_code == 429:
                    # 查询限流，等待后重试即可，不算失败
                    continue
                else:
                    print(f"[轮询] MiniMax 查询接口返回 {resp.status_code}: {resp.text[:200]}")
                continue

            # agnes-video-2.5: 使用 /agnesapi?video_id=<VIDEO_ID>&model_name=agnes-video-2.5 查询
            if use_agnesapi_poll:
                resp = requests.get(
                    f'{base_url}/agnesapi',
                    headers=headers,
                    params={'video_id': video_id, 'model_name': model},
                    timeout=30
                )
            else:
                # 传统方式：GET /videos/{task_id}
                resp = requests.get(
                    f'{base_url}/videos/{task_id}',
                    headers=headers,
                    timeout=30
                )

            if resp.status_code == 200:
                result = resp.json()
                status = result.get('status', '')

                with task_lock:
                    if task_id in video_tasks:
                        video_tasks[task_id]['status'] = status

                        if status == 'completed':
                            raw_result = json.dumps(result)
                            print(f"[视频完成] task_id={task_id}, 原始响应: {raw_result}")

                            video_url = (
                                result.get('video_url')
                                or result.get('url')
                                or result.get('output_url')
                                or result.get('video')
                                or result.get('remixed_from_video_id')
                                or ''
                            )
                            if not video_url and isinstance(result.get('data'), dict):
                                video_url = result['data'].get('url', '') or result['data'].get('video_url', '') or result['data'].get('video', '')
                            if not video_url and isinstance(result.get('data'), list) and len(result['data']) > 0:
                                video_url = result['data'][0].get('url', '') or result['data'][0].get('video_url', '')
                            if not video_url and isinstance(result.get('metadata'), dict):
                                meta = result['metadata']
                                video_url = meta.get('video_url', '') or meta.get('url', '') or meta.get('output_url', '')
                            if not video_url:
                                try:
                                    content_resp = requests.get(f'{base_url}/videos/{task_id}/content', headers=headers, timeout=30)
                                    if content_resp.status_code == 200:
                                        content_data = content_resp.json()
                                        video_url = content_data.get('url', '') or content_data.get('video_url', '') or content_data.get('video', '')
                                        if not video_url and isinstance(content_data.get('data'), dict):
                                            video_url = content_data['data'].get('url', '') or content_data['data'].get('video_url', '')
                                        print(f"[视频URL] 通过 content 端点获取: {video_url[:150] if video_url else '(无)'}")
                                except Exception as e2:
                                    print(f"[视频URL] content 端点请求失败: {e2}")

                            print(f"[视频URL] {video_url or '(未获取到)'}")

                            local_filename = None
                            if video_url:
                                local_filename = download_and_save_file(
                                    video_url, 'videos', 'video', 'mp4'
                                )
                            elif use_agnesapi_poll:
                                # agnes-video-2.5: /agnesapi 直接返回视频流
                                print(f"[视频下载] agnes-video-2.5 通过 /agnesapi 下载视频: {video_id[:50]}...")
                                local_filename = download_video_by_video_id(
                                    video_id, base_url, headers, 'videos', 'video', model_name=model
                                )
                            elif result.get('video_id'):
                                # 其他模型的新 API 格式：通过 video_id 下载
                                vid_id = result['video_id']
                                print(f"[视频下载] 使用 video_id: {vid_id[:50]}...")
                                local_filename = download_video_by_video_id(
                                    vid_id, base_url, headers, 'videos', 'video'
                                )

                            video_tasks[task_id]['result'] = {
                                'video_url': video_url,
                                'local_file': local_filename,
                                'raw_response': result
                            }
                            break
                        elif status == 'failed':
                            video_tasks[task_id]['result'] = {
                                'error': result.get('error', '生成失败')
                            }
                            break
        except Exception as e:
            if shutdown_event.is_set():
                return
            continue
