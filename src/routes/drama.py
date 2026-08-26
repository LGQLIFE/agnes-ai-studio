"""
短剧生成路由 + 流水线
"""

import os
import json
import time
import uuid
import threading
import requests
from flask import Blueprint, request, jsonify

from ..config import (
    get_api_key, get_app_dir, get_vendor_api_key, get_vendor_base_url, shutdown_event,
    get_custom_models_by_type
)
from ..models import (
    drama_tasks, drama_lock, ensure_drama_dirs,
    TEXT_MODEL_OPTIONS, IMAGE_MODEL_OPTIONS, VIDEO_MODEL_OPTIONS,
    DEFAULT_TEXT_MODEL, DEFAULT_IMAGE_MODEL, DEFAULT_VIDEO_MODEL
)
from ..services.text_model import (
    call_text_model, parse_json_from_text,
    story_system_prompt, script_system_prompt, storyboard_system_prompt, assets_system_prompt,
    build_video_prompt, sanitize_image_prompt,
    translate_cn_to_en, is_mostly_chinese
)
from ..services.video_gen import download_and_save_file, download_video_by_video_id
from ..services.video_merge import merge_videos, burn_chinese_subtitle

# 每个短剧任务的停止事件
drama_stop_events = {}

# ==================== 角色风格样式映射 ====================
CHARACTER_STYLES = {
    'anime': {
        'name': '动漫卡通',
        'character': (
            "high quality anime character design sheet, detailed illustration, "
            "vibrant colors, clean lineart, soft shading, professional concept art. "
            "soft natural studio lighting, warm color temperature. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "high quality anime scene design, detailed illustration, vibrant colors, "
            "soft shading, professional concept art, warm color temperature. "
            "soft natural studio lighting. "
        ),
        'prop': (
            "high quality anime prop design sheet, detailed illustration, vibrant colors, "
            "soft shading, professional concept art, warm color temperature. "
            "soft natural studio lighting. "
        ),
    },
    'realistic': {
        'name': '写实真人',
        'character': (
            "photorealistic character design sheet, hyper-detailed real human reference, "
            "professional photography style, natural skin texture, realistic lighting. "
            "studio lighting setup, 8K ultra HD quality. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "photorealistic scene design, hyper-detailed environment, "
            "professional photography style, natural lighting, 8K ultra HD quality. "
            "realistic textures and materials. "
        ),
        'prop': (
            "photorealistic prop design sheet, hyper-detailed object reference, "
            "professional product photography style, studio lighting, 8K ultra HD quality. "
            "realistic textures and materials. "
        ),
    },
    'pixar3d': {
        'name': '皮克斯3D',
        'character': (
            "Pixar 3D style character design sheet, cute cartoon character, "
            "smooth 3D rendering, soft global illumination, vibrant saturated colors. "
            "Disney animation style, rounded shapes, big expressive eyes. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "Pixar 3D style scene design, cute cartoon environment, "
            "smooth 3D rendering, soft global illumination, vibrant saturated colors. "
            "Disney animation style. "
        ),
        'prop': (
            "Pixar 3D style prop design sheet, cute cartoon object, "
            "smooth 3D rendering, soft global illumination, vibrant saturated colors. "
            "Disney animation style. "
        ),
    },
    'watercolor': {
        'name': '水彩手绘',
        'character': (
            "beautiful watercolor painting character design sheet, soft brush strokes, "
            "delicate color bleeding effects, hand-painted illustration style. "
            "artistic watercolor textures, warm pastel tones. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "beautiful watercolor painting scene design, soft brush strokes, "
            "delicate color bleeding effects, hand-painted illustration style. "
            "artistic watercolor textures, warm pastel tones. "
        ),
        'prop': (
            "beautiful watercolor painting prop design sheet, soft brush strokes, "
            "delicate color bleeding effects, hand-painted illustration style. "
            "artistic watercolor textures, warm pastel tones. "
        ),
    },
    'ink': {
        'name': '中国水墨',
        'character': (
            "Chinese ink painting style character design sheet, traditional sumi-e brush strokes, "
            "elegant black ink on rice paper, minimalist composition, zen aesthetics. "
            "subtle color accents, artistic calligraphy elements. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "Chinese ink painting style scene design, traditional sumi-e brush strokes, "
            "elegant black ink on rice paper, minimalist composition, zen aesthetics. "
            "subtle color accents. "
        ),
        'prop': (
            "Chinese ink painting style prop design sheet, traditional sumi-e brush strokes, "
            "elegant black ink on rice paper, minimalist composition, zen aesthetics. "
            "subtle color accents. "
        ),
    },
    'semi_realistic': {
        'name': '半写实插画',
        'character': (
            "semi-realistic digital painting character design sheet, detailed illustration, "
            "realistic proportions with stylized features, smooth rendering. "
            "professional concept art, balanced between realism and stylization. "
            "9:16 vertical composition, pure white minimalist background, premium character design board layout. "
        ),
        'scene': (
            "semi-realistic digital painting scene design, detailed illustration, "
            "realistic proportions with stylized features, smooth rendering. "
            "professional concept art, balanced between realism and stylization. "
        ),
        'prop': (
            "semi-realistic digital painting prop design sheet, detailed illustration, "
            "realistic proportions with stylized features, smooth rendering. "
            "professional concept art, balanced between realism and stylization. "
        ),
    },
}

DEFAULT_CHARACTER_STYLE = 'anime'


def get_style_base(category, character_style=None, custom_style_text=None):
    """根据分类和风格获取基础样式提示词
    
    Args:
        category: 'character', 'scene', 'prop'
        character_style: 风格名称
        custom_style_text: 自定义风格描述文本（当 character_style='custom' 时使用）
    """
    # 如果是自定义风格，使用用户提供的风格描述
    if character_style == 'custom' and custom_style_text:
        return (
            f"{custom_style_text} style, "
            "high quality, detailed illustration, professional concept art. "
            "9:16 vertical composition, pure white minimalist background, premium design board layout. "
        )
    style = CHARACTER_STYLES.get(character_style, CHARACTER_STYLES['anime'])
    return style.get(category, style['character'])


