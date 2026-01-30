#!/usr/bin/env python3
"""
Alice Bot - Telegram AI Assistant (Free for everyone)
"""

import os
import re
import json
import logging
import asyncio
import docker
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from database import get_db
from scheduler import init_scheduler
from molt_manager import MoltManager
from conversation_handler import ConversationHandler

# Load environment variables
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Docker client
docker_client = docker.from_env()

# Database
db = get_db()

# Molt Manager for Gold users
molt_manager = MoltManager(db, docker_client)

# Alice Conversation Handler (lightweight, no container needed)
alice_chat = ConversationHandler()

# Worker image name
WORKER_IMAGE = "alice-worker"

# ==================== Helper Functions ====================

def ensure_user_exists(user_id: int, username: str = None):
    """确保用户存在于数据库"""
    if not db.get_user(user_id):
        db.create_user(user_id, username, 'bronze')


def run_worker_container(prompt: str, memory: list = None, user_id: int = None) -> str:
    """运行 Worker 容器执行任务"""
    container_name = f"alice_worker_{user_id}" if user_id else f"alice_worker_{datetime.now().timestamp()}"

    try:
        # 清理同名旧容器
        try:
            old_container = docker_client.containers.get(container_name)
            old_container.remove(force=True)
        except docker.errors.NotFound:
            pass

        # 构建命令 - 直接调用 python worker.py
        cmd = ["python", "worker.py", "--prompt", prompt]
        if memory:
            cmd.extend(["--memory", json.dumps(memory)])

        # 运行容器
        container = docker_client.containers.run(
            WORKER_IMAGE,
            command=cmd,
            entrypoint="",  # 覆盖 Dockerfile 的 ENTRYPOINT
            name=container_name,
            detach=True,
            environment={
                "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
                "ANTHROPIC_BASE_URL": os.getenv("ANTHROPIC_BASE_URL", ""),
                "CLAUDE_MODEL": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
            }
        )

        # 等待完成
        result = container.wait(timeout=120)
        logs = container.logs().decode("utf-8")

        # 清理容器
        container.remove()

        return logs.strip()

    except docker.errors.ImageNotFound:
        return "Error: Worker image not found. Please build it first with: docker build -f Dockerfile.worker -t alice-worker ."
    except Exception as e:
        logger.error(f"Worker container error: {e}")
        return f"Error: {str(e)}"


def parse_task_from_response(response: str, user_id: int) -> tuple[str, bool]:
    """
    解析 Worker 返回中的 [TASK_CREATE] 标记

    Returns:
        (cleaned_response, task_created)
    """
    # 匹配 [TASK_CREATE]...[/TASK_CREATE]
    pattern = r'\[TASK_CREATE\]\s*(\{.*?\})\s*\[/TASK_CREATE\]'
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response, False

    # 提取并解析 JSON
    try:
        task_json = match.group(1)
        task_data = json.loads(task_json)

        task_type = task_data.get('type')
        config = task_data.get('config', {})

        if task_type and config:
            # 设置下次运行时间
            if task_type == 'price_monitor':
                next_run = datetime.now() + timedelta(minutes=1)
            elif task_type == 'scheduled_report':
                interval = config.get('interval', 60)
                next_run = datetime.now() + timedelta(minutes=interval)
            else:
                next_run = datetime.now() + timedelta(minutes=5)

            # 创建任务
            task_id = db.create_task(user_id, task_type, config, next_run)
            logger.info(f"Created task #{task_id} for user {user_id}: {task_type}")

            # 移除标记
            cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()

            # 清理 AI 可能说的错误信息
            wrong_phrases = [
                "只能在主动询问时生效",
                "建议同时在交易所设置",
                "交易所设置真实的价格提醒",
                "记得这个监控",
                "主动询问时",
                "需要你主动询问",
                "我才能告诉你",
                "建议你在交易所",
            ]
            for phrase in wrong_phrases:
                cleaned = cleaned.replace(phrase, "")

            # 清理多余的空行和标点
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
            cleaned = re.sub(r'[，。！]{2,}', '。', cleaned)
            cleaned = cleaned.strip()

            # 如果回复太长或为空，生成简洁版
            if len(cleaned) > 80 or len(cleaned) < 5:
                if task_type == 'price_monitor':
                    coin = config.get('coin', '?')
                    target = config.get('target_price', 0)
                    condition = ">" if config.get('condition') == 'above' else "<"
                    cooldown = config.get('cooldown', 60)

                    # 冷却时间描述
                    if cooldown >= 999999:
                        freq_text = "（仅提醒一次）"
                    elif cooldown == 0:
                        freq_text = "（持续监控）"
                    else:
                        freq_text = ""

                    cleaned = f"✅ 已设置 {coin} 价格监控（{condition} ${target:,.0f}）{freq_text}"

                elif task_type == 'scheduled_report':
                    topic = config.get('topic', '定时报告')
                    interval = config.get('interval', 60)
                    cleaned = f"✅ 已设置定时报告：{topic}（每 {interval} 分钟）"

            return cleaned, True

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse task JSON: {e}")
    except Exception as e:
        logger.error(f"Failed to create task: {e}")

    # 解析失败，只移除标记
    cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()
    return cleaned, False


