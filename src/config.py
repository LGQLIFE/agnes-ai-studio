"""
配置与路径工具模块
包含：PyInstaller 路径处理、目录管理、API Key 读写、厂商路由
"""

import os
import sys
import json
import threading

# ==================== PyInstaller 路径处理 ====================

def get_base_path():
    """获取程序基础路径（兼容 PyInstaller 打包和开发模式）"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_config_path():
    """获取配置文件路径（保存在 exe 同级目录，而非临时目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'config.json')
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json')

# ==================== 路径工具 ====================

def get_app_dir():
    """获取应用程序所在目录（用于保存输出文件）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def ensure_output_dirs():
    """确保输出目录存在"""
    app_dir = get_app_dir()
    videos_dir = os.path.join(app_dir, 'videos')
    pictures_dir = os.path.join(app_dir, 'pictures')
    os.makedirs(videos_dir, exist_ok=True)
    os.makedirs(pictures_dir, exist_ok=True)
    return videos_dir, pictures_dir

def resolve_image_url(image_url):
    """
    处理图片 URL：如果是本地 /pictures/ 路径，转为 base64 data URL；
    否则直接返回原 URL。
    """
    import base64

    if not image_url:
        return image_url

    # 检查是否是本地 pictures 路径
    if image_url.startswith('/pictures/') or image_url.startswith('pictures/'):
        app_dir = get_app_dir()
        filename = image_url.replace('/pictures/', '').replace('pictures/', '')
        filepath = os.path.join(app_dir, 'pictures', filename)

        if os.path.exists(filepath):
            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'
            }
            mime = mime_map.get(ext, 'image/png')
            with open(filepath, 'rb') as f:
                b64_data = base64.b64encode(f.read()).decode('utf-8')
            return f'data:{mime};base64,{b64_data}'
        else:
            print(f"[警告] 本地图片不存在: {filepath}")
            return image_url

    # 也处理完整的本地 URL（如 http://localhost:xxxx/pictures/xxx）
    if '/pictures/' in image_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(image_url)
            path = parsed.path
            if path.startswith('/pictures/'):
                filename = path.replace('/pictures/', '')
                app_dir = get_app_dir()
                filepath = os.path.join(app_dir, 'pictures', filename)
                if os.path.exists(filepath):
                    ext = os.path.splitext(filename)[1].lower()
                    mime_map = {
                        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'
                    }
                    mime = mime_map.get(ext, 'image/png')
                    with open(filepath, 'rb') as f:
                        b64_data = base64.b64encode(f.read()).decode('utf-8')
                    return f'data:{mime};base64,{b64_data}'
        except Exception:
            pass

    return image_url

# ==================== 常量配置 ====================

BASE_URL = "https://api.agnes-ai.cn/v1"

# 全局关闭事件：用于通知所有后台线程退出
shutdown_event = threading.Event()

# ---------- 厂商 Base URL 映射（通用，文本/图片/视频共用）----------
VENDOR_BASE_URLS = {
    'agnes': 'https://api.agnes-ai.cn/v1',
    'deepseek': 'https://api.deepseek.com/v1',
    'gpt': 'https://api.openai.com/v1',
    'qwen': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'doubao': 'https://ark.cn-beijing.volces.com/api/v3',
    'minimax': 'https://api.minimaxi.com/v1',
    'ollama': 'http://localhost:11434/v1',  # Ollama 本地部署
}

# 兼容旧名称
TEXT_MODEL_BASE_URLS = VENDOR_BASE_URLS

# ==================== 厂商路由函数 ====================

def get_vendor_from_model(model):
    """从模型名称推断厂商标识"""
    if not model:
        return 'agnes'
    model_lower = model.lower()
    # Ollama 模型名称前缀检测
    if model_lower.startswith('ollama:') or model_lower.startswith('ollama/'):
        return 'ollama'
    for prefix in VENDOR_BASE_URLS:
        if prefix in ('agnes', 'ollama'):
            continue
        if model_lower.startswith(prefix):
            return prefix
    # 检查是否是 Ollama 动态检测到的模型（存储在配置中）
    ollama_config = get_ollama_config()
    if ollama_config.get('enabled'):
        ollama_models = ollama_config.get('models', [])
        if model in ollama_models:
            return 'ollama'
    return 'agnes'

def get_vendor_base_url(model):
    """根据模型名称获取厂商 Base URL（优先检查自定义模型，其次自定义配置，最后默认）"""
    # 先检查自定义模型
    custom_config = get_custom_model_config(model)
    if custom_config and custom_config.get('base_url'):
        return custom_config['base_url']
    
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        vendor = get_vendor_from_model(model)
        custom_key = f'{vendor}_base_url'
        custom_url = data.get(custom_key, '').strip()
        if custom_url:
            return custom_url
        if vendor in ('deepseek', 'gpt', 'qwen', 'doubao'):
            text_url = data.get('text_base_url', '').strip()
            if text_url:
                return text_url
    return VENDOR_BASE_URLS.get(get_vendor_from_model(model), BASE_URL)

def get_vendor_api_key(model, fallback_key=None):
    """根据模型名称获取厂商 API Key（优先检查自定义模型，其次厂商专用 Key，回退到全局 Key）"""
    # Ollama 不需要 API Key
    vendor = get_vendor_from_model(model)
    if vendor == 'ollama':
        return 'ollama'
    
    # 先检查自定义模型
    custom_config = get_custom_model_config(model)
    if custom_config and custom_config.get('api_key'):
        return custom_config['api_key']
    
    vendor = get_vendor_from_model(model)
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        vendor_key = data.get(f'{vendor}_api_key', '').strip()
        if vendor_key:
            return vendor_key
        if vendor in ('deepseek', 'gpt', 'qwen', 'doubao'):
            text_key = data.get('text_api_key', '').strip()
            if text_key:
                return text_key
    if fallback_key:
        return fallback_key
    return get_api_key()

# 兼容旧函数名
def get_text_base_url(model=None):
    return get_vendor_base_url(model)

# ==================== API Key 读写 ====================

def get_api_key():
    """读取保存的 API Key"""
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('api_key', '')
    return ''

def get_text_api_key():
    """读取文本模型专用 API Key，未设置则回退到全局 Key"""
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        text_key = data.get('text_api_key', '').strip()
        if text_key:
            return text_key
    return get_api_key()


# ==================== 自定义模型管理 ====================

def get_custom_models():
    """获取所有自定义模型列表"""
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('custom_models', [])
    return []


def save_custom_models(custom_models):
    """保存自定义模型列表"""
    config_file = get_config_path()
    data = {}
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    data['custom_models'] = custom_models
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_custom_model(model_id, display_name, model_type, base_url, api_key=''):
    """添加自定义模型
    
    Args:
        model_id: 模型 ID（用于 API 调用）
        display_name: 显示名称
        model_type: 模型类型 (text/image/video)
        base_url: API Base URL
        api_key: API Key（可选，未填则使用全局 Key）
    """
    custom_models = get_custom_models()
    # 检查是否已存在
    for m in custom_models:
        if m['id'] == model_id:
            return False, '模型 ID 已存在'
    custom_models.append({
        'id': model_id,
        'name': display_name,
        'type': model_type,
        'base_url': base_url.rstrip('/'),
        'api_key': api_key
    })
    save_custom_models(custom_models)
    return True, '添加成功'


def remove_custom_model(model_id):
    """删除自定义模型"""
    custom_models = get_custom_models()
    custom_models = [m for m in custom_models if m['id'] != model_id]
    save_custom_models(custom_models)
    return True


def get_custom_model_config(model_id):
    """获取自定义模型的配置"""
    custom_models = get_custom_models()
    for m in custom_models:
        if m['id'] == model_id:
            return m
    return None


def get_custom_models_by_type(model_type):
    """获取指定类型的自定义模型字典 {id: display_name}"""
    custom_models = get_custom_models()
    result = {}
    for m in custom_models:
        if m['type'] == model_type:
            result[m['id']] = f"{m['name']} (自定义)"
    return result


# ==================== Ollama 配置 ====================

OLLAMA_DEFAULT_URL = 'http://localhost:11434'

def get_ollama_config():
    """获取 Ollama 配置"""
    config_file = get_config_path()
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('ollama', {'enabled': False, 'base_url': OLLAMA_DEFAULT_URL, 'models': []})
    return {'enabled': False, 'base_url': OLLAMA_DEFAULT_URL, 'models': []}


def save_ollama_config(ollama_config):
    """保存 Ollama 配置"""
    config_file = get_config_path()
    data = {}
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    data['ollama'] = ollama_config
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_ollama_models():
    """从 Ollama 服务获取可用模型列表"""
    import requests
    ollama_config = get_ollama_config()
    base_url = ollama_config.get('base_url', OLLAMA_DEFAULT_URL).rstrip('/')
    try:
        resp = requests.get(f'{base_url}/api/tags', timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = [m['name'] for m in data.get('models', [])]
            return models
    except Exception as e:
        print(f"[Ollama] 获取模型列表失败: {e}")
    return []
