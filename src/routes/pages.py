"""
首页路由 + 静态文件服务 + 本地文件删除
"""

import os
from flask import Blueprint, request, jsonify, send_from_directory
from ..config import get_base_path, get_app_dir, ensure_output_dirs

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    """返回前端页面"""
    return send_from_directory(
        os.path.join(get_base_path(), 'static'),
        'index.html'
    )


@pages_bp.route('/api/file/delete', methods=['POST'])
def delete_local_file():
    """删除本地生成的文件（仅支持 pictures / videos 目录）"""
    data = request.get_json() or {}
    folder = data.get('folder', '')
    filename = data.get('filename', '')

    if folder not in ('pictures', 'videos'):
        return jsonify({'success': False, 'error': '不支持的目录'}), 400
    # 安全检查：防止路径穿越
    if not filename or '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'success': False, 'error': '无效的文件名'}), 400

    videos_dir, pictures_dir = ensure_output_dirs()
    target_dir = pictures_dir if folder == 'pictures' else videos_dir
    filepath = os.path.join(target_dir, filename)

    if not os.path.exists(filepath):
        return jsonify({'success': False, 'error': '文件不存在或已被删除'}), 404
    try:
        os.remove(filepath)
        print(f"[文件删除] {folder}/{filename}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'删除失败: {e}'}), 500


@pages_bp.route('/videos/<path:filename>')
def serve_video(filename):
    videos_dir, _ = ensure_output_dirs()
    return send_from_directory(videos_dir, filename)


@pages_bp.route('/pictures/<path:filename>')
def serve_picture(filename):
    _, pictures_dir = ensure_output_dirs()
    return send_from_directory(pictures_dir, filename)


@pages_bp.route('/dramas/<path:filename>')
def serve_drama_file(filename):
    """服务短剧输出文件（图片/视频）"""
    app_dir = get_app_dir()
    dramas_dir = os.path.join(app_dir, 'dramas')
    os.makedirs(dramas_dir, exist_ok=True)
    return send_from_directory(dramas_dir, filename)


@pages_bp.route('/anchor/<path:filename>')
def serve_anchor_file(filename):
    """服务数字人口播输出文件（形象图/视频/音频）"""
    app_dir = get_app_dir()
    anchor_dir = os.path.join(app_dir, 'anchor')
    os.makedirs(anchor_dir, exist_ok=True)
    return send_from_directory(anchor_dir, filename)