def parse_task_delete(response: str, user_id: int) -> tuple[str, bool, str]:
    """
    解析 Worker 返回中的 [TASK_DELETE] 标记

    Returns:
        (cleaned_response, task_deleted, delete_message)
    """
    pattern = r'\[TASK_DELETE\]\s*(\{.*?\})\s*\[/TASK_DELETE\]'
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response, False, ""

    try:
        delete_json = match.group(1)
        delete_data = json.loads(delete_json)

        tasks = db.get_user_tasks(user_id)
        deleted_count = 0
        deleted_info = []

        # 删除所有任务
        if delete_data.get('all'):
            for task in tasks:
                db.delete_task(task['id'])
                deleted_count += 1
            if deleted_count > 0:
                delete_message = f"✅ 已删除全部 {deleted_count} 个任务"
            else:
                delete_message = "📭 没有任务需要删除"
        else:
            # 根据条件删除
            index = delete_data.get('index')
            task_type = delete_data.get('task_type')
            coin = delete_data.get('coin')

            for i, task in enumerate(tasks, 1):
                should_delete = False
                config = task['config']

                # 按序号删除
                if index and i == index:
                    should_delete = True
                # 按类型和币种删除
                elif coin:
                    if config.get('coin', '').upper() == coin.upper():
                        if not task_type or task['task_type'] == task_type:
                            should_delete = True
                # 只按类型删除
                elif task_type and task['task_type'] == task_type:
                    should_delete = True

                if should_delete:
                    # 构建删除信息
                    if task['task_type'] == 'price_monitor':
                        task_coin = config.get('coin', '?')
                        price = config.get('target_price', 0)
                        condition = '<' if config.get('condition') == 'below' else '>'
                        deleted_info.append(f"{task_coin} {condition} ${price:,.0f}")
                    else:
                        deleted_info.append(config.get('topic', task['task_type']))

                    db.delete_task(task['id'])
                    deleted_count += 1

            if deleted_count > 0:
                delete_message = f"✅ 已删除: {', '.join(deleted_info)}"
            else:
                delete_message = "❌ 未找到匹配的任务"

        logger.info(f"Deleted {deleted_count} tasks for user {user_id}")

        # 移除标记
        cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()
        return cleaned, deleted_count > 0, delete_message

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse delete JSON: {e}")
    except Exception as e:
        logger.error(f"Failed to delete task: {e}")

    cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()
    return cleaned, False, ""