def build_character_image_prompt(desc, character_style=None, custom_style_text=None):
    """根据角色描述自动识别角色类型，生成合适的图片 prompt
    
    Args:
        desc: 角色描述
        character_style: 风格名称
        custom_style_text: 自定义风格描述文本
    """
    desc_lower = desc.lower()
    
    # 检测角色类型
    # 植物关键词
    plant_keywords = ['flower', 'rose', 'tree', 'plant', 'leaf', 'seed', 'root', 'stem', 'branch',
                      'grass', 'vine', 'bush', 'shrub', 'bloom', 'petal', 'bud', 'blossom',
                      '花', '玫瑰', '树', '植物', '叶', '种子', '根', '茎', '枝', '草', '藤',
                      '灌木', '花苞', '花瓣', '花蕾', '开花', '发芽', '竹', '松', '柳', '桃',
                      '菊', '兰', '莲', '荷', '牡丹', '向日葵', '百合', '郁金香']
    # 动物关键词
    animal_keywords = ['cat', 'dog', 'bird', 'fish', 'rabbit', 'horse', 'deer', 'bear', 'lion',
                       'tiger', 'wolf', 'fox', 'mouse', 'rat', 'snake', 'frog', 'turtle', 'whale',
                       'dolphin', 'eagle', 'hawk', 'owl', 'butterfly', 'bee', 'ant', 'spider',
                       '猫', '狗', '鸟', '鱼', '兔', '马', '鹿', '熊', '狮', '虎', '狼', '狐',
                       '鼠', '蛇', '蛙', '龟', '鲸', '海豚', '鹰', '猫头鹰', '蝴蝶', '蜂', '蚁',
                       '蜘蛛', '鸡', '鸭', '鹅', '猪', '牛', '羊', '猴', '象', '企鹅', '鹦鹉']
    
    is_plant = any(kw in desc_lower for kw in plant_keywords)
    is_animal = any(kw in desc_lower for kw in animal_keywords)
    
    base_style = get_style_base('character', character_style, custom_style_text)
    
    if is_plant and not is_animal:
        # 植物角色
        return (
            f"{base_style}"
            f"Plant character design: show the plant in its natural form at various growth stages. "
            f"Left side: large-scale full-body illustration of the plant in its prime state. "
            f"Right top: front/side/back views showing the plant from different angles. "
            f"Right middle: close-up of the most distinctive feature (flower bud, leaf pattern, seed texture). "
            f"Left bottom: root system or base detail showcase. "
            f"Right bottom: texture details of petals, leaves, bark, or surface features. "
            f"Plant description: {desc}. "
            f"Same plant throughout, shape color and features fully consistent, no deformation. "
            f"Natural growth pose, rigorous botanical accuracy."
        ), '768x1344'
    elif is_animal and not is_plant:
        # 动物角色
        return (
            f"{base_style}"
            f"Animal character design: show the animal character with expressive features. "
            f"Left side: large-scale full-body illustration in standing or natural pose. "
            f"Right top: front/side/back three-view orthographic. "
            f"Right middle: face close-up with expressive eyes, below it detail shots of ears, paws, tail. "
            f"Left bottom: paw or claw detail showcase. "
            f"Right bottom: fur/feather/scale texture, markings and color pattern details. "
            f"Animal description: {desc}. "
            f"Same animal throughout, fur color markings and features fully consistent, no deformation. "
            f"Natural pose, rigorous anatomical structure."
        ), '768x1344'
    else:
        # 人类角色（默认）
        return (
            f"{base_style}"
            f"natural warm skin tone with healthy complexion, soft skin texture, "
            f"lifelike appearance, natural facial features. "
            f"Left side: large-scale front full-body illustration. "
            f"Right top: front/side/back three-view orthographic. "
            f"Right middle: one front face close-up, below it 5 small expression close-ups including 1 side face. "
            f"Left bottom: hand detail showcase (clear fingers, no extra or missing fingers). "
            f"Right bottom: clothing, accessories, hair detail close-ups. "
            f"Character description: {desc}. "
            f"Same character throughout, facial features hairstyle and clothing fully consistent, no deformation, no distortion. "
            f"Standard standing pose, rigorous structure."
        ), '768x1344'


drama_bp = Blueprint('drama', __name__)

# 暂停事件：每个短剧任务一个，用于 Step 3 完成后等待用户确认
drama_pause_events = {}
# 合并前确认事件：用于 Step 4 完成后等待用户确认是否等待失败镜头
drama_merge_pause_events = {}
# 故事编辑确认事件：用于 Step 1a 完成后等待用户确认/编辑故事
drama_story_edit_events = {}
# 素材重生成跟踪：drama_id -> {asset_index: threading.Event}
drama_asset_regen_events = {}
# 视频生成启动事件：用于素材确认后等待用户手动点击“开始生成视频”
drama_video_start_events = {}


# ==================== 短剧流水线 ====================

