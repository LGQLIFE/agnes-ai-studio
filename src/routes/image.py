"""
图片生成 + 上传路由
"""

import os
import uuid
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify

from ..config import get_vendor_api_key, get_vendor_base_url, resolve_image_url, ensure_output_dirs
from ..services.video_gen import download_and_save_file

image_bp = Blueprint('image', __name__)


@image_bp.route('/api/image/generate', methods=['POST'])
def generate_image():
    """文生图"""
    data = request.get_json()
    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'success': False, 'error': '请输入图片描述'}), 400

    size = data.get('size', '1024x1024')
    model = data.get('model', 'agnes-image-2.1-flash')
    save_local = data.get('save_local', True)
    
    api_key = get_vendor_api_key(model)
    if not api_key:
        return jsonify({'success': False, 'error': '请先配置 API Key'}), 401
    base_url = get_vendor_base_url(model)

    try:
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        payload = {
            'model': model,
            'prompt': prompt,
            'size': size
        }

        resp = requests.post(
            f'{base_url}/images/generations',
            headers=headers,
            json=payload,
            timeout=120
        )

        if resp.status_code == 200:
            result = resp.json()
            image_url = None
            if 'data' in result and len(result['data']) > 0:
                image_url = result['data'][0].get('url')

            local_filename = None
            if save_local and image_url:
                local_filename = download_and_save_file(
                    image_url, 'pictures', 'image', 'png'
                )

            return jsonify({
                'success': True,
                'image_url': image_url,
                'local_file': local_filename,
                'raw_response': result
            })
        else:
            return jsonify({
                'success': False,
                'error': f'API 错误 ({resp.status_code}): {resp.text}'
            }), resp.status_code

    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': '请求超时，请重试'}), 504
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@image_bp.route('/api/image/img2img', methods=['POST'])
def img2img():
    """图生图 / 图片编辑（支持多张参考图）"""
    data = request.get_json()
    prompt = data.get('prompt', '').strip()
    # 支持多张图片：优先使用 image_urls 数组，兼容旧的 image_url 单张
    image_urls = data.get('image_urls', [])
    if not image_urls:
        single_url = data.get('image_url', '').strip()
        if single_url:
            image_urls = [single_url]
    if not prompt or not image_urls:
        return jsonify({'success': False, 'error': '请输入描述并至少提供一张图片'}), 400

    # 转换本地图片为 base64
    resolved_urls = []
    for u in image_urls:
        resolved = resolve_image_url(u.strip()) if isinstance(u, str) else u
        if resolved:
            resolved_urls.append(resolved)
    if not resolved_urls:
        return jsonify({'success': False, 'error': '图片URL无效'}), 400

    size = data.get('size', '1024x768')
    save_local = data.get('save_local', True)
    model = data.get('model', 'agnes-image-2.1-flash')
    
    api_key = get_vendor_api_key(model)
    if not api_key:
        return jsonify({'success': False, 'error': '请先配置 API Key'}), 401
    base_url = get_vendor_base_url(model)

    try:
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        payload = {
            'model': model,
            'prompt': prompt,
            'size': size,
            'extra_body': {
                'tags': ['img2img'],
                'image': resolved_urls,  # 支持多张图片
                'response_format': 'url'
            }
        }

        resp = requests.post(
            f'{base_url}/images/generations',
            headers=headers,
            json=payload,
            timeout=120
        )

        if resp.status_code == 200:
            result = resp.json()
            image_url_out = None
            if 'data' in result and len(result['data']) > 0:
                image_url_out = result['data'][0].get('url')

            local_filename = None
            if save_local and image_url_out:
                local_filename = download_and_save_file(
                    image_url_out, 'pictures', 'edited_image', 'png'
                )

            return jsonify({
                'success': True,
                'image_url': image_url_out,
                'local_file': local_filename,
                'raw_response': result
            })
        else:
            return jsonify({
                'success': False,
                'error': f'API 错误 ({resp.status_code}): {resp.text}'
            }), resp.status_code

    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': '请求超时，请重试'}), 504
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@image_bp.route('/api/upload/image', methods=['POST'])
def upload_image():
    """上传本地图片，返回可访问的 URL"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未找到上传文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
    if ext not in allowed_exts:
        return jsonify({'success': False, 'error': f'不支持的图片格式: {ext}'}), 400

    _, pictures_dir = ensure_output_dirs()
    unique_name = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    save_path = os.path.join(pictures_dir, unique_name)
    file.save(save_path)

    return jsonify({
        'success': True,
        'filename': unique_name,
        'url': f'/pictures/{unique_name}'
    })