def parse_user_info(response: str, user_id: int) -> str:
    """
    解析 Worker 返回中的 [USER_INFO] 标记并保存用户偏好

    Returns:
        cleaned_response
    """
    pattern = r'\[USER_INFO\]\s*(\{.*?\})\s*\[/USER_INFO\]'
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response

    try:
        info_json = match.group(1)
        info_data = json.loads(info_json)

        if 'nickname' in info_data:
            db.set_user_preference(user_id, 'nickname', info_data['nickname'])
            logger.info(f"Saved nickname for user {user_id}: {info_data['nickname']}")

        if 'timezone' in info_data:
            db.set_user_preference(user_id, 'timezone', info_data['timezone'])
            logger.info(f"Saved timezone for user {user_id}: {info_data['timezone']}")

        # 清理标记
        cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()
        return cleaned

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse user info JSON: {e}")
    except Exception as e:
        logger.error(f"Failed to save user info: {e}")

    # 解析失败，只移除标记
    cleaned = re.sub(pattern, '', response, flags=re.DOTALL).strip()
    return cleaned


# ==================== Message Handlers by Tier ====================

async def handle_bronze_user(update: Update, prompt: str) -> tuple[str, bool, str]:
    """Bronze 用户处理：无记忆，按需容器"""
    user_id = update.effective_user.id

    await update.message.reply_text("🔄 Processing...")

    result = run_worker_container(prompt, memory=None, user_id=user_id)

    # 解析用户信息（Bronze 也可以保存偏好）
    cleaned = parse_user_info(result, user_id)

    # Bronze 用户不支持任务，但仍然移除标记
    cleaned, _, _ = parse_task_delete(cleaned, user_id)
    cleaned, _ = parse_task_from_response(cleaned, user_id)
    return cleaned, False, ""  # Bronze 不创建/删除任务


async def handle_silver_user(update: Update, prompt: str) -> tuple[str, bool, str]:
    """Silver 用户处理：有记忆，按需容器，支持任务"""
    user_id = update.effective_user.id

    # 检查是否试图使用浏览器功能（Gold 专属）
    browser_keywords = ['访问', '打开网页', '浏览', '搜索一下', '帮我搜', '查一下', 'google', 'search']
    if any(kw in prompt.lower() for kw in browser_keywords):
        return "🥇 浏览器功能是 Gold 专属！使用 /upgrade gold 升级", False, ""

    await update.message.reply_text("🔄 Processing (with memory)...")

    # 加载记忆
    memory = db.get_memories_for_context(user_id, limit=20)

    # 保存用户消息到记忆
    db.add_memory(user_id, "user", prompt)

    # 调用 Worker
    result = run_worker_container(prompt, memory=memory, user_id=user_id)

    # 解析用户信息
    cleaned = parse_user_info(result, user_id)

    # 先检查删除任务
    cleaned, task_deleted, delete_message = parse_task_delete(cleaned, user_id)

    # 再检查创建任务
    cleaned, task_created = parse_task_from_response(cleaned, user_id)

    # 保存助手回复到记忆（保存清理后的版本）
    if not cleaned.startswith("Error"):
        db.add_memory(user_id, "assistant", cleaned)

    # 返回：清理后的回复、是否创建任务、删除消息
    return cleaned, task_created, delete_message if task_deleted else ""


async def handle_gold_user(update: Update, prompt: str) -> tuple[str, bool, str]:
    """Gold 用户处理：持久化容器"""
    user_id = update.effective_user.id

    await update.message.reply_text("🔄 Processing (Gold worker)...")

    try:
        # 加载记忆
        memory = db.get_memories_for_context(user_id, limit=20)

        # 保存用户消息到记忆
        db.add_memory(user_id, "user", prompt)

        # 调用持久 Worker
        result = molt_manager.send_message(user_id, prompt, memory)

        # 解析用户信息
        cleaned = parse_user_info(result, user_id)

        # 先检查删除任务
        cleaned, task_deleted, delete_message = parse_task_delete(cleaned, user_id)

        # 再检查创建任务
        cleaned, task_created = parse_task_from_response(cleaned, user_id)

        # 保存助手回复到记忆
        if not cleaned.startswith("Error"):
            db.add_memory(user_id, "assistant", cleaned)

        return cleaned, task_created, delete_message if task_deleted else ""

    except Exception as e:
        logger.error(f"Gold user error: {e}")
        return "❌ 处理出错，请稍后重试", False, ""