def drama_pipeline(drama_id, api_key, text_api_key=None):
    """短剧生成 5 步流水线（后台线程执行）"""
    if text_api_key is None:
        text_api_key = api_key

    # 创建该任务的停止事件
    drama_stop_events[drama_id] = threading.Event()

    def _update(**kwargs):
        with drama_lock:
            if drama_id in drama_tasks:
                drama_tasks[drama_id].update(kwargs)

    def _is_shutdown():
        return shutdown_event.is_set() or drama_stop_events.get(drama_id, threading.Event()).is_set()

    try:
        text_model = drama_tasks[drama_id].get('text_model', DEFAULT_TEXT_MODEL)

        # ---- Step 1a: 生成故事梗概 ----
        print(f"[短剧 {drama_id}] Step 1a: 生成故事梗概...")
        _update(status='step1', step='step1', message='①a 正在创作故事梗概...')
        if _is_shutdown(): return

        try:
            story_text = call_text_model(
                story_system_prompt(),
                f"请根据以下描述，创作一个 300～500 字的短剧故事：\n{drama_tasks[drama_id]['prompt']}",
                text_api_key,
                model=text_model
            )
            _update(story=story_text, message='故事梗概完成')
            print(f"[短剧 {drama_id}] 故事: {story_text[:200]}...")
        except Exception as e:
            _update(status='failed', message=f'故事生成失败: {e}')
            return

        # ---- Step 1a 完成，暂停让用户确认/编辑故事梗概 ----
        _update(status='paused_story', step='paused_story',
                message='故事梗概已生成，您可以编辑后确认，或直接继续。等待 3 分钟未确认将自动继续')
        print(f"[短剧 {drama_id}] Step 1a 完成，暂停等待用户确认故事（3分钟超时）...")

        story_edit_event = drama_story_edit_events.get(drama_id)
        if story_edit_event:
            story_edit_event.clear()
            confirmed = story_edit_event.wait(timeout=180)  # 等待 3 分钟
            if not confirmed:
                print(f"[短剧 {drama_id}] 故事确认超时(3分钟)，自动继续生成剧本")
                _update(message='故事确认超时，自动继续生成剧本...')
            else:
                # 检查用户是否编辑了故事
                with drama_lock:
                    edited_story = drama_tasks[drama_id].get('edited_story', '')
                if edited_story:
                    story_text = edited_story
                    _update(story=story_text)
                    print(f"[短剧 {drama_id}] 用户编辑了故事，使用编辑后版本")
                else:
                    print(f"[短剧 {drama_id}] 用户确认故事，继续生成剧本")
        if _is_shutdown(): return

        # ---- Step 1b: 生成专业剧本 ----
        print(f"[短剧 {drama_id}] Step 1b: 生成专业剧本...")
        _update(message='①b 正在将故事改编为拍摄剧本...')
        if _is_shutdown(): return

        try:
            script_text = call_text_model(
                script_system_prompt(),
                f"请将以下故事 1:1 精准还原为专业短剧剧本，要求画面描述详细，有 vo 的台词必须搭配画面，特写镜头要标注：\n\n{story_text}",
                text_api_key,
                model=text_model,
                max_tokens=8192
            )
            _update(script=script_text, message='剧本生成完成')
            print(f"[短剧 {drama_id}] 剧本: {script_text[:200]}...")
        except Exception as e:
            _update(status='failed', message=f'剧本生成失败: {e}')
            return

        # ---- Step 2: 生成分镜 ----
        if _is_shutdown(): return
        print(f"[短剧 {drama_id}] Step 2: 生成分镜...")
        _update(status='step2', step='step2', message='正在生成分镜脚本...')

        shot_duration = drama_tasks[drama_id].get('shot_duration', 5)
        try:
            storyboard_text = call_text_model(
                storyboard_system_prompt(shot_duration),
                f"请将以下剧本改写为分镜脚本，每个分镜约{shot_duration}秒：\n\n{script_text}",
                text_api_key,
                model=text_model,
                max_tokens=16384
            )
            storyboard = parse_json_from_text(storyboard_text)
            shots = storyboard.get('shots', [])
            _update(storyboard=storyboard, shots=shots, message=f'分镜生成完成，共 {len(shots)} 个镜头')
            print(f"[短剧 {drama_id}] 分镜数: {len(shots)}")
        except Exception as e:
            _update(status='failed', message=f'分镜生成失败: {e}')
            return

        # ---- Step 3: 提取素材 + 生成参考图 ----
        if _is_shutdown(): return
        print(f"[短剧 {drama_id}] Step 3: 提取素材并生成参考图...")
        _update(status='step3', step='step3', message='正在提取角色/场景/道具特征...')

        try:
            assets_text = call_text_model(
                assets_system_prompt(),
                f"请从以下剧本和分镜中提取所有角色、场景、道具的视觉特征描述：\n"
                f"剧本：\n{script_text}\n\n"
                f"分镜：\n{json.dumps(storyboard, ensure_ascii=False)}",
                text_api_key,
                model=text_model,
                max_tokens=16384
            )
            assets = parse_json_from_text(assets_text)
            all_assets = []
            for cat in ('characters', 'scenes', 'props'):
                for item in assets.get(cat, []):
                    all_assets.append({
                        'category': cat,
                        'name': item.get('name', ''),
                        'desc': item.get('desc', ''),
                        'image_url': None,
                        'local_file': None
                    })
            _update(assets=all_assets, message=f'提取到 {len(all_assets)} 个素材，正在生成参考图...')
        except Exception as e:
            _update(status='failed', message=f'素材提取失败: {e}')
            return

        drama_base = ensure_drama_dirs(drama_id)
        character_style = drama_tasks[drama_id].get('character_style', DEFAULT_CHARACTER_STYLE)
        custom_character_style = drama_tasks[drama_id].get('custom_character_style', '')
        img_success = 0
        img_fail = 0
        for idx, asset in enumerate(all_assets):
            if _is_shutdown(): return
            category = asset.get('category', 'characters')
            cat_label = {'characters': '角色', 'scenes': '场景', 'props': '道具'}.get(category, '素材')
            _update(message=f'生成{cat_label}图 ({idx+1}/{len(all_assets)}): {asset["name"]}...')

            desc = asset.get('desc', '')
            prompt_en = asset.get('prompt_en', '')  # 素材提取时生成的英文 prompt
            
            # 优先使用素材提取时的 prompt_en，否则用模板构建
            if prompt_en:
                img_prompt = prompt_en
                if category == 'scenes':
                    img_size = '1344x768'
                else:
                    img_size = '768x1344'
            elif category == 'characters':
                img_prompt, img_size = build_character_image_prompt(desc, character_style, custom_character_style)
            elif category == 'scenes':
                style_base = get_style_base('scene', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"16:9 horizontal composition, pure white background border. "
                    f"Scene environment design concept art, multiple angles view. "
                    f"Scene description: {desc}. "
                    f"Highly detailed environment, consistent style, no characters."
                )
                img_size = '1344x768'
            else:
                style_base = get_style_base('prop', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"9:16 vertical composition, pure white minimalist background, premium prop design board layout. "
                    f"Multiple views: front, side, back, top, detail close-ups. "
                    f"Material and texture details clearly visible. "
                    f"Prop description: {desc}. "
                    f"Consistent design, no deformation, high detail craftsmanship showcase."
                )
                img_size = '768x1344'
            
            image_model = drama_tasks[drama_id].get('image_model', DEFAULT_IMAGE_MODEL)
            img_base_url = get_vendor_base_url(image_model)
            img_api_key = get_vendor_api_key(image_model, fallback_key=api_key)
            headers = {'Authorization': f'Bearer {img_api_key}', 'Content-Type': 'application/json'}

            # 清洗 prompt 中的敏感内容
            img_prompt = sanitize_image_prompt(img_prompt)
            
            # 保存 prompt 到 asset 数据（用于前端显示和自定义编辑）
            asset['img_prompt'] = img_prompt

            # 重试机制：最多重试 2 次（共 3 次尝试）
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    resp = requests.post(f'{img_base_url}/images/generations', headers=headers,
                        json={'model': image_model, 'prompt': img_prompt, 'size': img_size},
                        timeout=180)
                    if resp.status_code == 200:
                        result = resp.json()
                        if 'data' in result and len(result['data']) > 0:
                            image_url = result['data'][0].get('url')
                            asset['image_url'] = image_url
                            if image_url:
                                # 用中文名作为文件名前缀
                                safe_name = asset.get('name', f'asset_{idx}').replace(' ', '_')
                                local = download_and_save_file(image_url, f'dramas/{drama_id}/images', safe_name, 'png')
                                asset['local_file'] = local
                                img_success += 1
                                print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}]: OK")
                                break
                            else:
                                print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}] 警告: data[0] 中无 url 字段，响应: {json.dumps(result, ensure_ascii=False)[:300]}")
                        else:
                            print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}] 警告: 响应中无 data 字段，响应: {json.dumps(result, ensure_ascii=False)[:300]}")
                    elif resp.status_code in (429, 503, 433):
                        # 限流或服务不可用，等待后重试
                        wait_sec = 15 * (attempt + 1)
                        print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}] 服务繁忙({resp.status_code})，等待{wait_sec}秒后重试 ({attempt+1}/{max_retries})")
                        time.sleep(wait_sec)
                        continue
                    else:
                        print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}] API错误 {resp.status_code}: {resp.text[:300]}")
                        if attempt < max_retries:
                            time.sleep(5)
                            continue
                    # 没有 break 说明本次失败且无更多重试
                    if not asset.get('image_url'):
                        img_fail += 1
                        print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}]: FAIL")
                        break
                except requests.exceptions.Timeout:
                    print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}] 请求超时 (attempt {attempt+1}/{max_retries+1})")
                    if attempt < max_retries:
                        time.sleep(5)
                        continue
                    img_fail += 1
                    print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset['name']}]: FAIL (超时)")
                    break
                except Exception as e:
                    print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset.get('name', '?')}] 图片生成异常: {type(e).__name__}: {e}")
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    img_fail += 1
                    print(f"[短剧 {drama_id}] 素材 {idx+1} [{asset.get('name', '?')}]: FAIL (异常)")
                    break

            # 请求间隔 2 秒，避免连续请求触发限流
            if idx < len(all_assets) - 1:
                time.sleep(2)
            with drama_lock:
                drama_tasks[drama_id]['assets'] = list(all_assets)

        _update(message=f'参考图生成完成，{img_success}/{len(all_assets)} 成功' + (f'，{img_fail} 失败' if img_fail else ''))

        # ---- 暂停等待用户确认参考图（超时 2 分钟自动继续）----
        _update(status='paused', step='paused', message='参考图已就绪，请检查并确认（可手动替换/重新生成），2 分钟内未确认将自动继续')
        print(f"[短剧 {drama_id}] Step 3 完成，暂停等待用户确认（超时 2 分钟）...")
        pause_event = drama_pause_events.get(drama_id)
        if pause_event:
            confirmed = pause_event.wait(timeout=120)  # 最多等待 2 分钟
            if confirmed:
                print(f"[短剧 {drama_id}] 用户已确认，继续 Step 4...")
            else:
                print(f"[短剧 {drama_id}] 等待超时（2分钟），自动继续 Step 4...")
                _update(message='确认超时，自动继续生成视频...')
        if _is_shutdown(): return

        # ---- 等待所有进行中的素材重生成完成 ----
        regen_events = drama_asset_regen_events.get(drama_id, {})
        if regen_events:
            pending_count = sum(1 for e in regen_events.values() if not e.is_set())
            if pending_count > 0:
                _update(message=f'等待 {pending_count} 个素材重新生成完成...')
                print(f"[短剧 {drama_id}] 等待 {pending_count} 个素材重生成完成...")
                for idx, evt in regen_events.items():
                    evt.wait(timeout=300)  # 每个最多等待 5 分钟
                _update(message='素材重生成完成，开始生成视频...')
                print(f"[短剧 {drama_id}] 所有素材重生成已完成，继续 Step 4")
        if _is_shutdown(): return

        # ---- Step 4a: 预计算所有镜头的提示词和参考图 ----
        print(f"[短剧 {drama_id}] Step 4a: 预计算所有镜头提示词和参考图...")
        shot_duration_to_frames = {5: 121, 10: 241, 18: 441}
        num_frames = shot_duration_to_frames.get(shot_duration, 121)
        video_results = []
        
        for shot_idx, shot in enumerate(shots):
            if _is_shutdown(): return
            shot_chars = [c.lower().strip() for c in shot.get('characters', [])]
            shot_asset_list = []
            primary_image = None
        
            # 匹配角色素材
            for asset in all_assets:
                if not asset.get('image_url'):
                    continue
                asset_name = asset.get('name', '').lower().strip()
                if any(asset_name in c or c in asset_name for c in shot_chars):
                    shot_asset_list.append(asset)
                    if not primary_image:
                        primary_image = asset['image_url']
        
            # 匹配场景素材
            for asset in all_assets:
                if not asset.get('image_url') or asset.get('category') != 'scenes':
                    continue
                asset_name = asset.get('name', '').lower().strip()
                scene_desc = shot.get('scene_desc', '').lower()
                if asset_name and asset_name in scene_desc:
                    shot_asset_list.append(asset)
                    if not primary_image:
                        primary_image = asset['image_url']
        
            # 匹配道具素材
            for asset in all_assets:
                if not asset.get('image_url') or asset.get('category') != 'props':
                    continue
                asset_name = asset.get('name', '').lower().strip()
                action_desc = shot.get('action', '').lower()
                if asset_name and asset_name in action_desc:
                    shot_asset_list.append(asset)
        
            # 如果没有主图，使用第一个角色素材
            if not primary_image:
                for asset in all_assets:
                    if asset.get('image_url') and asset.get('category') == 'characters':
                        primary_image = asset['image_url']
                        shot_asset_list.append(asset)
                        break
        
            video_prompt_en, video_prompt_cn = build_video_prompt(shot, shot_asset_list)
            video_prompt = video_prompt_en
            shot_ref_images = []
            for a in shot_asset_list:
                if a.get('image_url'):
                    local_file = a.get('local_file', '')
                    shot_ref_images.append({
                        'asset_name': a.get('name', ''),
                        'category': a.get('category', ''),
                        'image_url': a['image_url'],
                        'local_file': local_file
                    })
            with drama_lock:
                if 'shot_details' not in drama_tasks[drama_id]:
                    drama_tasks[drama_id]['shot_details'] = {}
                drama_tasks[drama_id]['shot_details'][shot.get('shot_index', shot_idx+1)] = {
                    'video_prompt': video_prompt_en,
                    'video_prompt_cn': video_prompt_cn,
                    'reference_images': shot_ref_images,
                    'primary_image': primary_image
                }
            video_results.append({
                'shot_index': shot.get('shot_index', shot_idx+1),
                'status': 'pending',
                'prompt': video_prompt
            })
        
        with drama_lock:
            drama_tasks[drama_id]['video_results'] = list(video_results)
        
        # ---- Step 4b: 暂停让用户检查提示词和参考图，逐个启动视频生成 ----
        _update(status='paused_video', step='paused_video',
                message='素材已就绪，请检查分镜提示词和参考图，点击每个镜头的「生成视频」按钮逐个启动')
        print(f"[短剧 {drama_id}] Step 4b: 等待用户逐个启动视频生成...")
        
        # 等待所有镜头视频生成完成（非阻塞，用户逐个点击启动）
        while True:
            if _is_shutdown(): return
            with drama_lock:
                current_results = drama_tasks[drama_id].get('video_results', [])
                all_done = all(v.get('status') in ('completed', 'failed') for v in current_results)
                if all_done and len(current_results) >= len(shots):
                    video_results = list(current_results)
                    break
        
            # 更新进度消息
            with drama_lock:
                cr = drama_tasks[drama_id].get('video_results', [])
            completed_count = sum(1 for v in cr if v.get('status') == 'completed')
            failed_count = sum(1 for v in cr if v.get('status') == 'failed')
            generating_count = sum(1 for v in cr if v.get('status') == 'generating')
            pending_count = sum(1 for v in cr if v.get('status') == 'pending')
        
            if generating_count > 0:
                _update(message=f'视频生成中... 已完成 {completed_count}/{len(shots)}，生成中 {generating_count}')
            elif pending_count > 0:
                _update(message=f'请启动视频生成: {pending_count} 个待启动，{completed_count} 个已完成')
            else:
                _update(message=f'全部 {completed_count} 个镜头生成完成')
        
            if shutdown_event.wait(timeout=3):
                return
        
        # ---- Step 4 完成，直接设为 completed（不自动合并，用户可手动合并）----
        failed_shots = [v for v in video_results if v.get('status') == 'failed']
        completed_count = sum(1 for v in video_results if v["status"] == "completed")
        
        if failed_shots:
            final_msg = f'{completed_count} 个镜头成功，{len(failed_shots)} 个失败。可重新生成失败镜头或手动合并已完成的视频'
        else:
            final_msg = f'全部 {completed_count} 个镜头生成完成！可手动合并视频'
        
        _update(status='completed', step='completed', message=final_msg)
        print(f"[短剧 {drama_id}] 所有镜头处理完成: {final_msg}")
        print(f"[短剧 {drama_id}] 完成: {video_results}")

    except Exception as e:
        print(f"[短剧 {drama_id}] 流水线异常: {e}")
        _update(status='failed', message=f'流水线错误: {e}')


