"""
模型选项与任务状态模块
包含：模型选项字典、默认模型、视频/短剧任务状态管理
"""

import os
import threading
from .config import get_app_dir

# ---------- 模型选项 ----------
TEXT_MODEL_OPTIONS = {
    'agnes-2.5-flash': 'Agnes 2.5 Flash (推荐，免费)',
    'MiniMax-M3': 'MiniMax M3',
    'agnes-2.5-pro-alpha': 'Agnes 2.5 Pro Alpha (高级)',
    'agnes-2.0-flash': 'Agnes 2.0 Flash',
    'deepseek-v4-flash': 'DeepSeek V4 Flash',
    'deepseek-chat': 'DeepSeek Chat',
    'deepseek-reasoner': 'DeepSeek Reasoner',
    'qwen-turbo': 'Qwen Turbo',
    'qwen-plus': 'Qwen Plus',
    'doubao-pro-32k': '豆包 Pro 32K',
    'doubao-lite-32k': '豆包 Lite 32K',
    # Ollama 本地模型（常用）
    'qwen2.5:7b': 'Ollama Qwen2.5 7B (本地)',
    'qwen2.5:14b': 'Ollama Qwen2.5 14B (本地)',
    'llama3.1:8b': 'Ollama Llama3.1 8B (本地)',
    'mistral:7b': 'Ollama Mistral 7B (本地)',
}
IMAGE_MODEL_OPTIONS = {
    'agnes-image-2.1-flash': 'Agnes Image 2.1 Flash (推荐)',
    'agnes-image-2.0-flash': 'Agnes Image 2.0 Flash',
    'doubao-seedream-3-0': '豆包 Seedream 3.0',
    'minimax-image-01': 'MiniMax Image 01',
    'qwen-image-plus': 'Qwen Image Plus',
}
VIDEO_MODEL_OPTIONS = {
    'agnes-video-2.5-flash': 'Agnes Video 2.5 Flash (推荐)',
    'agnes-video-2.5': 'Agnes Video 2.5',
    'MiniMax-H3': 'MiniMax H3 (MiniMax 视频生成)',
    'agnes-video-v2.0': 'Agnes Video 2.0',
    'minimax-video-01': 'MiniMax Video 01',
    'doubao-seaweed-t2v': '豆包 Seaweed T2V',
    'qwen-video-gen': 'Qwen Video Gen',
}
DEFAULT_TEXT_MODEL = 'agnes-2.5-flash'
DEFAULT_IMAGE_MODEL = 'agnes-image-2.1-flash'
DEFAULT_VIDEO_MODEL = 'agnes-video-2.5-flash'

# ---------- 视频任务状态（内存存储，重启后丢失）----------
video_tasks = {}
task_lock = threading.Lock()

# ---------- 短剧任务状态 ----------
drama_tasks = {}
drama_lock = threading.Lock()

def ensure_drama_dirs(drama_id):
    """确保短剧输出目录存在"""
    app_dir = get_app_dir()
    base = os.path.join(app_dir, 'dramas', drama_id)
    for sub in ('images', 'videos'):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    return base