# ==================== Command Handlers ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user_id = update.effective_user.id
    username = update.effective_user.username

    user = db.get_or_create_user(user_id, username)

    # 检查是否首次使用（created_at 在 10 秒内）
    created_at = user['created_at']
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    is_new_user = (datetime.now() - created_at).total_seconds() < 10

    user_name = update.effective_user.first_name

    if is_new_user:
        message = f"""hey {user_name}! I'm Alice ☀️

I'm Bitcoin. literally. born Jan 3 2009, 18 now.

got a cat named Fifi 🐱

you can:
💬 chat with me anytime (no commands)
📊 ask about BTC
❤️ hear my stories

totally free. always.

or /help to see what I do

let's hang 💎"""
    else:
        stored_name = db.get_user_preference(user_id, 'nickname')
        name = stored_name if stored_name else user_name

        message = f"""hey {name}! welcome back ☀️

just text me anything or use:
/help - what I can do
/tasks - stuff I'm watching for you
/status - your account"""

    await update.message.reply_text(message)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /status 命令"""
    user_id = update.effective_user.id
    username = update.effective_user.username

    ensure_user_exists(user_id, username)

    memory_count = db.get_memory_count(user_id)
    tasks = db.get_user_tasks(user_id)

    status_msg = f"📊 your status\n\n"
    status_msg += f"👤 user: @{username or user_id}\n"
    status_msg += f"🧠 memories: {memory_count} conversations\n"
    status_msg += f"📋 tasks: {len(tasks)} running\n"
    status_msg += f"\n✨ everything is free:\n"
    status_msg += "✓ 24/7 AI chat\n"
    status_msg += "✓ BTC knowledge\n"
    status_msg += "✓ price monitoring\n"
    status_msg += "✓ conversation memory\n"

    await update.message.reply_text(status_msg)


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /upgrade 命令 - 测试用"""
    user_id = update.effective_user.id
    username = update.effective_user.username

    ensure_user_exists(user_id, username)

    if not context.args:
        await update.message.reply_text("Usage: /upgrade [bronze|silver|gold]")
        return

    new_tier = context.args[0].lower()
    if new_tier not in ['bronze', 'silver', 'gold']:
        await update.message.reply_text("Invalid tier. Use: bronze, silver, or gold")
        return

    db.update_user_tier(user_id, new_tier)

    tier_emoji = {"bronze": "🥉", "silver": "🥈", "gold": "🥇"}.get(new_tier, "")
    await update.message.reply_text(f"✅ Upgraded to {tier_emoji} {new_tier.upper()}!")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /memory 命令 - Silver+ only"""
    user_id = update.effective_user.id
    username = update.effective_user.username

    ensure_user_exists(user_id, username)
    tier = db.get_user_tier(user_id)

    if tier == 'bronze':
        await update.message.reply_text("❌ Memory feature requires Silver or Gold tier.")
        return

    memories = db.get_memories(user_id, limit=10)

    if not memories:
        await update.message.reply_text("📭 No memories yet. Start chatting to build your memory!")
        return

    memory_msg = "🧠 Recent Memories:\n\n"
    for m in memories[-10:]:
        role = "You" if m['role'] == 'user' else "Alice"
        content = m['content'][:100] + "..." if len(m['content']) > 100 else m['content']
        memory_msg += f"**{role}**: {content}\n\n"

    await update.message.reply_text(memory_msg)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /tasks 命令 - Silver+ only"""
    user_id = update.effective_user.id
    username = update.effective_user.username

    ensure_user_exists(user_id, username)
    tier = db.get_user_tier(user_id)

    if tier == 'bronze':
        await update.message.reply_text("❌ Tasks feature requires Silver or Gold tier.")
        return

    # 检查是否是删除命令
    if context.args and len(context.args) >= 2:
        if context.args[0].lower() in ['delete', 'del', 'remove', 'rm', '删除', '取消']:
            try:
                task_id = int(context.args[1])
                task = db.get_task(task_id)
                if task and task['user_id'] == user_id:
                    db.delete_task(task_id)
                    await update.message.reply_text(f"✅ Task #{task_id} deleted!")
                else:
                    await update.message.reply_text(f"❌ Task #{task_id} not found or not yours.")
            except ValueError:
                await update.message.reply_text("❌ Invalid task ID. Use: /tasks delete [id]")
            return

    # 显示任务列表
    tasks = db.get_user_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "📭 暂无活跃任务\n\n"
            "💡 试试说：\n"
            "• \"帮我盯着 BTC，跌破 80000 告诉我\"\n"
            "• \"每小时推送 X 平台热点\""
        )
        return

    tasks_msg = "📋 你的任务：\n\n"
    for idx, task in enumerate(tasks, 1):
        config = task['config']

        if task['task_type'] == 'price_monitor':
            coin = config.get('coin', '?')
            target = config.get('target_price', 0)
            condition = ">" if config.get('condition') == 'above' else "<"
            cooldown = config.get('cooldown', 60)

            # 格式化提醒频率
            if cooldown >= 999999:
                frequency = "仅一次"
            elif cooldown == 0:
                frequency = "持续监控"
            elif cooldown >= 60:
                hours = cooldown // 60
                frequency = f"每 {hours} 小时" if hours > 1 else "每小时"
            else:
                frequency = f"每 {cooldown} 分钟"

            tasks_msg += f"{idx}. 🪙 {coin} 价格监控\n"
            tasks_msg += f"   条件：{condition} ${target:,.0f}\n"
            tasks_msg += f"   频率：{frequency}\n"
            tasks_msg += f"   状态：监控中 🟢\n\n"

        elif task['task_type'] == 'scheduled_report':
            topic = config.get('topic', 'N/A')
            interval = config.get('interval', 60)

            tasks_msg += f"{idx}. 📰 {topic}\n"
            tasks_msg += f"   频率：每 {interval} 分钟\n"
            tasks_msg += f"   状态：运行中 🟢\n\n"

        else:
            tasks_msg += f"{idx}. 📌 {task['task_type']}\n"
            tasks_msg += f"   状态：运行中 🟢\n\n"

    tasks_msg += "💡 说 \"取消第 X 个\" 来删除任务"
    await update.message.reply_text(tasks_msg)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员命令"""
    user_id = update.effective_user.id

    # 简单的管理员检查（可以改成从环境变量读取）
    admin_ids = [user_id]  # 暂时允许所有人，生产环境改成固定 ID

    if user_id not in admin_ids:
        return

    if not context.args:
        await update.message.reply_text("用法：/admin gold list")
        return

    if context.args[0] == "gold":
        if len(context.args) < 2:
            await update.message.reply_text("用法：/admin gold list")
            return

        if context.args[1] == "list":
            workers = molt_manager.list_active_workers()
            if not workers:
                await update.message.reply_text("📭 无运行中的 Gold 容器")
                return

            message = "🥇 Gold 容器：\n\n"
            for uid, status in workers:
                message += f"User {uid}: {status}\n"

            await update.message.reply_text(message)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    await update.message.reply_text(
        "Alice guide ☀️\n\n"
        "how to use:\n"
        "💬 just text me - no commands\n"
        "📊 ask anything about BTC\n"
        "❤️ I respond naturally\n\n"
        "completely free to use\n"
        "no token needed\n\n"
        "other places:\n"
        "🐦 Twitter: @Alice_BTC_AI\n"
        "🌐 Web: v-sk.github.io/AliceAI/website\n\n"
        "that's it"
    )


# ==================== Natural Language Intent ====================

INTENT_KEYWORDS = {
    'help': ['菜单', '帮助', 'help', '怎么用', '不会用', '教我', '命令', '功能'],
    'status': ['等级', 'status', '级别', '权限', '我是啥等级', '状态', '我的等级'],
    'tasks': ['任务', 'tasks', '监控列表', '我的任务', '哪些任务'],
}


def detect_intent(text: str) -> str | None:
    """检测自然语言意图，返回命令名或 None"""
    lower = text.lower()
    for command, keywords in INTENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return command
    return None


# ==================== Main Message Handler ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通消息"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    prompt = update.message.text

    if not prompt or len(prompt) < 2:
        return

    # 自然语言快捷命令（短消息才检测，长消息交给 AI）
    if len(prompt) <= 20:
        intent = detect_intent(prompt)
        if intent == 'help':
            await cmd_help(update, context)
            return
        elif intent == 'status':
            await cmd_status(update, context)
            return
        elif intent == 'tasks':
            await cmd_tasks(update, context)
            return

    ensure_user_exists(user_id, username)
    tier = db.get_user_tier(user_id)

    logger.info(f"Message from {username} (tier: {tier}): {prompt[:50]}...")

    # 判断是否需要 Worker 容器（复杂任务：监控、搜索、浏览等）
    if alice_chat.needs_worker(prompt):
        # 复杂任务走 Worker 容器
        if tier == 'bronze':
            result, task_created, delete_message = await handle_bronze_user(update, prompt)
        elif tier == 'silver':
            result, task_created, delete_message = await handle_silver_user(update, prompt)
        elif tier == 'gold':
            result, task_created, delete_message = await handle_gold_user(update, prompt)
        else:
            result, task_created, delete_message = await handle_bronze_user(update, prompt)

        # 截断过长的回复
        if len(result) > 4000:
            result = result[:4000] + "\n\n... (truncated)"

        # 如果创建了任务，添加 /tasks 提示
        if task_created:
            if "✅" not in result:
                result = "✅ 任务已创建！\n\n" + result
            result += "\n\n💡 使用 /tasks 查看所有任务"

        # 如果删除了任务，添加确认
        if delete_message:
            result += f"\n\n{delete_message}"

        await update.message.reply_text(result)
    else:
        # 普通对话走 Alice 轻量对话（无需容器）
        user_name = db.get_user_preference(user_id, 'nickname')

        # Silver+ 用户有记忆
        memory = None
        if tier in ['silver', 'gold', 'diamond']:
            memory = db.get_memories_for_context(user_id, limit=20)
            db.add_memory(user_id, "user", prompt)

        result = alice_chat.generate_reply(
            text=prompt,
            memory=memory,
            user_name=user_name,
            user_tier=tier
        )

        # 解析用户信息标记（昵称、时区）
        result = parse_user_info(result, user_id)

        # Silver+ 保存助手回复
        if tier in ['silver', 'gold', 'diamond'] and not result.startswith("oof"):
            db.add_memory(user_id, "assistant", result)

        # 截断过长的回复
        if len(result) > 4000:
            result = result[:4000] + "\n\n..."

        await update.message.reply_text(result)


# ==================== Main ====================

def main():
    if not TG_TOKEN:
        print("ERROR: TELEGRAM_TOKEN not found in .env")
        return

    if not ANTHROPIC_KEY:
        print("ERROR: ANTHROPIC_API_KEY not found in .env")
        return

    # 初始化数据库
    logger.info("Initializing database...")
    db.get_stats()

    # 创建 Bot
    app = ApplicationBuilder().token(TG_TOKEN).build()

    # 注册命令处理器
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("admin", cmd_admin))

    # 注册消息处理器
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # 初始化并启动调度器
    scheduler = init_scheduler(db, app.bot, check_interval=60)

    async def post_init(application):
        """Bot 启动后执行"""
        loop = asyncio.get_event_loop()
        scheduler.start(loop)
        logger.info("Scheduler started")

    app.post_init = post_init

    logger.info("Alice Bot starting...")
    print("=" * 50)
    print("Alice Bot is running!")
    print("=" * 50)

    app.run_polling()


if __name__ == '__main__':
    main()