# ==================== 短剧 API 路由 ====================

@drama_bp.route('/api/drama/start', methods=['POST'])
def drama_start():
    """启动短剧生成流水线"""
    data = request.get_json()
    api_key = get_api_key()
    if not api_key:
        return jsonify({'success': False, 'error': '请先配置 API Key'}), 401

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'success': False, 'error': '请输入短剧描述'}), 400

    shot_duration = data.get('shot_duration', 5)
    text_model = data.get('text_model', DEFAULT_TEXT_MODEL)
    image_model = data.get('image_model', DEFAULT_IMAGE_MODEL)
    video_model = data.get('video_model', DEFAULT_VIDEO_MODEL)
    character_style = data.get('character_style', DEFAULT_CHARACTER_STYLE)
    custom_character_style = data.get('custom_character_style', '').strip()
    drama_id = uuid.uuid4().hex[:12]

    text_api_key = get_vendor_api_key(text_model, fallback_key=api_key)

    with drama_lock:
        drama_tasks[drama_id] = {
            'drama_id': drama_id, 'status': 'pending', 'step': '',
            'prompt': prompt, 'shot_duration': shot_duration,
            'text_model': text_model, 'image_model': image_model, 'video_model': video_model,
            'character_style': character_style,
            'custom_character_style': custom_character_style,
            'text_api_key': text_api_key,
            'script': None, 'story': None, 'storyboard': None, 'shots': [],
            'assets': [], 'video_results': [], 'shot_details': {},
            'wait_for_failed_shots': False, 'edited_story': '',
            'message': '正在启动...', 'created_at': time.time()
        }
        drama_pause_events[drama_id] = threading.Event()
        drama_merge_pause_events[drama_id] = threading.Event()
        drama_story_edit_events[drama_id] = threading.Event()
        drama_video_start_events[drama_id] = threading.Event()
        drama_asset_regen_events[drama_id] = {}

    thread = threading.Thread(target=drama_pipeline, args=(drama_id, api_key, text_api_key), daemon=True)
    thread.start()

    return jsonify({'success': True, 'drama_id': drama_id, 'status': 'pending'})


@drama_bp.route('/api/drama/stop', methods=['POST'])
def drama_stop():
    """停止短剧生成流水线"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    if not drama_id:
        return jsonify({'success': False, 'error': '缺少 drama_id'}), 400

    # 触发该任务的停止事件
    if drama_id in drama_stop_events:
        drama_stop_events[drama_id].set()

    # 更新任务状态
    with drama_lock:
        if drama_id in drama_tasks:
            drama_tasks[drama_id]['status'] = 'stopped'
            drama_tasks[drama_id]['message'] = '用户已停止生成'

    print(f"[短剧 {drama_id}] 用户请求停止")
    return jsonify({'success': True, 'message': '已发送停止信号'})


@drama_bp.route('/api/drama/resume', methods=['POST'])
def drama_resume():
    """用户确认参考图后恢复流水线"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    if not drama_id:
        return jsonify({'success': False, 'error': '缺少 drama_id'}), 400

    pause_event = drama_pause_events.get(drama_id)
    if not pause_event:
        return jsonify({'success': False, 'error': '任务不存在或无需恢复'}), 404

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama or drama.get('status') != 'paused':
            return jsonify({'success': False, 'error': '当前状态无需确认'}), 400

    pause_event.set()
    print(f"[短剧 {drama_id}] 用户确认参考图，流水线已恢复")
    return jsonify({'success': True, 'message': '已确认，继续生成视频...'})


@drama_bp.route('/api/drama/story/confirm', methods=['POST'])
def drama_story_confirm():
    """用户确认或编辑故事梗概后恢复流水线"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    edited_story = data.get('edited_story', '').strip()
    if not drama_id:
        return jsonify({'success': False, 'error': '缺少 drama_id'}), 400

    story_edit_event = drama_story_edit_events.get(drama_id)
    if not story_edit_event:
        return jsonify({'success': False, 'error': '任务不存在或无需确认'}), 404

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama or drama.get('status') != 'paused_story':
            return jsonify({'success': False, 'error': '当前状态无需确认'}), 400
        # 如果用户编辑了故事，保存编辑后的版本
        if edited_story:
            drama['edited_story'] = edited_story
            print(f"[短剧 {drama_id}] 用户编辑了故事梗概 ({len(edited_story)} 字)")
        else:
            print(f"[短剧 {drama_id}] 用户确认原始故事梗概")
        # 立即更新状态，防止轮询再次触发确认弹窗
        drama['status'] = 'step1'
        drama['step'] = 'step1'
        drama['message'] = '故事已确认，正在生成剧本...'

    story_edit_event.set()
    return jsonify({'success': True, 'message': '已确认，开始生成剧本...'})


@drama_bp.route('/api/drama/merge/confirm', methods=['POST'])
def drama_merge_confirm():
    """用户确认是否等待失败镜头重新生成后再合并"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    wait_for_retry = data.get('wait_for_retry', False)
    if not drama_id:
        return jsonify({'success': False, 'error': '缺少 drama_id'}), 400

    merge_pause_event = drama_merge_pause_events.get(drama_id)
    if not merge_pause_event:
        return jsonify({'success': False, 'error': '任务不存在或无需确认'}), 404

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama or drama.get('status') != 'paused_merge':
            return jsonify({'success': False, 'error': '当前状态无需确认'}), 400
        drama['wait_for_failed_shots'] = wait_for_retry

    if wait_for_retry:
        print(f"[短剧 {drama_id}] 用户选择等待失败镜头重新生成")
    else:
        print(f"[短剧 {drama_id}] 用户选择直接合并已成功的镜头")

    merge_pause_event.set()
    return jsonify({'success': True, 'wait_for_retry': wait_for_retry})


@drama_bp.route('/api/drama/video/start', methods=['POST'])
def drama_video_start():
    """用户手动点击“开始生成视频”按钮后恢复流水线"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    if not drama_id:
        return jsonify({'success': False, 'error': '缺少 drama_id'}), 400

    video_start_event = drama_video_start_events.get(drama_id)
    if not video_start_event:
        return jsonify({'success': False, 'error': '任务不存在或无需启动'}), 404

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama or drama.get('status') != 'paused_video':
            return jsonify({'success': False, 'error': '当前状态无需启动'}), 400
        drama['status'] = 'step4'
        drama['step'] = 'step4'
        drama['message'] = '开始逐镜头生成视频...'

    video_start_event.set()
    print(f"[短剧 {drama_id}] 用户点击启动视频生成，流水线已恢复")
    return jsonify({'success': True, 'message': '开始生成视频...'})


@drama_bp.route('/api/drama/merge/custom', methods=['POST'])
def drama_merge_custom():
    """用户自定义选择镜头顺序并合并为视频
    
    请求体:
        drama_id: 短剧 ID
        shot_indices: 镜头顺序列表 [1, 3, 2, 5] 表示按镜头1→3→2→5顺序合并
        merge_name: 可选，合并视频名称前缀
    """
    data = request.get_json()
    drama_id = data.get('drama_id')
    shot_indices = data.get('shot_indices', [])
    merge_name = data.get('merge_name', 'custom')
    
    if not drama_id or not shot_indices or len(shot_indices) < 2:
        return jsonify({'success': False, 'error': '至少需要选择 2 个镜头'}), 400
    
    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        video_results = list(drama.get('video_results', []))
    
    # 验证所选镜头都存在且已完成
    completed_shots = {v['shot_index'] for v in video_results if v.get('status') == 'completed' and v.get('local_file')}
    valid_indices = [si for si in shot_indices if si in completed_shots]
    if len(valid_indices) < 2:
        return jsonify({'success': False, 'error': f'有效的已完成镜头不足 2 个（选了 {len(valid_indices)} 个）'}), 400
    
    # 执行合并
    prefix = f'merge_{merge_name}' if merge_name != 'custom' else 'custom'
    merged_file = merge_videos(drama_id, video_results, shot_order=valid_indices, output_prefix=prefix)
    
    if merged_file:
        # 记录自定义合并结果
        with drama_lock:
            if 'custom_merges' not in drama_tasks[drama_id]:
                drama_tasks[drama_id]['custom_merges'] = []
            drama_tasks[drama_id]['custom_merges'].append({
                'name': merge_name,
                'shot_indices': valid_indices,
                'merged_file': merged_file
            })
        print(f"[短剧 {drama_id}] 自定义合并成功: {merged_file} (镜头顺序: {valid_indices})")
        return jsonify({'success': True, 'merged_file': merged_file, 'shot_indices': valid_indices})
    else:
        return jsonify({'success': False, 'error': '合并失败，请检查日志'}), 500


@drama_bp.route('/api/drama/models', methods=['GET'])
def drama_models():
    """返回可选模型列表（包含自定义模型 + Ollama 动态检测模型）"""
    from ..config import get_ollama_config
    # 合并自定义模型
    text_models = dict(TEXT_MODEL_OPTIONS)
    text_models.update(get_custom_models_by_type('text'))
    image_models = dict(IMAGE_MODEL_OPTIONS)
    image_models.update(get_custom_models_by_type('image'))
    video_models = dict(VIDEO_MODEL_OPTIONS)
    video_models.update(get_custom_models_by_type('video'))
    
    # 动态加入 Ollama 已检测到的模型
    ollama_config = get_ollama_config()
    if ollama_config.get('enabled'):
        for model_name in ollama_config.get('models', []):
            model_id = f'ollama:{model_name}'
            label = f'Ollama {model_name} (本地)'
            text_models[model_id] = label
    
    return jsonify({
        'success': True,
        'text_models': text_models,
        'image_models': image_models,
        'video_models': video_models,
        'defaults': {
            'text_model': DEFAULT_TEXT_MODEL,
            'image_model': DEFAULT_IMAGE_MODEL,
            'video_model': DEFAULT_VIDEO_MODEL
        }
    })


@drama_bp.route('/api/drama/status/<drama_id>', methods=['GET'])
def drama_status(drama_id):
    """查询短剧任务状态"""
    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        return jsonify({
            'success': True,
            'drama_id': drama['drama_id'],
            'status': drama['status'],
            'step': drama.get('step', ''),
            'message': drama.get('message', ''),
            'prompt': drama['prompt'],
            'shot_duration': drama.get('shot_duration', 5),
            'script': drama.get('script'),
            'story': drama.get('story'),
            'storyboard': drama.get('storyboard'),
            'assets': drama.get('assets', []),
            'shot_details': drama.get('shot_details', {}),
            'video_results': drama.get('video_results', []),
            'merged_video': drama.get('merged_video'),
            'custom_merges': drama.get('custom_merges', []),
            'shots_count': len(drama.get('shots', [])),
            'completed_shots': sum(1 for v in drama.get('video_results', []) if v.get('status') == 'completed'),
            'created_at': drama['created_at']
        })


@drama_bp.route('/api/drama/list', methods=['GET'])
def drama_list():
    """列出所有短剧任务"""
    with drama_lock:
        items = []
        for did, d in drama_tasks.items():
            items.append({
                'drama_id': d['drama_id'], 'status': d['status'],
                'prompt': d['prompt'][:60] + ('...' if len(d['prompt']) > 60 else ''),
                'shot_duration': d.get('shot_duration', 5),
                'shots_count': len(d.get('shots', [])),
                'completed_shots': sum(1 for v in d.get('video_results', []) if v.get('status') == 'completed'),
                'assets_count': len(d.get('assets', [])),
                'message': d.get('message', ''),
                'created_at': d['created_at']
            })
        return jsonify({'success': True, 'dramas': items})


@drama_bp.route('/api/drama/asset/replace', methods=['POST'])
def drama_asset_replace():
    """手动替换素材参考图"""
    drama_id = request.form.get('drama_id')
    asset_index = request.form.get('asset_index')
    if not drama_id or asset_index is None:
        return jsonify({'success': False, 'error': '缺少 drama_id 或 asset_index'}), 400

    asset_index = int(asset_index)

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        assets = drama.get('assets', [])
        if asset_index < 0 or asset_index >= len(assets):
            return jsonify({'success': False, 'error': '素材索引越界'}), 400

    # 检查上传文件
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未上传文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    # 保存文件
    drama_base = ensure_drama_dirs(drama_id)
    ext = os.path.splitext(file.filename)[1].lower() or '.png'
    filename = f'asset_{asset_index}_custom{ext}'
    filepath = os.path.join(drama_base, 'images', filename)
    file.save(filepath)

    # 更新素材记录
    with drama_lock:
        assets[asset_index]['local_file'] = filename
        assets[asset_index]['image_url'] = f'/dramas/{drama_id}/images/{filename}'
        drama['assets'] = list(assets)

    print(f"[短剧 {drama_id}] 素材 {asset_index+1} 参考图已手动替换: {filename}")
    return jsonify({
        'success': True,
        'asset_index': asset_index,
        'filename': filename,
        'image_url': f'/dramas/{drama_id}/images/{filename}'
    })


@drama_bp.route('/api/drama/asset/regenerate', methods=['POST'])
def drama_asset_regenerate():
    """重新生成单个素材参考图（支持自定义中文描述）"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    asset_index = data.get('asset_index')
    custom_desc = data.get('custom_desc', '')  # 用户自定义中文描述
    if not drama_id or asset_index is None:
        return jsonify({'success': False, 'error': '缺少 drama_id 或 asset_index'}), 400

    asset_index = int(asset_index)

    # 创建重生成事件，跟踪进行中的重生成
    regen_event = threading.Event()
    with drama_lock:
        if drama_id not in drama_asset_regen_events:
            drama_asset_regen_events[drama_id] = {}
        drama_asset_regen_events[drama_id][asset_index] = regen_event

    try:
        with drama_lock:
            drama = drama_tasks.get(drama_id)
            if not drama:
                return jsonify({'success': False, 'error': '任务不存在'}), 404
            assets = drama.get('assets', [])
            if asset_index < 0 or asset_index >= len(assets):
                return jsonify({'success': False, 'error': '素材索引越界'}), 400
            asset = assets[asset_index]
            api_key = drama.get('api_key', '')

        category = asset.get('category', 'characters')
        desc = asset.get('desc', '')
        name = asset.get('name', '')
        original_prompt_en = asset.get('prompt_en', '')
        character_style = drama.get('character_style', DEFAULT_CHARACTER_STYLE)
        custom_character_style = drama.get('custom_character_style', '')

        # 如果用户提供了自定义中文描述，替换原始 desc 并重建 prompt
        if custom_desc and custom_desc.strip():
            desc = custom_desc.strip()
            # 同步更新 asset 中的 desc
            with drama_lock:
                assets[asset_index]['desc'] = desc
            # 用新 desc 通过模板重建英文 prompt
            if category == 'characters':
                img_prompt, img_size = build_character_image_prompt(desc, character_style, custom_character_style)
            elif category == 'scenes':
                style_base = get_style_base('scene', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"16:9 horizontal composition, pure white background border. "
                    f"Scene environment design concept art, multiple angles view. "
                    f"Scene description: {desc}. "
                    f"Highly detailed environment, consistent style, no characters."
                )
                img_size = '1344x768'
            else:
                style_base = get_style_base('prop', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"9:16 vertical composition, pure white minimalist background, premium prop design board layout. "
                    f"Multiple views: front, side, back, top, detail close-ups. "
                    f"Material and texture details clearly visible. "
                    f"Prop description: {desc}. "
                    f"Consistent design, no deformation, high detail craftsmanship showcase."
                )
                img_size = '768x1344'
        else:
            # 无自定义，优先使用原始 prompt_en
            if original_prompt_en:
                img_prompt = original_prompt_en
                img_size = '1344x768' if category == 'scenes' else '768x1344'
            elif category == 'characters':
                img_prompt, img_size = build_character_image_prompt(desc, character_style, custom_character_style)
            elif category == 'scenes':
                style_base = get_style_base('scene', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"16:9 horizontal composition, pure white background border. "
                    f"Scene environment design concept art, multiple angles view. "
                    f"Scene description: {desc}. "
                    f"Highly detailed environment, consistent style, no characters."
                )
                img_size = '1344x768'
            else:
                style_base = get_style_base('prop', character_style, custom_character_style)
                img_prompt = (
                    f"{style_base}"
                    f"9:16 vertical composition, pure white minimalist background, premium prop design board layout. "
                    f"Multiple views: front, side, back, top, detail close-ups. "
                    f"Material and texture details clearly visible. "
                    f"Prop description: {desc}. "
                    f"Consistent design, no deformation, high detail craftsmanship showcase."
                )
                img_size = '768x1344'

        image_model = drama.get('image_model', DEFAULT_IMAGE_MODEL)
        img_base_url = get_vendor_base_url(image_model)
        img_api_key = get_vendor_api_key(image_model, fallback_key=api_key)
        headers = {'Authorization': f'Bearer {img_api_key}', 'Content-Type': 'application/json'}

        img_prompt = sanitize_image_prompt(img_prompt)

        # 调用图片 API（带重试）
        max_retries = 2
        image_url = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(f'{img_base_url}/images/generations', headers=headers,
                    json={'model': image_model, 'prompt': img_prompt, 'size': img_size}, timeout=180)
                if resp.status_code == 200:
                    result = resp.json()
                    if 'data' in result and len(result['data']) > 0:
                        image_url = result['data'][0].get('url')
                        break
                elif resp.status_code in (429, 503, 433) and attempt < max_retries:
                    time.sleep(15 * (attempt + 1))
                    continue
                else:
                    print(f"[素材重生成] 素材 {name} API 错误 {resp.status_code}")
                    break
            except Exception as e:
                print(f"[素材重生成] 素材 {name} 异常: {e}")
                if attempt < max_retries:
                    time.sleep(5)
                    continue

        if not image_url:
            return jsonify({'success': False, 'error': '图片生成失败，请重试'}), 500

        # 保存文件
        safe_name = name.replace(' ', '_')
        local = download_and_save_file(image_url, f'dramas/{drama_id}/images', safe_name, 'png')

        with drama_lock:
            assets[asset_index]['image_url'] = image_url
            assets[asset_index]['local_file'] = local
            assets[asset_index]['img_prompt'] = img_prompt
            assets[asset_index]['desc'] = desc
            drama['assets'] = list(assets)

        print(f"[短剧 {drama_id}] 素材 {asset_index+1} [{name}] 参考图已重新生成: {local}")
        return jsonify({
            'success': True,
            'asset_index': asset_index,
            'filename': local,
            'image_url': f'/dramas/{drama_id}/images/{local}',
            'img_prompt': img_prompt,
            'desc': desc
        })
    finally:
        # 无论成功或失败，都标记重生成完成
        regen_event.set()


@drama_bp.route('/api/drama/shot/upload_image', methods=['POST'])
def drama_shot_upload_image():
    """上传镜头自定义参考图，并添加到 shot_details 的参考图列表"""
    drama_id = request.form.get('drama_id')
    shot_index = request.form.get('shot_index')
    if not drama_id or shot_index is None:
        return jsonify({'success': False, 'error': '缺少 drama_id 或 shot_index'}), 400

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未上传文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    shot_index = int(shot_index)
    drama_base = ensure_drama_dirs(drama_id)
    ext = os.path.splitext(file.filename)[1].lower() or '.png'
    filename = f'shot_{shot_index}_ref_{uuid.uuid4().hex[:6]}{ext}'
    filepath = os.path.join(drama_base, 'images', filename)
    file.save(filepath)

    image_url = f'/dramas/{drama_id}/images/{filename}'
    print(f"[短剧 {drama_id}] 镜头 {shot_index} 上传自定义参考图: {filename}")

    # 添加到 shot_details 的参考图列表
    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if drama:
            if 'shot_details' not in drama:
                drama['shot_details'] = {}
            if shot_index not in drama['shot_details']:
                drama['shot_details'][shot_index] = {'video_prompt': '', 'video_prompt_cn': '', 'reference_images': [], 'primary_image': None}
            shot_detail = drama['shot_details'][shot_index]
            # 添加新上传的参考图
            new_img = {
                'asset_name': f'自定义_{filename[:10]}',
                'category': 'custom',
                'image_url': image_url,
                'local_file': filename
            }
            if 'reference_images' not in shot_detail:
                shot_detail['reference_images'] = []
            shot_detail['reference_images'].append(new_img)
            # 如果没有主参考图，设为新上传的
            if not shot_detail.get('primary_image'):
                shot_detail['primary_image'] = image_url

    return jsonify({
        'success': True,
        'image_url': image_url,
        'filename': filename
    })


@drama_bp.route('/api/drama/shot/delete_image', methods=['POST'])
def drama_shot_delete_image():
    """删除镜头的某张参考图"""
    data = request.get_json()
    drama_id = data.get('drama_id')
    shot_index = data.get('shot_index')
    image_index = data.get('image_index')  # 参考图在列表中的索引
    if not drama_id or shot_index is None or image_index is None:
        return jsonify({'success': False, 'error': '缺少参数'}), 400

    shot_index = int(shot_index)
    image_index = int(image_index)

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        shot_detail = drama.get('shot_details', {}).get(shot_index)
        if not shot_detail:
            return jsonify({'success': False, 'error': f'镜头 {shot_index} 无参考图信息'}), 404
        ref_images = shot_detail.get('reference_images', [])
        if image_index < 0 or image_index >= len(ref_images):
            return jsonify({'success': False, 'error': f'图片索引 {image_index} 超出范围'}), 400

        removed = ref_images.pop(image_index)
        print(f"[短剧 {drama_id}] 镜头 {shot_index} 删除参考图: {removed.get('asset_name', '')}")

        # 如果删除的是主参考图，更新为剩余第一张或 None
        primary = shot_detail.get('primary_image', '')
        if removed.get('image_url') == primary:
            shot_detail['primary_image'] = ref_images[0].get('image_url', '') if ref_images else None

    return jsonify({'success': True, 'remaining': len(ref_images)})


@drama_bp.route('/api/drama/shot/regenerate', methods=['POST'])
def drama_shot_regenerate():
    """启动/重新生成单个镜头视频（后台异步执行）
    支持自定义参数:
      - custom_prompt: 自定义视频提示词（覆盖自动生成）
      - custom_images: 自定义参考图列表 [{"image_url": "..."}, ...]（覆盖自动匹配）
    """
    data = request.get_json()
    drama_id = data.get('drama_id')
    shot_index = data.get('shot_index')
    custom_prompt = data.get('custom_prompt', '').strip() or None
    custom_images = data.get('custom_images', None)  # list of {"image_url": "..."}
    if not drama_id or shot_index is None:
        return jsonify({'success': False, 'error': '缺少 drama_id 或 shot_index'}), 400

    shot_index = int(shot_index)

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        shots = drama.get('shots', [])
        video_results = drama.get('video_results', [])
        api_key = drama.get('api_key', '')

        # 找到对应的 shot
        target_shot = None
        result_idx = None
        for i, v in enumerate(video_results):
            if v.get('shot_index') == shot_index:
                result_idx = i
                break
        for shot in shots:
            if shot.get('shot_index', shots.index(shot)+1) == shot_index:
                target_shot = shot
                break

        if not target_shot:
            return jsonify({'success': False, 'error': f'镜头 {shot_index} 不存在'}), 404

        # 检查是否已经在生成中
        if result_idx is not None:
            current_status = video_results[result_idx].get('status', '')
            if current_status == 'generating':
                return jsonify({'success': False, 'error': f'镜头 {shot_index} 正在生成中，请等待完成'}), 400

    # 标记为生成中
    if result_idx is not None:
        with drama_lock:
            video_results[result_idx] = {'shot_index': shot_index, 'status': 'generating'}
            drama['video_results'] = list(video_results)

    # 启动后台线程生成视频（传入自定义参数）
    thread = threading.Thread(
        target=_regenerate_shot_video,
        args=(drama_id, shot_index, target_shot, api_key, result_idx, custom_prompt, custom_images),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'shot_index': shot_index, 'message': '开始重新生成'})


def _regenerate_shot_video(drama_id, shot_index, shot, api_key, result_idx, custom_prompt=None, custom_images=None):
    """后台线程：重新生成单个镜头视频"""
    import json as _json

    with drama_lock:
        drama = drama_tasks.get(drama_id)
        if not drama:
            return
        all_assets = drama.get('assets', [])
        shot_duration = drama.get('shot_duration', 5)

    shot_duration_to_frames = {5: 121, 10: 241, 18: 441}
    num_frames = shot_duration_to_frames.get(shot_duration, 121)

    # 使用自定义参数或自动匹配
    video_prompt_cn = ''
    if custom_prompt:
        # 如果是中文提示词，先翻译为英文再发给视频模型
        if is_mostly_chinese(custom_prompt):
            video_prompt = translate_cn_to_en(custom_prompt, drama.get('text_api_key', ''))
            video_prompt_cn = custom_prompt
            print(f"[镜头重生成] 镜头 {shot_index} 中文提示词已翻译为英文")
        else:
            video_prompt = custom_prompt
            video_prompt_cn = custom_prompt
        print(f"[镜头重生成] 镜头 {shot_index} 使用自定义提示词")
    else:
        shot_chars = [c.lower().strip() for c in shot.get('characters', [])]
        shot_asset_list = []
        for asset in all_assets:
            if not asset.get('image_url'):
                continue
            asset_name = asset.get('name', '').lower().strip()
            if any(asset_name in c or c in asset_name for c in shot_chars):
                shot_asset_list.append(asset)
        video_prompt_en, video_prompt_cn = build_video_prompt(shot, shot_asset_list)
        video_prompt = video_prompt_en

    # 确定参考图：自定义列表 > 自动匹配
    primary_image = None
    if custom_images and len(custom_images) > 0:
        primary_image = custom_images[0].get('image_url', '')
        print(f"[镜头重生成] 镜头 {shot_index} 使用 {len(custom_images)} 张自定义参考图")
    else:
        shot_chars = [c.lower().strip() for c in shot.get('characters', [])]
        for asset in all_assets:
            if not asset.get('image_url'):
                continue
            asset_name = asset.get('name', '').lower().strip()
            if any(asset_name in c or c in asset_name for c in shot_chars):
                if not primary_image:
                    primary_image = asset['image_url']
        if not primary_image:
            for asset in all_assets:
                if asset.get('image_url') and asset.get('category') == 'characters':
                    primary_image = asset['image_url']
                    break

    # 更新 shot_details 中的记录
    with drama_lock:
        if 'shot_details' not in drama:
            drama['shot_details'] = {}
        drama['shot_details'][shot_index] = {
            'video_prompt': video_prompt,
            'video_prompt_cn': video_prompt_cn,
            'reference_images': custom_images or drama.get('shot_details', {}).get(shot_index, {}).get('reference_images', []),
            'primary_image': primary_image
        }

    try:
        video_model = drama.get('video_model', DEFAULT_VIDEO_MODEL)
        vid_base_url = get_vendor_base_url(video_model)
        vid_api_key = get_vendor_api_key(video_model, fallback_key=api_key)
        headers = {'Authorization': f'Bearer {vid_api_key}', 'Content-Type': 'application/json'}
        if video_model.startswith('agnes-video-2.5'):
            # Agnes Video 2.5 新参数格式：禁止 width/height/num_frames/negative_prompt 等字段
            seconds = max(4, min(12, num_frames // 24))
            if primary_image:
                payload = {
                    'model': video_model, 'prompt': video_prompt,
                    'mode': 'keyframe', 'first_frame': primary_image,
                    'seconds': str(seconds), 'size': '720P',
                }
            else:
                payload = {
                    'model': video_model, 'prompt': video_prompt,
                    'mode': 'text', 'seconds': str(seconds), 'size': '720P',
                    'aspect_ratio': '16:9',
                }
        else:
            payload = {
                'model': video_model, 'prompt': video_prompt,
                'width': 1152, 'height': 768,
                'num_frames': num_frames, 'frame_rate': 24,
                'negative_prompt': 'text, subtitles, captions, labels, letters, words, writing, watermark, signs, typography, English text, Chinese text, any text overlay'
            }
            if primary_image:
                payload['image'] = primary_image

        # 提交视频任务
        vtask_id = None
        max_submit_retries = 3
        use_negative_prompt = True
        for submit_attempt in range(max_submit_retries + 1):
            resp = requests.post(f'{vid_base_url}/videos', headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                vdata = resp.json()
                vtask_id = vdata.get('task_id') or vdata.get('id') or vdata.get('video_id')
                v_video_id_resp = vdata.get('video_id') or vdata.get('id', '')
                break
            elif resp.status_code == 400 and use_negative_prompt and 'negative_prompt' in resp.text.lower():
                print(f"[镜头重生成] 视频模型不支持 negative_prompt 参数，已移除")
                payload.pop('negative_prompt', None)
                use_negative_prompt = False
                continue
            elif resp.status_code in (503, 429, 433) and submit_attempt < max_submit_retries:
                wait_sec = 30 * (submit_attempt + 1)
                print(f"[镜头重生成] 镜头 {shot_index} 队列满({resp.status_code})，等待{wait_sec}秒...")
                time.sleep(wait_sec)
                continue
            else:
                with drama_lock:
                    drama_tasks[drama_id]['video_results'][result_idx] = {
                        'shot_index': shot_index, 'status': 'failed',
                        'error': f'API {resp.status_code}: {resp.text[:300]}', 'prompt': video_prompt
                    }
                return

        if not vtask_id:
            with drama_lock:
                drama_tasks[drama_id]['video_results'][result_idx] = {
                    'shot_index': shot_index, 'status': 'failed',
                    'error': '视频任务提交失败', 'prompt': video_prompt
                }
            return

        print(f"[镜头重生成] 镜头 {shot_index} 已提交，task_id={vtask_id}")

        # 判断是否使用 /agnesapi 查询端点
        use_agnesapi_poll = video_model.startswith('agnes-video-2.5') and v_video_id_resp

        # 轮询等待完成
        v_url = ''
        v_video_id = ''
        for poll_i in range(120):
            time.sleep(10)
            try:
                if use_agnesapi_poll:
                    poll_resp = requests.get(
                        f'{vid_base_url}/agnesapi',
                        headers=headers,
                        params={'video_id': v_video_id_resp, 'model_name': video_model},
                        timeout=30
                    )
                else:
                    poll_resp = requests.get(f'{vid_base_url}/videos/{vtask_id}', headers=headers, timeout=30)
                if poll_resp.status_code != 200:
                    continue
                # 检查是否是直接的视频流
                content_type = poll_resp.headers.get('Content-Type', '')
                if 'video' in content_type or 'octet-stream' in content_type:
                    print(f"[镜头重生成] 镜头 {shot_index} /agnesapi 返回视频流")
                    v_video_id = v_video_id_resp
                    break
                pr_data = poll_resp.json()
                v_status = pr_data.get('status', '')
                if v_status == 'completed':
                    # 提取视频 URL（多种字段兼容）
                    v_url = (pr_data.get('video_url') or pr_data.get('url')
                             or pr_data.get('output_url') or pr_data.get('video') or '')
                    if not v_url and isinstance(pr_data.get('data'), dict):
                        v_url = pr_data['data'].get('url', '') or pr_data['data'].get('video_url', '')
                    if not v_url and isinstance(pr_data.get('remixed_from_video_id'), str) and pr_data['remixed_from_video_id'].startswith('http'):
                        v_url = pr_data['remixed_from_video_id']
                    # 提取 video_id（新 API 格式）
                    v_video_id = pr_data.get('video_id', '') or v_video_id_resp
                    break
                elif v_status == 'failed':
                    with drama_lock:
                        drama_tasks[drama_id]['video_results'][result_idx] = {
                            'shot_index': shot_index, 'status': 'failed',
                            'error': pr_data.get('error', '生成失败'), 'prompt': video_prompt
                        }
                    return
            except Exception as e:
                print(f"[镜头重生成] 镜头 {shot_index} 轮询异常: {e}")
                continue
        else:
            with drama_lock:
                drama_tasks[drama_id]['video_results'][result_idx] = {
                    'shot_index': shot_index, 'status': 'failed',
                    'error': '轮询超时(20分钟)', 'prompt': video_prompt
                }
            return

        # 下载视频
        local_fn = None
        if v_url:
            print(f"[镜头重生成] 镜头 {shot_index} 开始下载视频...")
            try:
                local_fn = download_and_save_file(v_url, f'dramas/{drama_id}/videos', f'shot_{shot_index}', 'mp4')
            except Exception as dl_err:
                print(f"[镜头重生成] 镜头 {shot_index} 下载异常: {type(dl_err).__name__}: {dl_err}")
        if not local_fn:
            # 回退 content 端点
            try:
                content_resp = requests.get(f'{vid_base_url}/videos/{vtask_id}/content', headers=headers, timeout=30)
                if content_resp.status_code == 200:
                    content_data = content_resp.json()
                    c_url = content_data.get('url', '') or content_data.get('video_url', '') or content_data.get('video', '')
                    if not c_url and isinstance(content_data.get('data'), dict):
                        c_url = content_data['data'].get('url', '') or content_data['data'].get('video_url', '')
                    if c_url:
                        print(f"[镜头重生成] 镜头 {shot_index} 通过 content 端点获取URL")
                        local_fn = download_and_save_file(c_url, f'dramas/{drama_id}/videos', f'shot_{shot_index}', 'mp4')
            except Exception as ce:
                print(f"[镜头重生成] 镜头 {shot_index} content 端点请求失败: {ce}")
        if not local_fn and v_video_id:
            print(f"[镜头重生成] 镜头 {shot_index} 使用 video_id 下载: {v_video_id[:50]}...")
            model_name_param = video_model if video_model.startswith('agnes-video-2.5') else None
            local_fn = download_video_by_video_id(
                v_video_id, vid_base_url, headers,
                f'dramas/{drama_id}/videos', f'shot_{shot_index}',
                model_name=model_name_param
            )

        if local_fn:
            # 烧录中文字幕（如果有对话）
            dialogue = shot.get('dialogue', '')
            if dialogue:
                try:
                    from ..config import get_app_dir
                    app_dir = get_app_dir()
                    full_video_path = os.path.join(app_dir, 'dramas', drama_id, 'videos', local_fn)
                    if os.path.exists(full_video_path):
                        print(f"[镜头重生成] 镜头 {shot_index} 开始烧录字幕...")
                        burn_chinese_subtitle(full_video_path, dialogue)
                        print(f"[镜头重生成] 镜头 {shot_index} 字幕烧录完成")
                except Exception as sub_err:
                    print(f"[镜头重生成] 镜头 {shot_index} 字幕烧录异常: {type(sub_err).__name__}: {sub_err}")
            
            with drama_lock:
                drama_tasks[drama_id]['video_results'][result_idx] = {
                    'shot_index': shot_index, 'status': 'completed',
                    'video_url': v_url, 'local_file': local_fn, 'prompt': video_prompt
                }
            print(f"[镜头重生成] 镜头 {shot_index} 完成: {local_fn}")
        else:
            with drama_lock:
                drama_tasks[drama_id]['video_results'][result_idx] = {
                    'shot_index': shot_index, 'status': 'failed',
                    'error': '视频下载失败', 'prompt': video_prompt
                }
    except Exception as e:
        print(f"[镜头重生成] 镜头 {shot_index} 异常: {e}")
        with drama_lock:
            drama_tasks[drama_id]['video_results'][result_idx] = {
                'shot_index': shot_index, 'status': 'failed',
                'error': str(e), 'prompt': video_prompt
            }
